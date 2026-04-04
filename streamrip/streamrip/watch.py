"""Playlist watch-list management for streamrip.

Architecture
------------
* ``watchlist.json``  – persisted list of watched playlists + per-entry settings
* ``snapshots/<hash>.json`` – last-known track list for each watched playlist
* Scheduler – a launchd agent (macOS) or crontab entry (Linux/Windows-WSL)
  that calls ``rip watch run`` on a configurable interval.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .config import APP_DIR

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WATCH_DIR = os.path.join(APP_DIR, "watch")
WATCHLIST_PATH = os.path.join(WATCH_DIR, "watchlist.json")
SNAPSHOTS_DIR = os.path.join(WATCH_DIR, "snapshots")
WATCH_LOG_PATH = os.path.join(WATCH_DIR, "watch.log")

# macOS launchd
LAUNCHD_LABEL = "com.streamrip.watchlist"
LAUNCHD_PLIST_PATH = os.path.expanduser(
    f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist"
)

# crontab marker so we can surgically add/remove just our line
CRON_MARKER = "# streamrip-watchlist"


def _ensure_dirs() -> None:
    os.makedirs(WATCH_DIR, exist_ok=True)
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WatchedPlaylist:
    url: str
    source: str
    fallback_source: str = ""
    enabled: bool = True
    label: str = ""
    # Quality levels to download at.  Empty list = use whatever is in global config.
    # Multiple entries (e.g. [1, 3]) download at each quality into a Q{n} subfolder.
    qualities: list = field(default_factory=list)
    # Per-playlist metadata overrides.  None means "inherit from global config".
    set_playlist_to_album: bool | None = None   # override metadata.set_playlist_to_album
    renumber_tracks: bool | None = None          # override metadata.renumber_playlist_tracks

    def display_name(self) -> str:
        return self.label if self.label else self.url


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class WatchlistManager:
    """Manages the persisted watchlist and per-playlist snapshots."""

    def __init__(self) -> None:
        _ensure_dirs()
        self.playlists: list[WatchedPlaylist] = []
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if os.path.isfile(WATCHLIST_PATH):
            with open(WATCHLIST_PATH) as f:
                data = json.load(f)
            known = {f.name for f in WatchedPlaylist.__dataclass_fields__.values()}
            self.playlists = [
                WatchedPlaylist(**{k: v for k, v in p.items() if k in known})
                for p in data.get("playlists", [])
            ]

    def _save(self) -> None:
        with open(WATCHLIST_PATH, "w") as f:
            json.dump({"playlists": [asdict(p) for p in self.playlists]}, f, indent=2)

    # -- watchlist CRUD ------------------------------------------------------

    def add(
        self,
        url: str,
        source: str,
        fallback_source: str = "",
        label: str = "",
        qualities: list | None = None,
        set_playlist_to_album: bool | None = None,
        renumber_tracks: bool | None = None,
    ) -> bool:
        """Add a playlist. Returns ``False`` if already present."""
        if any(p.url == url for p in self.playlists):
            return False
        self.playlists.append(
            WatchedPlaylist(
                url=url,
                source=source,
                fallback_source=fallback_source,
                label=label,
                qualities=qualities or [],
                set_playlist_to_album=set_playlist_to_album,
                renumber_tracks=renumber_tracks,
            )
        )
        self._save()
        return True

    def remove(self, url: str) -> bool:
        """Remove a playlist and its snapshot. Returns ``False`` if not found."""
        before = len(self.playlists)
        self.playlists = [p for p in self.playlists if p.url != url]
        if len(self.playlists) == before:
            return False
        self._save()
        snap = self._snapshot_path(url)
        if os.path.isfile(snap):
            os.remove(snap)
        return True

    def set_enabled(self, url: str, enabled: bool) -> bool:
        """Enable or disable a watched playlist. Returns ``False`` if not found."""
        for p in self.playlists:
            if p.url == url:
                p.enabled = enabled
                self._save()
                return True
        return False

    def get(self, url: str) -> WatchedPlaylist | None:
        return next((p for p in self.playlists if p.url == url), None)

    # -- snapshot management -------------------------------------------------

    @staticmethod
    def _snapshot_path(url: str) -> str:
        slug = hashlib.md5(url.encode()).hexdigest()[:16]
        return os.path.join(SNAPSHOTS_DIR, f"{slug}.json")

    def load_snapshot(self, url: str) -> dict | None:
        path = self._snapshot_path(url)
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            return json.load(f)

    def save_snapshot(
        self,
        url: str,
        playlist_name: str,
        tracks: list[tuple[str, str]],
    ) -> None:
        path = self._snapshot_path(url)
        with open(path, "w") as f:
            json.dump(
                {
                    "playlist_name": playlist_name,
                    "last_checked": datetime.now().isoformat(),
                    "tracks": [list(t) for t in tracks],
                },
                f,
                indent=2,
            )

    def get_new_tracks(
        self,
        url: str,
        current_tracks: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Return tracks that are in ``current_tracks`` but not in the snapshot.

        On the first run (no snapshot yet) every track is treated as new so
        the full playlist is downloaded.
        """
        snapshot = self.load_snapshot(url)
        if snapshot is None:
            return list(current_tracks)
        known = {(t[0], t[1]) for t in snapshot["tracks"]}
        return [t for t in current_tracks if tuple(t) not in known]

    def snapshot_info(self, url: str) -> dict | None:
        """Return raw snapshot dict, or ``None`` if no snapshot exists."""
        return self.load_snapshot(url)

    # -- scheduler -----------------------------------------------------------

    @staticmethod
    def _find_rip_exe() -> str:
        """Locate the ``rip`` executable in the active environment."""
        # Prefer the rip binary that lives next to the current Python interpreter
        # (i.e. inside the same venv the user is running streamrip from).
        venv_rip = os.path.join(os.path.dirname(sys.executable), "rip")
        if os.path.isfile(venv_rip):
            return venv_rip
        found = shutil.which("rip")
        if found:
            return found
        # Last resort: invoke as a module
        return f"{sys.executable} -m streamrip.rip"

    def install_scheduler(
        self,
        interval_hours: int,
        config_path: str | None = None,
    ) -> None:
        """Install a launchd agent (macOS) or cron job (Linux/WSL).

        The scheduled job runs ``rip watch run`` on the given interval.
        Logs are written to ``WATCH_LOG_PATH``.
        """
        rip_exe = self._find_rip_exe()
        cmd: list[str] = [rip_exe]
        if config_path:
            cmd += ["--config-path", config_path]
        cmd += ["watch", "run"]

        if platform.system() == "Darwin":
            self._install_launchd(cmd, interval_hours)
        else:
            self._install_cron(cmd, interval_hours)

    def uninstall_scheduler(self) -> None:
        """Remove the launchd agent or cron job created by ``install_scheduler``."""
        if platform.system() == "Darwin":
            self._uninstall_launchd()
        else:
            self._uninstall_cron()

    def scheduler_installed(self) -> bool:
        if platform.system() == "Darwin":
            return os.path.isfile(LAUNCHD_PLIST_PATH)
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return CRON_MARKER in result.stdout

    # macOS launchd ----------------------------------------------------------

    def _install_launchd(self, cmd: list[str], interval_hours: int) -> None:
        args_xml = "\n        ".join(f"<string>{c}</string>" for c in cmd)
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        {args_xml}
    </array>
    <key>StartInterval</key>
    <integer>{interval_hours * 3600}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{WATCH_LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>{WATCH_LOG_PATH}</string>
</dict>
</plist>
"""
        os.makedirs(os.path.dirname(LAUNCHD_PLIST_PATH), exist_ok=True)
        # Unload old agent first so the new interval takes effect immediately.
        if os.path.isfile(LAUNCHD_PLIST_PATH):
            subprocess.run(
                ["launchctl", "unload", LAUNCHD_PLIST_PATH],
                capture_output=True,
            )
        with open(LAUNCHD_PLIST_PATH, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "load", LAUNCHD_PLIST_PATH], check=True)

    def _uninstall_launchd(self) -> None:
        if os.path.isfile(LAUNCHD_PLIST_PATH):
            subprocess.run(
                ["launchctl", "unload", LAUNCHD_PLIST_PATH],
                capture_output=True,
            )
            os.remove(LAUNCHD_PLIST_PATH)

    # Linux / WSL cron -------------------------------------------------------

    def _install_cron(self, cmd: list[str], interval_hours: int) -> None:
        cmd_str = " ".join(cmd)
        new_line = (
            f"0 */{interval_hours} * * * {cmd_str} "
            f">> {WATCH_LOG_PATH} 2>&1 {CRON_MARKER}"
        )
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
        lines = [l for l in existing.splitlines() if CRON_MARKER not in l]
        lines.append(new_line)
        subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            text=True,
            check=True,
        )

    def _uninstall_cron(self) -> None:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode != 0:
            return
        lines = [l for l in result.stdout.splitlines() if CRON_MARKER not in l]
        subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            text=True,
        )
