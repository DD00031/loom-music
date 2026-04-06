"""loom setup wizard — migrate from streamrip / beets configs.

Exposed as three command aliases:
    loom setup
    loom start
    loom hello

Sub-command:
    loom setup cleanup   (only available after a successful setup)
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.text import Text

console = Console()

# ─────────────────────────────────────────────────────────────────────────────
#  Paths
# ─────────────────────────────────────────────────────────────────────────────

def _loom_app_dir() -> Path:
    return Path(click.get_app_dir("loom"))


def _loom_config_path() -> Path:
    return _loom_app_dir() / "config.toml"


def _setup_sentinel() -> Path:
    """File whose existence means setup has been run successfully."""
    return _loom_app_dir() / ".setup_complete"


def _streamrip_config_path() -> Optional[Path]:
    p = Path(click.get_app_dir("streamrip")) / "config.toml"
    return p if p.is_file() else None


def _beets_config_dir() -> Optional[Path]:
    """Return the beets app dir (contains config.yaml, library.db, …) if it exists."""
    p = Path(click.get_app_dir("beets"))
    return p if p.is_dir() else None


def _beets_config_path() -> Optional[Path]:
    p = Path(click.get_app_dir("beets")) / "config.yaml"
    return p if p.is_file() else None


def _backup(path: Path) -> Optional[Path]:
    """Copy *path* to a timestamped .bak file in the same directory.

    Returns the backup path, or None if the original doesn't exist.
    """
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path.with_name(f"{path.name}.bak_{ts}")
    shutil.copy2(path, bak)
    return bak


def _restore(bak: Optional[Path], original: Path) -> None:
    """Overwrite *original* with *bak* (rollback on failure)."""
    if bak and bak.exists():
        shutil.copy2(bak, original)


# ─────────────────────────────────────────────────────────────────────────────
#  Migration helpers
# ─────────────────────────────────────────────────────────────────────────────

# Sections that live under [download.*] in the unified config.
_DOWNLOAD_SECTIONS = [
    "downloads", "qobuz", "tidal", "deezer", "soundcloud",
    "youtube", "database", "conversion", "qobuz_filters",
    "artwork", "metadata", "filepaths", "lastfm", "spotify", "cli", "misc",
]

# Keys that contain credentials — shown with a special marker.
_CREDENTIAL_KEYS = {
    "email_or_userid", "password_or_token", "arl",
    "access_token", "refresh_token", "app_id", "secrets",
    "client_id", "client_secret",
}


def _migrate_streamrip(
    sr_path: Path, loom_toml,
) -> list[str]:
    """Overlay values from a streamrip config.toml into *loom_toml*.

    Handles both old flat format ([downloads], [qobuz], …) and the new nested
    format ([download.downloads], [download.qobuz], …).
    """
    import tomlkit

    with open(sr_path, encoding="utf-8") as fh:
        sr = tomlkit.load(fh)

    # Detect whether the streamrip file uses the new nested layout already.
    if "download" in sr:
        src = sr["download"]
    else:
        src = sr  # old flat layout

    download_node = loom_toml.get("download", loom_toml)
    notes: list[str] = []

    for section in _DOWNLOAD_SECTIONS:
        if section not in src:
            continue
        if section not in download_node:
            continue
        for key, value in src[section].items():
            if key not in download_node[section]:
                continue
            # Skip version marker — keep loom's version
            if section == "misc" and key == "version":
                continue
            download_node[section][key] = value
            tag = " [bold yellow](credential)[/bold yellow]" if key in _CREDENTIAL_KEYS else ""
            notes.append(f"  [green]✓[/green] [download.{section}] {key}{tag}")

    return notes


def _migrate_beets(beets_path: Path, loom_toml) -> list[str]:
    """Overlay values from a beets config.yaml into *loom_toml['library']*."""
    import yaml

    with open(beets_path, encoding="utf-8") as fh:
        beets_cfg = yaml.safe_load(fh) or {}

    library_node = loom_toml.get("library")
    if library_node is None:
        return ["  [yellow]⚠[/yellow] No [library] section found in loom config — skipping beets migration."]

    notes: list[str] = []

    def _apply(src: dict, dst, section: str = "library") -> None:
        for key, value in src.items():
            if isinstance(value, dict):
                if key in dst:
                    _apply(value, dst[key], f"{section}.{key}")
            else:
                if key in dst:
                    dst[key] = value
                    notes.append(f"  [green]✓[/green] [{section}] {key}")

    _apply(beets_cfg, library_node)
    return notes


# ─────────────────────────────────────────────────────────────────────────────
#  Core wizard logic
# ─────────────────────────────────────────────────────────────────────────────

def _run_setup() -> None:
    """Interactive setup wizard (shared by setup / start / hello)."""
    import tomlkit
    from loom.download.config import DEFAULT_CONFIG_PATH, set_user_defaults

    loom_config = Path(DEFAULT_CONFIG_PATH)
    sentinel = _setup_sentinel()

    # ── Already set up? ──────────────────────────────────────────────────────
    if sentinel.exists():
        ts = sentinel.read_text().strip()
        console.print(
            Panel(
                f"[bold green]loom is already set up![/bold green]\n\n"
                f"Setup was completed on [cyan]{ts}[/cyan].\n\n"
                f"Config file: [cyan]{loom_config}[/cyan]\n\n"
                f"To edit your config:  [bold]loom config open[/bold]\n"
                f"To reset to defaults: [bold]loom config reset[/bold]\n"
                f"To remove old tools:  [bold]loom setup cleanup[/bold]",
                title="[bold]loom setup[/bold]",
                border_style="green",
            )
        )
        return

    # ── Detect existing configs ───────────────────────────────────────────────
    sr_path = _streamrip_config_path()
    beets_path = _beets_config_path()

    console.print(Rule("[bold cyan]loom setup wizard[/bold cyan]"))
    console.print()

    if not sr_path and not beets_path:
        console.print(
            "[yellow]No existing streamrip or beets config found.[/yellow]\n"
            "A fresh loom config will be created with default values."
        )
    else:
        console.print("[bold]Detected existing configurations:[/bold]")
        if sr_path:
            console.print(f"  [green]•[/green] streamrip: [cyan]{sr_path}[/cyan]")
        if beets_path:
            console.print(f"  [green]•[/green] beets:     [cyan]{beets_path}[/cyan]")

    console.print()
    console.print(
        Panel(
            "[bold yellow]⚠  Before continuing, please back up your configs manually![/bold yellow]\n\n"
            "loom will create automatic backups during migration, but it is always\n"
            "safer to keep your own copies of:\n"
            + (f"  • {sr_path}\n" if sr_path else "")
            + (f"  • {beets_path}\n" if beets_path else "")
            + f"  • {loom_config} (if it exists)",
            border_style="yellow",
            title="Backup warning",
        )
    )
    console.print()

    if not Confirm.ask("[bold]Continue with setup?[/bold]", default=True):
        console.print("[yellow]Setup cancelled.[/yellow]")
        return

    # ── Back up existing loom config ─────────────────────────────────────────
    bak = _backup(loom_config)
    if bak:
        console.print(f"\n[dim]Backed up existing loom config → {bak}[/dim]")

    # ── Write fresh defaults ──────────────────────────────────────────────────
    loom_config.parent.mkdir(parents=True, exist_ok=True)
    try:
        console.print("\n[bold]Step 1/3:[/bold] Writing default loom config…")
        set_user_defaults(str(loom_config))
        console.print(f"  [green]✓[/green] Created [cyan]{loom_config}[/cyan]")
    except Exception as exc:
        _restore(bak, loom_config)
        console.print(f"[red]Failed to write default config: {exc}[/red]")
        if bak:
            console.print(f"[yellow]Restored backup from {bak}[/yellow]")
        raise SystemExit(1)

    # ── Load the freshly written config ──────────────────────────────────────
    try:
        with open(loom_config, encoding="utf-8") as fh:
            loom_toml = tomlkit.load(fh)
    except Exception as exc:
        _restore(bak, loom_config)
        console.print(f"[red]Failed to parse new config: {exc}[/red]")
        raise SystemExit(1)

    migration_notes: list[str] = []

    # ── Migrate streamrip ─────────────────────────────────────────────────────
    if sr_path:
        console.print("\n[bold]Step 2/3:[/bold] Migrating streamrip settings…")
        try:
            notes = _migrate_streamrip(sr_path, loom_toml)
            migration_notes.extend(notes)
            for note in notes:
                console.print(note)
        except Exception as exc:
            _restore(bak, loom_config)
            console.print(f"[red]Error migrating streamrip config: {exc}[/red]")
            if bak:
                console.print(f"[yellow]Restored backup from {bak}[/yellow]")
            raise SystemExit(1)
    else:
        console.print("\n[dim]Step 2/3: No streamrip config found — skipped.[/dim]")

    # ── Migrate beets ─────────────────────────────────────────────────────────
    if beets_path:
        console.print("\n[bold]Step 3/3:[/bold] Migrating beets settings…")
        try:
            notes = _migrate_beets(beets_path, loom_toml)
            migration_notes.extend(notes)
            for note in notes:
                console.print(note)
        except Exception as exc:
            _restore(bak, loom_config)
            console.print(f"[red]Error migrating beets config: {exc}[/red]")
            if bak:
                console.print(f"[yellow]Restored backup from {bak}[/yellow]")
            raise SystemExit(1)
    else:
        console.print("\n[dim]Step 3/3: No beets config found — skipped.[/dim]")

    # ── Write merged config ───────────────────────────────────────────────────
    try:
        with open(loom_config, "w", encoding="utf-8") as fh:
            fh.write(tomlkit.dumps(loom_toml))
    except Exception as exc:
        _restore(bak, loom_config)
        console.print(f"[red]Failed to write merged config: {exc}[/red]")
        if bak:
            console.print(f"[yellow]Restored backup from {bak}[/yellow]")
        raise SystemExit(1)

    # ── Write sentinel ────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sentinel.write_text(ts, encoding="utf-8")

    # ── Done ──────────────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            f"[bold green]Setup complete![/bold green]\n\n"
            + (f"Migrated {len(migration_notes)} settings from your existing configs.\n\n"
               if migration_notes else "Created fresh config with defaults.\n\n")
            + f"Config: [cyan]{loom_config}[/cyan]\n\n"
            "Next steps:\n"
            "  [bold]loom config open[/bold]               — review your config\n"
            + (f"  [bold]loom setup cleanup[/bold]            — remove old streamrip / beets\n"
               if sr_path or beets_path else ""),
            title="[bold]loom setup[/bold]",
            border_style="green",
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Cleanup wizard
# ─────────────────────────────────────────────────────────────────────────────

def _pkg_installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _run_cleanup() -> None:
    """Interactively remove streamrip and beets packages + config dirs."""
    sentinel = _setup_sentinel()

    # ── Guard: setup must have been run first ─────────────────────────────────
    if not sentinel.exists():
        console.print(
            Panel(
                "[bold red]Setup has not been run yet.[/bold red]\n\n"
                "Please run [bold]loom setup[/bold] first to migrate your configs\n"
                "before removing the old packages.",
                border_style="red",
                title="loom setup cleanup",
            )
        )
        raise SystemExit(1)

    ts = sentinel.read_text().strip()
    console.print(Rule("[bold red]loom setup cleanup[/bold red]"))
    console.print()

    # ── Detect what's installed ───────────────────────────────────────────────
    has_streamrip_pkg = _pkg_installed("streamrip")
    has_beets_pkg = _pkg_installed("beets")
    sr_dir = Path(click.get_app_dir("streamrip"))
    beets_dir = _beets_config_dir()

    targets: list[str] = []
    if has_streamrip_pkg:
        targets.append("  [red]•[/red] Python package: [bold]streamrip[/bold]")
    if sr_dir.is_dir():
        targets.append(f"  [red]•[/red] Config directory: [cyan]{sr_dir}[/cyan]")
    if has_beets_pkg:
        targets.append("  [red]•[/red] Python package: [bold]beets[/bold]")
    if beets_dir:
        targets.append(f"  [red]•[/red] Config directory: [cyan]{beets_dir}[/cyan]")

    if not targets:
        console.print(
            "[green]Nothing to clean up — streamrip and beets are not installed "
            "and their config directories do not exist.[/green]"
        )
        return

    console.print(
        Panel(
            Text.from_markup(
                "[bold yellow]⚠  THIS IS A DESTRUCTIVE OPERATION.[/bold yellow]\n\n"
                "The following will be [bold red]permanently deleted[/bold red]:\n\n"
                + "\n".join(targets)
                + "\n\n"
                "[bold]Your loom config is NOT affected.[/bold]\n"
                f"Setup was completed: [cyan]{ts}[/cyan]"
            ),
            border_style="red",
            title="[bold]WARNING[/bold]",
        )
    )
    console.print()

    # ── Confirmation 1 ────────────────────────────────────────────────────────
    if not Confirm.ask(
        "[bold yellow]Are you sure you want to permanently delete these files and packages?[/bold yellow]",
        default=False,
    ):
        console.print("[green]Cleanup cancelled.[/green]")
        return

    # ── Confirmation 2 ────────────────────────────────────────────────────────
    console.print()
    console.print(
        "[bold red]This cannot be undone.[/bold red] "
        "Your music files and loom config are safe, but the old tool configs will be gone."
    )
    if not Confirm.ask("[bold yellow]Really continue?[/bold yellow]", default=False):
        console.print("[green]Cleanup cancelled.[/green]")
        return

    # ── Typed confirmation ────────────────────────────────────────────────────
    console.print()
    console.print(
        "Type [bold cyan]I UNDERSTAND[/bold cyan] (exactly) to confirm:"
    )
    typed = Prompt.ask("").strip()
    if typed != "I UNDERSTAND":
        console.print("[yellow]Confirmation text did not match — cleanup cancelled.[/yellow]")
        return

    # ── Perform cleanup ───────────────────────────────────────────────────────
    console.print()
    errors: list[str] = []

    if has_streamrip_pkg:
        console.print("[bold]Uninstalling streamrip…[/bold]")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "streamrip"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            console.print("  [green]✓[/green] streamrip uninstalled")
        else:
            msg = result.stderr.strip() or result.stdout.strip()
            console.print(f"  [yellow]⚠[/yellow] Could not uninstall streamrip: {msg}")
            errors.append(f"pip uninstall streamrip: {msg}")

    if sr_dir.is_dir():
        console.print(f"[bold]Removing streamrip config directory…[/bold]")
        try:
            shutil.rmtree(sr_dir)
            console.print(f"  [green]✓[/green] Deleted {sr_dir}")
        except Exception as exc:
            console.print(f"  [yellow]⚠[/yellow] Could not delete {sr_dir}: {exc}")
            errors.append(str(exc))

    if has_beets_pkg:
        console.print("[bold]Uninstalling beets…[/bold]")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "beets"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            console.print("  [green]✓[/green] beets uninstalled")
        else:
            msg = result.stderr.strip() or result.stdout.strip()
            console.print(f"  [yellow]⚠[/yellow] Could not uninstall beets: {msg}")
            errors.append(f"pip uninstall beets: {msg}")

    if beets_dir:
        # Beets library.db is often valuable — ask separately before deleting it
        lib_db = beets_dir / "library.db"
        delete_beets_dir = True
        if lib_db.exists():
            console.print()
            console.print(
                f"[yellow]The beets library database [cyan]{lib_db}[/cyan] was found.[/yellow]\n"
                "If you have imported music into beets previously, deleting this file\n"
                "means you will lose your library index (your actual music files are unaffected)."
            )
            delete_beets_dir = Confirm.ask(
                "Delete the entire beets config directory (including library.db)?",
                default=False,
            )
        if delete_beets_dir:
            try:
                shutil.rmtree(beets_dir)
                console.print(f"  [green]✓[/green] Deleted {beets_dir}")
            except Exception as exc:
                console.print(f"  [yellow]⚠[/yellow] Could not delete {beets_dir}: {exc}")
                errors.append(str(exc))
        else:
            console.print(f"  [dim]Kept {beets_dir}[/dim]")

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    if errors:
        console.print(
            Panel(
                "[yellow]Cleanup completed with some warnings:[/yellow]\n\n"
                + "\n".join(f"  • {e}" for e in errors),
                border_style="yellow",
                title="Cleanup done (with warnings)",
            )
        )
    else:
        console.print(
            Panel(
                "[bold green]Cleanup complete![/bold green]\n\n"
                "streamrip and beets have been removed.\n"
                "Your loom config and music library are untouched.",
                border_style="green",
                title="loom setup cleanup",
            )
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Click command group
# ─────────────────────────────────────────────────────────────────────────────

@click.group(
    "setup",
    invoke_without_command=True,
    help="Migrate existing streamrip / beets configs to loom.",
)
@click.pass_context
def setup_group(ctx):
    """Detect and migrate streamrip / beets configs into a unified loom config.

    \b
    Run once after installing loom:
        loom setup          (also: loom start, loom hello)

    \b
    After setup is complete, remove the old tools:
        loom setup cleanup
    """
    if ctx.invoked_subcommand is None:
        _run_setup()


@setup_group.command("cleanup")
def setup_cleanup():
    """Remove streamrip and beets packages and their config directories.

    Only available after 'loom setup' has been run successfully.
    Asks for multiple confirmations before deleting anything.
    """
    _run_cleanup()
