import asyncio
import json
import logging
import os
import shutil
import subprocess
from functools import wraps
from typing import Any

import aiofiles
import aiohttp
import click
from click_help_colors import HelpColorsGroup  # type: ignore
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.prompt import Confirm
from rich.traceback import install

from loom import __version__
from .. import db
from ..config import DEFAULT_CONFIG_PATH, Config, OutdatedConfigError, set_user_defaults
from ..console import console
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .main import Main


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


def parse_quality(quality_str: str | None) -> list[int] | None:
    """Parse a quality string into a list of validated quality integers.

    Accepts a single value ("3") or a comma-separated list ("1,3").
    Returns ``None`` when the string is ``None`` (not supplied by the user).
    Raises ``click.BadParameter`` on invalid input.
    """
    if quality_str is None:
        return None
    values = []
    for part in quality_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            raise click.BadParameter(
                f"'{part}' is not a valid integer. "
                "Quality must be 0–4, e.g. '3' or '1,3'.",
                param_hint="--quality",
            )
        if not (0 <= v <= 4):
            raise click.BadParameter(
                f"Quality {v} is out of range. Must be between 0 and 4.",
                param_hint="--quality",
            )
        values.append(v)
    if not values:
        raise click.BadParameter("No quality values provided.", param_hint="--quality")
    return values


@click.group(
    cls=HelpColorsGroup,
    help_headers_color="yellow",
    help_options_color="green",
)
@click.version_option(version=__version__)
@click.option(
    "--config-path",
    default=DEFAULT_CONFIG_PATH,
    help="Path to the configuration file",
    type=click.Path(readable=True, writable=True),
)
@click.option(
    "-f",
    "--folder",
    help="The folder to download items into.",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option(
    "-ndb",
    "--no-db",
    help="Download items even if they have been logged in the database",
    default=False,
    is_flag=True,
)
@click.option(
    "-q",
    "--quality",
    help=(
        "Quality level(s) to download. A single value (e.g. 3) or a "
        "comma-separated list for multiple qualities (e.g. 1,3). "
        "Each quality is saved in its own subfolder when multiple are given."
    ),
    type=str,
    default=None,
)
@click.option(
    "-c",
    "--codec",
    help="Convert the downloaded files to an audio codec (ALAC, FLAC, MP3, AAC, or OGG)",
)
@click.option(
    "--no-progress",
    help="Do not show progress bars",
    is_flag=True,
    default=False,
)
@click.option(
    "--no-ssl-verify",
    help="Disable SSL certificate verification (use if you encounter SSL errors)",
    is_flag=True,
    default=False,
)
@click.option(
    "-v",
    "--verbose",
    help="Enable verbose output (debug mode)",
    is_flag=True,
)
@click.pass_context
def rip(
    ctx, config_path, folder, no_db, quality, codec, no_progress, no_ssl_verify, verbose
):
    """Streamrip: the all in one music downloader."""
    global logger
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler()],
    )
    logger = logging.getLogger("loom.download")
    if verbose:
        install(
            console=console,
            suppress=[
                click,
            ],
            show_locals=True,
            locals_hide_sunder=False,
        )
        logger.setLevel(logging.DEBUG)
        logger.debug("Showing all debug logs")
    else:
        install(console=console, suppress=[click, asyncio], max_frames=1)
        logger.setLevel(logging.INFO)

    if not os.path.isfile(config_path):
        console.print(
            f"No file found at [bold cyan]{config_path}[/bold cyan], creating default config.",
        )
        set_user_defaults(config_path)

    # pass to subcommands
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path

    try:
        c = Config(config_path)
    except OutdatedConfigError as e:
        console.print(e)
        console.print("Auto-updating config file...")
        Config.update_file(config_path)
        c = Config(config_path)
    except Exception as e:
        console.print(
            f"Error loading config from [bold cyan]{config_path}[/bold cyan]: {e}\n"
            "Try running [bold]rip config reset[/bold]",
        )
        ctx.obj["config"] = None
        return

    # set session config values to command line args
    if no_db:
        c.session.database.downloads_enabled = False
    if folder is not None:
        c.session.downloads.folder = folder

    qualities = parse_quality(quality)
    if qualities is not None:
        # When only a single quality is given apply it immediately like before.
        # When multiple are given they are handled at download time per command.
        if len(qualities) == 1:
            c.session.qobuz.quality = qualities[0]
            c.session.tidal.quality = qualities[0]
            c.session.deezer.quality = qualities[0]
            c.session.soundcloud.quality = qualities[0]
    ctx.obj["qualities"] = qualities  # may be None, [q], or [q1, q2, ...]

    if codec is not None:
        c.session.conversion.enabled = True
        assert codec.upper() in ("ALAC", "FLAC", "OGG", "MP3", "AAC")
        c.session.conversion.codec = codec.upper()

    if no_progress:
        c.session.cli.progress_bars = False

    if no_ssl_verify:
        c.session.downloads.verify_ssl = False

    ctx.obj["config"] = c


