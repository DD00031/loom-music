"""loom — unified CLI: download + library management.

Command map
-----------
Download / streaming:
  loom url <urls…>                 — download from any supported URL
  loom search <source> <type> …   — interactive search & download
  loom id <source> <type> <id>    — download by service ID
  loom file <path>                 — batch-download URLs from a file
  loom lastfm <url>                — download a Last.fm playlist
  loom spotify <url>               — download a Spotify playlist / track / album
  loom watch …                     — watch playlists for new tracks (scheduler)
  loom database …                  — inspect the download history DB

Library management (beets):
  loom import [path]               — import music with MusicBrainz auto-tagging
  loom list [query]                — query & list library items
  loom modify [query]              — edit metadata on library items
  loom move [query]                — move / copy files per path template
  loom remove [query]              — remove items from the library
  loom update [query]              — re-read tags from disk
  loom write [query]               — write library metadata back to files
  loom stats                       — library statistics
  loom fields                      — list available metadata fields

Setup / migration:
  loom setup (= loom start = loom hello) — migrate from streamrip / beets
  loom setup cleanup               — remove old packages after migration

Configuration:
  loom config open                 — open the unified config.toml in your editor
  loom config reset                — reset config to defaults
  loom config path                 — print the path to config.toml

Global flags:
  -q / --quality   1,3             — download quality level(s)
  -f / --folder    ~/Music/        — override download folder
  --auto-import                    — import the download folder into the library
                                     after every download command finishes
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from functools import wraps

import click
from click_help_colors import HelpColorsGroup
from rich.logging import RichHandler
from rich.traceback import install

from loom import __version__


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_console():
    """Return the shared Rich console."""
    from loom.download.console import console
    return console


def _coro(f):
    """Wrap an async Click command so it can be used synchronously."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper


def _parse_quality(quality_str: str | None) -> list[int] | None:
    """Parse '3' → [3] or '1,3' → [1, 3].  Returns None when not supplied."""
    if quality_str is None:
        return None
    values: list[int] = []
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
                f"Quality {v} is out of range. Must be 0–4.",
                param_hint="--quality",
            )
        values.append(v)
    if not values:
        raise click.BadParameter("No quality values provided.", param_hint="--quality")
    return values


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group(
    cls=HelpColorsGroup,
    help_headers_color="yellow",
    help_options_color="green",
    invoke_without_command=True,
)
@click.version_option(version=__version__, prog_name="loom")
@click.option(
    "--config-path",
    default=None,
    help="Path to the download config (TOML). Defaults to the "
         "standard location (~/.config/loom/config.toml).",
    type=click.Path(readable=True, writable=True),
)
@click.option(
    "-f", "--folder",
    help="Folder to download music into.",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option(
    "-ndb", "--no-db",
    help="Download even if the item is already in the download database.",
    default=False,
    is_flag=True,
)
@click.option(
    "-q", "--quality",
    help=(
        "Quality level(s). Single value (e.g. 3) or comma-separated list "
        "(e.g. 1,3). Multiple qualities are saved in separate sub-folders."
    ),
    type=str,
    default=None,
)
@click.option(
    "-c", "--codec",
    help="Convert downloaded files to ALAC, FLAC, MP3, AAC, or OGG.",
)
@click.option(
    "--no-progress",
    help="Hide download progress bars.",
    is_flag=True,
    default=False,
)
@click.option(
    "--no-ssl-verify",
    help="Disable SSL certificate verification.",
    is_flag=True,
    default=False,
)
@click.option(
    "-v", "--verbose",
    help="Show debug output.",
    is_flag=True,
)
@click.option(
    "--auto-import",
    help=(
        "After a download command finishes, automatically run "
        "'loom import' on the download folder to add the new "
        "music to your beets library."
    ),
    is_flag=True,
    default=False,
)
@click.pass_context
def loom(
    ctx,
    config_path,
    folder,
    no_db,
    quality,
    codec,
    no_progress,
    no_ssl_verify,
    verbose,
    auto_import,
):
    """loom — download, tag, and manage your music library.

    \b
    Download from Qobuz, Tidal, Deezer, SoundCloud, Spotify, or Last.fm:
        loom url https://open.spotify.com/album/…
        loom search qobuz album "Kind of Blue"
        loom -q 3 url https://www.qobuz.com/…

    \b
    Organize with beets (MusicBrainz auto-tagging):
        loom import ~/Downloads/new-music/
        loom list artist:Radiohead
        loom modify year:2024 artist:Unknown

    \b
    Combine both in one step:
        loom --auto-import url https://open.spotify.com/album/…
    """
    # ── logging ──────────────────────────────────────────────────────────────
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler()],
    )
    _log = logging.getLogger("loom")
    console = _get_console()
    if verbose:
        install(console=console, suppress=[click], show_locals=True,
                locals_hide_sunder=False)
        _log.setLevel(logging.DEBUG)
    else:
        install(console=console, suppress=[click, asyncio], max_frames=1)
        _log.setLevel(logging.INFO)

    # Expose logger to download internals (they reference it as a module global)
    import loom.download.rip.cli as _download_cli
    _download_cli.logger = _log

    # ── download config ───────────────────────────────────────────────────────
    from loom.download.config import (
        DEFAULT_CONFIG_PATH,
        Config,
        OutdatedConfigError,
        set_user_defaults,
    )

    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["auto_import"] = auto_import

    if not os.path.isfile(config_path):
        console.print(
            f"No config found at [bold cyan]{config_path}[/bold cyan], "
            "creating defaults."
        )
        set_user_defaults(config_path)

    try:
        c = Config(config_path)
    except OutdatedConfigError:
        console.print("Config is outdated — auto-updating…")
        Config.update_file(config_path)
        c = Config(config_path)
    except Exception as e:
        console.print(
            f"[red]Error loading config:[/red] {e}\n"
            "Run [bold]loom config reset[/bold] to restore defaults."
        )
        ctx.obj["config"] = None
        return

    if no_db:
        c.session.database.downloads_enabled = False
    if folder:
        c.session.downloads.folder = folder

    qualities = _parse_quality(quality)
    if qualities and len(qualities) == 1:
        for svc in ("qobuz", "tidal", "deezer", "soundcloud"):
            setattr(c.session, svc, c.session.__dict__.get(svc) or getattr(c.session, svc))
            getattr(c.session, svc).quality = qualities[0]
    ctx.obj["qualities"] = qualities

    if codec:
        c.session.conversion.enabled = True
        assert codec.upper() in ("ALAC", "FLAC", "OGG", "MP3", "AAC")
        c.session.conversion.codec = codec.upper()

    if no_progress:
        c.session.cli.progress_bars = False

    if no_ssl_verify:
        c.session.downloads.verify_ssl = False

    ctx.obj["config"] = c

    # Show help when invoked with no subcommand
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# Post-download auto-import hook
# ---------------------------------------------------------------------------