@rip.command()
@click.argument("urls", nargs=-1, required=True)
@click.pass_context
@coro
async def url(ctx, urls):
    """Download content from URLs."""
    if ctx.obj["config"] is None:
        return

    try:
        with ctx.obj["config"] as cfg:
            cfg: Config
            updates = cfg.session.misc.check_for_updates
            if updates:
                # Run in background
                version_coro = asyncio.create_task(
                    latest_streamrip_version(
                        verify_ssl=cfg.session.downloads.verify_ssl
                    )
                )
            else:
                version_coro = None

            qualities = ctx.obj.get("qualities")
            async with Main(cfg) as main:
                await main.add_all(urls)
                if qualities and len(qualities) > 1:
                    await main.rip_all_qualities(qualities, list(main.pending))
                else:
                    await main.resolve()
                    await main.rip()

            if version_coro is not None:
                latest_version, notes = await version_coro
                if latest_version != __version__:
                    console.print(
                        f"\n[green]A new version of streamrip [cyan]v{latest_version}[/cyan]"
                        " is available! Run [white][bold]pip3 install streamrip --upgrade[/bold][/white]"
                        " to update.[/green]\n"
                    )

                    console.print(Markdown(notes))

    except aiohttp.ClientConnectorCertificateError as e:
        from ..utils.ssl_utils import print_ssl_error_help

        console.print(f"[red]SSL Certificate verification error: {e}[/red]")
        print_ssl_error_help()


@rip.command()
@click.argument(
    "path",
    required=True,
    type=click.Path(exists=True, readable=True, file_okay=True, dir_okay=False),
)
@click.pass_context
@coro
async def file(ctx, path):
    """Download content from URLs in a file.

    Example usage:

        rip file urls.txt
    """
    try:
        with ctx.obj["config"] as cfg:
            async with Main(cfg) as main:
                async with aiofiles.open(path, "r") as f:
                    content = await f.read()
                    try:
                        items: Any = json.loads(content)
                        loaded = True
                    except json.JSONDecodeError:
                        items = content.split()
                        loaded = False
                if loaded:
                    console.print(
                        f"Detected json file. Loading [yellow]{len(items)}[/yellow] items"
                    )
                    await main.add_all_by_id(
                        [(i["source"], i["media_type"], i["id"]) for i in items]
                    )
                else:
                    s = set(items)
                    if len(s) < len(items):
                        console.print(
                            f"Found [orange]{len(items)-len(s)}[/orange] repeated URLs!"
                        )
                        items = list(s)
                    console.print(
                        f"Detected list of urls. Loading [yellow]{len(items)}[/yellow] items"
                    )
                    await main.add_all(items)

                await main.resolve()
                await main.rip()
    except aiohttp.ClientConnectorCertificateError as e:
        from ..utils.ssl_utils import print_ssl_error_help

        console.print(f"[red]SSL Certificate verification error: {e}[/red]")
        print_ssl_error_help()


@rip.group()
def config():
    """Manage configuration files."""


@config.command("open")
@click.option("-v", "--vim", help="Open in (Neo)Vim", is_flag=True)
@click.pass_context
def config_open(ctx, vim):
    """Open the config file in a text editor."""
    config_path = ctx.obj["config_path"]

    console.print(f"Opening file at [bold cyan]{config_path}")
    if vim:
        if shutil.which("nvim") is not None:
            subprocess.run(["nvim", config_path])
        elif shutil.which("vim") is not None:
            subprocess.run(["vim", config_path])
        else:
            logger.error("Could not find nvim or vim. Using default launcher.")
            click.launch(config_path)
    else:
        click.launch(config_path)


@config.command("reset")
@click.option("-y", "--yes", help="Don't ask for confirmation.", is_flag=True)
@click.pass_context
def config_reset(ctx, yes):
    """Reset the config file."""
    config_path = ctx.obj["config_path"]
    if not yes:
        if not Confirm.ask(
            f"Are you sure you want to reset the config file at {config_path}?",
        ):
            console.print("[green]Reset aborted")
            return

    set_user_defaults(config_path)
    console.print(f"Reset the config file at [bold cyan]{config_path}!")


@config.command("path")
@click.pass_context
def config_path(ctx):
    """Display the path of the config file."""
    config_path = ctx.obj["config_path"]
    console.print(f"Config path: [bold cyan]'{config_path}'")


@rip.group()
def database():
    """View and modify the downloads and failed downloads databases."""


@database.command("browse")
@click.argument("table")
@click.pass_context
def database_browse(ctx, table):
    """Browse the contents of a table.

    Available tables:

        * Downloads

        * Failed
    """
    from rich.table import Table

    cfg: Config = ctx.obj["config"]

    if table.lower() == "downloads":
        downloads = db.Downloads(cfg.session.database.downloads_path)
        t = Table(title="Downloads database")
        t.add_column("Row")
        t.add_column("ID")
        for i, row in enumerate(downloads.all()):
            t.add_row(f"{i:02}", *row)
        console.print(t)

    elif table.lower() == "failed":
        failed = db.Failed(cfg.session.database.failed_downloads_path)
        t = Table(title="Failed downloads database")
        t.add_column("Source")
        t.add_column("Media Type")
        t.add_column("ID")
        for i, row in enumerate(failed.all()):
            t.add_row(f"{i:02}", *row)
        console.print(t)

    else:
        console.print(
            f"[red]Invalid database[/red] [bold]{table}[/bold]. [red]Choose[/red] [bold]downloads "
            "[red]or[/red] failed[/bold].",
        )


@rip.command()
@click.option(
    "-f",
    "--first",
    help="Automatically download the first search result without showing the menu.",
    is_flag=True,
)
@click.option(
    "-o",
    "--output-file",
    help="Write search results to a file instead of showing interactive menu.",
    type=click.Path(writable=True),
)
@click.option(
    "-n",
    "--num-results",
    help="Maximum number of search results to show",
    default=100,
    type=click.IntRange(min=1),
)
@click.argument("source", required=True)
@click.argument("media-type", required=True)
@click.argument("query", required=True)
@click.pass_context
@coro
async def search(ctx, first, output_file, num_results, source, media_type, query):
    """Search for content using a specific source.

    Example:

        rip search qobuz album 'rumours'
    """
    if first and output_file:
        console.print("Cannot choose --first and --output-file!")
        return
    qualities = ctx.obj.get("qualities")
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            if first:
                await main.search_take_first(source, media_type, query)
            elif output_file:
                await main.search_output_file(
                    source, media_type, query, output_file, num_results
                )
            else:
                await main.search_interactive(source, media_type, query)
            if qualities and len(qualities) > 1:
                await main.rip_all_qualities(qualities, list(main.pending))
            else:
                await main.resolve()
                await main.rip()


@rip.command()
@click.option("-s", "--source", help="The source to search tracks on.")
@click.option(
    "-fs",
    "--fallback-source",
    help="The source to search tracks on if no results were found with the main source.",
)
@click.argument("url", required=True)
@click.pass_context
@coro
async def lastfm(ctx, source, fallback_source, url):
    """Download tracks from a last.fm playlist."""
    config = ctx.obj["config"]
    if source is not None:
        config.session.lastfm.source = source
    if fallback_source is not None:
        config.session.lastfm.fallback_source = fallback_source
    with config as cfg:
        async with Main(cfg) as main:
            await main.resolve_lastfm(url)
            await main.rip()


@rip.command()
@click.option(
    "-s",
    "--source",
    help="The download source to search tracks on (e.g. qobuz, tidal, deezer, soundcloud).",
)
@click.option(
    "-fs",
    "--fallback-source",
    help="The download source to use if no results were found with the main source.",
)
@click.option("--client-id", help="Spotify app Client ID (overrides config).")
@click.option("--client-secret", help="Spotify app Client Secret (overrides config).")
@click.argument("url", required=True)
@click.pass_context
@coro
async def spotify(ctx, source, fallback_source, client_id, client_secret, url):
    """Download tracks from a Spotify playlist.

    Requires a Spotify Developer app. Create one at
    https://developer.spotify.com/dashboard and set client_id and
    client_secret in the [spotify] section of your config, or pass them
    via --client-id / --client-secret.

    Example:

        rip spotify -s qobuz https://open.spotify.com/playlist/...
    """
    config = ctx.obj["config"]
    if source is not None:
        config.session.spotify.source = source
    if fallback_source is not None:
        config.session.spotify.fallback_source = fallback_source
    if client_id is not None:
        config.session.spotify.client_id = client_id
    if client_secret is not None:
        config.session.spotify.client_secret = client_secret
    with config as cfg:
        async with Main(cfg) as main:
            await main.resolve_spotify(url)
            await main.rip()