# Track whether the just-completed command was a download command.
_DOWNLOAD_COMMANDS = frozenset(
    {"url", "file", "search", "id", "lastfm", "spotify"}
)


@loom.result_callback()
@click.pass_context
def _post_command(ctx, result, **_kwargs):
    """Run beets import on the download folder after a download command."""
    if not (ctx.obj and ctx.obj.get("auto_import")):
        return

    cfg = ctx.obj.get("config")
    if cfg is None:
        return

    # Determine which subcommand ran.  Click stores the last-invoked name in
    # the context that was created for the subcommand; we inspect the parent.
    invoked = getattr(ctx, "_loom_last_subcommand", None)
    if invoked not in _DOWNLOAD_COMMANDS:
        return

    download_folder = cfg.session.downloads.folder
    console = _get_console()
    console.print(
        f"\n[bold cyan]Auto-importing[/bold cyan] "
        f"[yellow]{download_folder}[/yellow] into the library…"
    )
    _run_beets("import", download_folder)


def _run_beets(*args: str) -> None:
    """Invoke a beets sub-command programmatically."""
    from loom.library.ui import _raw_main
    try:
        _raw_main(list(args))
    except SystemExit:
        pass
    except Exception as exc:
        _get_console().print(f"[red]library error:[/red] {exc}")


# Small Click middleware: record which subcommand ran so _post_command can
# decide whether to trigger auto-import.
_orig_invoke = loom.invoke


def _tracking_invoke(ctx):
    if ctx.invoked_subcommand:
        ctx._loom_last_subcommand = ctx.invoked_subcommand
    return _orig_invoke(ctx)


loom.invoke = _tracking_invoke  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Mount download / watch / database commands
# ---------------------------------------------------------------------------
# These commands are Click Command/Group objects registered on the `rip` group
# Re-mounted on `loom` — they rely only on ctx.obj keys
# that the loom root group callback already sets up identically.

from loom.download.rip.cli import (  # noqa: E402  (after loom group is defined)
    url,
    file,
    search,
    id as id_cmd,
    lastfm,
    spotify,
    watch,
    database,
)
from loom.setup_wizard import setup_group  # noqa: E402