@rip.command()
@click.argument("source")
@click.argument("media-type")
@click.argument("id")
@click.pass_context
@coro
async def id(ctx, source, media_type, id):
    """Download an item by ID."""
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            await main.add_by_id(source, media_type, id)
            await main.resolve()
            await main.rip()


# ---------------------------------------------------------------------------
# watch command group
# ---------------------------------------------------------------------------


@rip.group()
def watch():
    """Watch playlists for new tracks and download them automatically.

    Quickstart (Spotify example):

    \b
        rip watch add https://open.spotify.com/playlist/… -s qobuz
        rip watch install --interval 6
        rip watch list

    The scheduler calls ``rip watch run`` on the configured interval and only
    downloads tracks that weren't present last time — the full playlist is
    downloaded on the very first run.
    """


@watch.command("add")
@click.argument("url")
@click.option("-s", "--source", required=True, help="Download source (qobuz, tidal, deezer, soundcloud).")
@click.option("-fs", "--fallback-source", default="", help="Fallback source if primary has no results.")
@click.option("-l", "--label", default="", help="Optional human-readable label for this playlist.")
@click.option(
    "-q", "--quality",
    default=None,
    help="Quality level(s) to download at, e.g. '3' or '1,3'. Empty = use global config.",
)
@click.option(
    "--set-playlist-to-album/--no-set-playlist-to-album",
    default=None,
    help="Override: tag all tracks with the playlist name as the album. Default: use global config.",
)
@click.option(
    "--renumber-tracks/--no-renumber-tracks",
    default=None,
    help="Override: renumber tracks sequentially within the playlist. Default: use global config.",
)
@click.pass_context
def watch_add(ctx, url, source, fallback_source, label, quality, set_playlist_to_album, renumber_tracks):
    """Add a playlist URL to the watchlist."""
    from ..watch import WatchlistManager

    qualities: list[int] = []
    if quality is not None:
        try:
            qualities = parse_quality(quality)
        except click.BadParameter as e:
            raise click.UsageError(str(e)) from e

    manager = WatchlistManager()
    added = manager.add(
        url,
        source,
        fallback_source=fallback_source,
        label=label,
        qualities=qualities if qualities else None,
        set_playlist_to_album=set_playlist_to_album,
        renumber_tracks=renumber_tracks,
    )
    if added:
        name = label or url
        console.print(f"[green]Added[/green] [cyan]{name}[/cyan] to watchlist.")
        if qualities:
            console.print(f"  Quality levels: {qualities}")
        if set_playlist_to_album is not None:
            console.print(f"  Set playlist as album tag: {set_playlist_to_album}")
        if renumber_tracks is not None:
            console.print(f"  Renumber tracks: {renumber_tracks}")
        console.print(
            "Run [bold]rip watch install[/bold] to set up automatic checking, "
            "or [bold]rip watch run[/bold] to check now."
        )
    else:
        console.print(f"[yellow]{url} is already in the watchlist.")


@watch.command("remove")
@click.argument("url")
def watch_remove(url):
    """Remove a playlist from the watchlist (also deletes its snapshot)."""
    from ..watch import WatchlistManager

    manager = WatchlistManager()
    if manager.remove(url):
        console.print(f"[green]Removed[/green] [cyan]{url}[/cyan] from watchlist.")
    else:
        console.print(f"[red]{url} was not found in the watchlist.")


@watch.command("enable")
@click.argument("url")
def watch_enable(url):
    """Re-enable a disabled watched playlist."""
    from ..watch import WatchlistManager

    manager = WatchlistManager()
    if manager.set_enabled(url, True):
        console.print(f"[green]Enabled[/green] [cyan]{url}[/cyan].")
    else:
        console.print(f"[red]{url} was not found in the watchlist.")


@watch.command("disable")
@click.argument("url")
def watch_disable(url):
    """Disable a watched playlist without removing it from the watchlist."""
    from ..watch import WatchlistManager

    manager = WatchlistManager()
    if manager.set_enabled(url, False):
        console.print(f"[yellow]Disabled[/yellow] [cyan]{url}[/cyan].")
    else:
        console.print(f"[red]{url} was not found in the watchlist.")