loom.add_command(url)
loom.add_command(file)
loom.add_command(search)
loom.add_command(id_cmd,    name="id")
loom.add_command(lastfm)
loom.add_command(spotify)
loom.add_command(watch)
loom.add_command(database)

# Setup wizard — three aliases for discoverability
loom.add_command(setup_group, name="setup")
loom.add_command(setup_group, name="start")
loom.add_command(setup_group, name="hello")

# Patch example text in shared command help strings: replace "rip " → "loom ".
# These commands are shared objects; updating them once applies everywhere.
def _fix_help(cmd, recursive: bool = True) -> None:
    if cmd.help:
        cmd.help = cmd.help.replace("rip ", "loom ")
    if recursive and hasattr(cmd, "commands"):
        for sub in cmd.commands.values():
            _fix_help(sub, recursive=True)

for _cmd in (url, file, search, id_cmd, lastfm, spotify, watch, database):
    _fix_help(_cmd)


# ---------------------------------------------------------------------------
# Unified config group
# ---------------------------------------------------------------------------

@loom.group("config")
def config_group():
    """Manage loom configuration (~/.config/loom/config.toml).

    \b
    All settings — both download and library — live in one file:
        loom config open    # edit in your default editor
        loom config reset   # restore defaults (keeps credentials)
        loom config path    # print the file path
    """


@config_group.command("open")
@click.option("-v", "--vim", is_flag=True, help="Open in (Neo)Vim.")
@click.pass_context
def config_open(ctx, vim):
    """Open the config file in your editor."""
    path = ctx.obj["config_path"]
    console = _get_console()
    console.print(f"Opening [bold cyan]{path}[/bold cyan]")
    if vim:
        editor = shutil.which("nvim") or shutil.which("vim")
        if editor:
            subprocess.run([editor, path])
            return
        console.print("[yellow]nvim/vim not found — using system default.")
    click.launch(path)


@config_group.command("reset")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def config_reset(ctx, yes):
    """Reset config to defaults (preserves credentials)."""
    from loom.download.config import set_user_defaults
    from rich.prompt import Confirm

    path = ctx.obj["config_path"]
    if not yes:
        if not Confirm.ask(f"Reset config at {path}?"):
            _get_console().print("[green]Aborted.")
            return
    set_user_defaults(path)
    _get_console().print(f"[green]Reset[/green] [bold cyan]{path}[/bold cyan]")


@config_group.command("path")
@click.pass_context
def config_path(ctx):
    """Print the path to the config file."""
    _get_console().print(f"Config: [bold cyan]{ctx.obj['config_path']}")


# ---------------------------------------------------------------------------
# Beets library-management commands (pass-through wrappers)
# ---------------------------------------------------------------------------
# Each command forwards all its arguments directly to beets' own CLI parser.
# This gives full access to every beets flag without having to redeclare them.

def _beets_passthrough(name: str, help_text: str):
    """Factory: create a Click pass-through command for a beets sub-command."""

    @loom.command(
        name,
        help=help_text,
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
        },
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def _cmd(args):
        _run_beets(name, *args)

    # Give the inner function a unique name so Click doesn't complain
    _cmd.__name__ = f"beets_{name}"
    return _cmd


_beets_passthrough(
    "import",
    "Import music into the beets library with MusicBrainz auto-tagging.\n\n"
    "Accepts all standard beets import flags, e.g.:\n\n"
    "  loom import ~/Downloads/new-album/\n\n"
    "  loom import -A ~/Music/  # skip auto-tagging",
)
_beets_passthrough(
    "list",
    "Query and list items in the beets library.\n\n"
    "Accepts beets query syntax, e.g.:\n\n"
    "  loom list artist:Radiohead\n\n"
    "  loom list year:2020.. album",
)
_beets_passthrough(
    "modify",
    "Modify metadata fields on items matching a query.\n\n"
    "  loom modify genre=Jazz artist:Coltrane",
)
_beets_passthrough(
    "move",
    "Move or copy files to match your path template.\n\n"
    "  loom move artist:Unknown",
)
_beets_passthrough(
    "remove",
    "Remove items from the library (optionally deletes files).\n\n"
    "  loom remove --delete artist:Unknown",
)
_beets_passthrough(
    "update",
    "Re-read tags from files and update the library.\n\n"
    "  loom update",
)
_beets_passthrough(
    "write",
    "Write library metadata back to audio files.\n\n"
    "  loom write artist:Radiohead",
)
_beets_passthrough(
    "stats",
    "Show library statistics (track count, total duration, etc.).",
)
_beets_passthrough(
    "fields",
    "List all available metadata fields.",
)