@watch.command("list")
def watch_list():
    """Show all watched playlists and their current status."""
    from rich.table import Table

    from ..watch import WatchlistManager

    manager = WatchlistManager()
    if not manager.playlists:
        console.print("[yellow]Watchlist is empty. Use [bold]rip watch add <url>[/bold] to get started.")
        return

    t = Table(title="Watched Playlists")
    t.add_column("#", style="dim")
    t.add_column("Label / URL", no_wrap=False)
    t.add_column("Source")
    t.add_column("Fallback")
    t.add_column("Status")
    t.add_column("Last checked")
    t.add_column("Known tracks")

    for i, p in enumerate(manager.playlists, 1):
        snap = manager.snapshot_info(p.url)
        last_checked = snap["last_checked"][:19].replace("T", " ") if snap else "never"
        track_count = str(len(snap["tracks"])) if snap else "—"
        status = "[green]enabled" if p.enabled else "[red]disabled"
        t.add_row(
            str(i),
            p.display_name(),
            p.source,
            p.fallback_source or "—",
            status,
            last_checked,
            track_count,
        )

    console.print(t)

    scheduler_status = WatchlistManager().scheduler_installed()
    label = "installed" if scheduler_status else "not installed"
    color = "green" if scheduler_status else "yellow"
    console.print(f"\nScheduler: [{color}]{label}[/{color}]")


@watch.command("run")
@click.option("--url", default=None, help="Only check this specific playlist URL.")
@click.pass_context
@coro
async def watch_run(ctx, url):
    """Check watched playlists and download any new tracks.

    This is the command the scheduler calls automatically. You can also run
    it manually at any time.
    """
    config = ctx.obj.get("config")
    if config is None:
        return
    with config as cfg:
        async with Main(cfg) as main:
            await main.run_watched_playlists(url_filter=url)
            await main.rip()


@watch.command("install")
@click.option(
    "--interval",
    default=6,
    type=click.IntRange(min=1),
    show_default=True,
    help="How often to check for new tracks, in hours.",
)
@click.pass_context
def watch_install(ctx, interval):
    """Install the automatic scheduler (launchd on macOS, cron on Linux).

    The scheduler runs ``rip watch run`` every INTERVAL hours and appends
    output to the watch log.
    """
    from ..watch import WatchlistManager, WATCH_LOG_PATH

    config_path = ctx.obj.get("config_path")
    manager = WatchlistManager()
    manager.install_scheduler(interval_hours=interval, config_path=config_path)
    console.print(
        f"[green]Scheduler installed.[/green] "
        f"Playlists will be checked every [bold]{interval}[/bold] hour(s)."
    )
    console.print(f"Log: [cyan]{WATCH_LOG_PATH}")


@watch.command("uninstall")
def watch_uninstall():
    """Remove the automatic scheduler without touching the watchlist."""
    from ..watch import WatchlistManager

    manager = WatchlistManager()
    if manager.scheduler_installed():
        manager.uninstall_scheduler()
        console.print("[green]Scheduler removed.")
    else:
        console.print("[yellow]Scheduler is not currently installed.")


@watch.command("status")
def watch_status():
    """Show the scheduler status and path to the watch log."""
    from ..watch import WatchlistManager, WATCH_LOG_PATH, WATCHLIST_PATH, WATCH_DIR

    manager = WatchlistManager()
    installed = manager.scheduler_installed()
    label = "installed" if installed else "not installed"
    color = "green" if installed else "yellow"

    console.print(f"Scheduler:    [{color}]{label}[/{color}]")
    console.print(f"Watchlist:    [cyan]{WATCHLIST_PATH}")
    console.print(f"Snapshots:    [cyan]{WATCH_DIR}/snapshots/")
    console.print(f"Log:          [cyan]{WATCH_LOG_PATH}")
    console.print(f"Playlists:    [bold]{len(manager.playlists)}[/bold] total")


async def latest_streamrip_version(verify_ssl: bool = True) -> tuple[str, str | None]:
    """Get the latest streamrip version from PyPI and release notes from GitHub.

    Args:
        verify_ssl: Whether to verify SSL certificates

    Returns:
        A tuple of (version, release_notes)
    """
    # Create connector with appropriate SSL settings
    connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
    connector = aiohttp.TCPConnector(**connector_kwargs)

    async with aiohttp.ClientSession(connector=connector) as s:
        async with s.get("https://pypi.org/pypi/streamrip/json") as resp:
            data = await resp.json()
        version = data["info"]["version"]

        if version == __version__:
            return version, None

        async with s.get(
            "https://api.github.com/repos/nathom/streamrip/releases/latest"
        ) as resp:
            json = await resp.json()
        notes = json["body"]
    return version, notes


if __name__ == "__main__":
    rip()
