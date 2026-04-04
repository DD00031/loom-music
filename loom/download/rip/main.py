import asyncio
import json
import logging
import os
import platform

import aiofiles

from .. import db
from ..client import Client, DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from ..config import Config
from ..console import console
from ..media import (
    Media,
    Pending,
    PendingAlbum,
    PendingArtist,
    PendingLabel,
    PendingLastfmPlaylist,
    PendingPlaylist,
    PendingSingle,
    PendingSpotifyPlaylist,
    PendingTrackList,
    Playlist,
    remove_artwork_tempdirs,
)
from ..metadata import SearchResults
from ..progress import clear_progress
from .parse_url import SpotifyURL, parse_url
from .prompter import get_prompter

logger = logging.getLogger("loom.download")

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Main:
    """Provides all of the functionality called into by the CLI.

    * Logs in to Clients and prompts for credentials
    * Handles output logging
    * Handles downloading Media
    * Handles interactive search

    User input (urls) -> Main --> Download files & Output messages to terminal
    """

    def __init__(self, config: Config):
        # Data pipeline:
        # input URL -> (URL) -> (Pending) -> (Media) -> (Downloadable) -> audio file
        self.pending: list[Pending] = []
        self.media: list[Media] = []
        self.config = config
        self.clients: dict[str, Client] = {
            "qobuz": QobuzClient(config),
            "tidal": TidalClient(config),
            "deezer": DeezerClient(config),
            "soundcloud": SoundcloudClient(config),
        }

        self.database: db.Database

        c = self.config.session.database
        if c.downloads_enabled:
            downloads_db = db.Downloads(c.downloads_path)
        else:
            downloads_db = db.Dummy()

        if c.failed_downloads_enabled:
            failed_downloads_db = db.Failed(c.failed_downloads_path)
        else:
            failed_downloads_db = db.Dummy()

        self.database = db.Database(downloads_db, failed_downloads_db)

    async def _make_spotify_pending(self, parsed: "SpotifyURL") -> Pending | None:
        """Resolve a SpotifyURL into the appropriate Pending item.

        * playlist → PendingSpotifyPlaylist (embed-scrape all tracks, search each)
        * track    → PendingSingle  (embed-scrape title/artist, search once)
        * album    → PendingAlbum   (embed-scrape name/artist, search once)
        * artist   → PendingArtist  (embed-scrape name, search once)
        """
        from ..media.playlist import fetch_spotify_embed_metadata

        c = self.config.session.spotify
        client = await self.get_logged_in_client(c.source)
        fallback_client = (
            await self.get_logged_in_client(c.fallback_source)
            if c.fallback_source
            else None
        )

        if parsed.media_type == "playlist":
            return PendingSpotifyPlaylist(
                parsed.url,
                c.client_id,
                c.client_secret,
                client,
                fallback_client,
                self.config,
                self.database,
            )

        # For track / album / artist: scrape embed page then search download service
        verify_ssl = getattr(self.config.session.downloads, "verify_ssl", True)
        try:
            name, artist = await fetch_spotify_embed_metadata(
                parsed.media_type, parsed.spotify_id, verify_ssl=verify_ssl
            )
        except Exception as e:
            logger.error("Could not fetch Spotify metadata for %s: %s", parsed.url, e)
            return None

        query = f"{name} {artist}".strip()
        search_type = parsed.media_type  # "track" | "album" | "artist"
        console.print(
            f"[cyan]Searching for Spotify {search_type}:[/cyan] [bold]{query}[/bold]"
        )

        pages = await client.search(search_type, query, limit=1)
        if not pages:
            console.print(f"[red]No results found for: {query}")
            return None

        results = SearchResults.from_pages(client.source, search_type, pages)
        if not results.results:
            console.print(f"[red]No results found for: {query}")
            return None

        item_id = results.results[0].id

        if search_type == "track":
            return PendingSingle(item_id, client, self.config, self.database)
        elif search_type == "album":
            return PendingAlbum(item_id, client, self.config, self.database)
        elif search_type == "artist":
            return PendingArtist(item_id, client, self.config, self.database)

        return None

    async def add(self, url: str):
        """Add url as a pending item.

        Do not `asyncio.gather` calls to this! Use `add_all` for concurrency.
        """
        parsed = parse_url(url)
        if parsed is None:
            raise Exception(f"Unable to parse url {url}")

        if isinstance(parsed, SpotifyURL):
            pending = await self._make_spotify_pending(parsed)
            if pending is not None:
                self.pending.append(pending)
            return

        client = await self.get_logged_in_client(parsed.source)
        self.pending.append(
            await parsed.into_pending(client, self.config, self.database),
        )
        logger.debug("Added url=%s", url)

    async def add_by_id(self, source: str, media_type: str, id: str):
        client = await self.get_logged_in_client(source)
        self._add_by_id_client(client, media_type, id)

    async def add_all_by_id(self, info: list[tuple[str, str, str]]):
        sources = set(s for s, _, _ in info)
        clients = {s: await self.get_logged_in_client(s) for s in sources}
        for source, media_type, id in info:
            self._add_by_id_client(clients[source], media_type, id)

    def _add_by_id_client(self, client: Client, media_type: str, id: str):
        if media_type == "track":
            item = PendingSingle(id, client, self.config, self.database)
        elif media_type == "album":
            item = PendingAlbum(id, client, self.config, self.database)
        elif media_type == "playlist":
            item = PendingPlaylist(id, client, self.config, self.database)
        elif media_type == "label":
            item = PendingLabel(id, client, self.config, self.database)
        elif media_type == "artist":
            item = PendingArtist(id, client, self.config, self.database)
        else:
            raise Exception(media_type)

        self.pending.append(item)

    async def add_all(self, urls: list[str]):
        """Add multiple urls concurrently as pending items."""
        parsed = [parse_url(url) for url in urls]
        url_client_pairs = []
        for i, p in enumerate(parsed):
            if p is None:
                console.print(
                    f"[red]Found invalid url [cyan]{urls[i]}[/cyan], skipping.",
                )
                continue
            if isinstance(p, SpotifyURL):
                pending = await self._make_spotify_pending(p)
                if pending is not None:
                    self.pending.append(pending)
                continue
            url_client_pairs.append((p, await self.get_logged_in_client(p.source)))

        pendings = await asyncio.gather(
            *[
                url.into_pending(client, self.config, self.database)
                for url, client in url_client_pairs
            ],
        )
        self.pending.extend(pendings)

    async def get_logged_in_client(self, source: str):
        """Return a functioning client instance for `source`."""
        client = self.clients.get(source)
        if client is None:
            raise Exception(
                f"No client named {source} available. Only have {self.clients.keys()}",
            )
        if not client.logged_in:
            prompter = get_prompter(client, self.config)
            if not prompter.has_creds():
                # Get credentials from user and log into client
                await prompter.prompt_and_login()
                prompter.save()
            else:
                with console.status(f"[cyan]Logging into {source}", spinner="dots"):
                    # Log into client using credentials from config
                    await client.login()

        assert client.logged_in
        return client

    async def resolve(self):
        """Resolve all currently pending items."""
        with console.status("Resolving URLs...", spinner="dots"):
            coros = [p.resolve() for p in self.pending]
            new_media: list[Media] = [
                m for m in await asyncio.gather(*coros) if m is not None
            ]

        self.media.extend(new_media)
        self.pending.clear()

    async def rip(self):
        """Download all resolved items."""
        results = await asyncio.gather(
            *[item.rip() for item in self.media], return_exceptions=True
        )

        failed_items = 0
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Error processing media item: {result}")
                failed_items += 1

        if failed_items > 0:
            total_items = len(self.media)
            logger.info(
                f"Download completed with {failed_items} failed items out of {total_items} total items."
            )

    def _apply_quality_subfolder(self, quality_tag: str):
        """Insert a quality subfolder *inside* each resolved playlist folder.

        Transforms  ``<downloads>/<Playlist Name>/track.flac``
        into        ``<downloads>/<Playlist Name>/Q3/track.flac``
        so that multiple quality passes land in the right place.
        """
        for item in self.media:
            if isinstance(item, Playlist):
                seen: dict[str, str] = {}
                for track in item.tracks:
                    if track.folder not in seen:
                        seen[track.folder] = os.path.join(track.folder, quality_tag)
                    track.folder = seen[track.folder]
                    os.makedirs(track.folder, exist_ok=True)

    async def rip_all_qualities(self, qualities: list[int], pending_backup: list):
        """Resolve and download pending items once for each requested quality.

        Each quality gets its own subdirectory *inside* the media folder so
        files land at ``<downloads>/<Playlist>/Q{n}/track.flac``.

        Args:
            qualities: Quality integers to download at.
            pending_backup: Snapshot of ``self.pending`` before the first
                resolve so it can be replayed for every quality level.
        """
        for quality in qualities:
            console.print(
                f"\n[bold cyan]Downloading at quality level "
                f"[/bold cyan][bold yellow]{quality}[/bold yellow]"
            )
            # Apply quality to every source.
            for source_cfg in (
                self.config.session.qobuz,
                self.config.session.tidal,
                self.config.session.deezer,
                self.config.session.soundcloud,
            ):
                source_cfg.quality = quality

            self.pending = list(pending_backup)
            self.media.clear()
            await self.resolve()
            self._apply_quality_subfolder(f"Q{quality}")
            await self.rip()

    async def search_interactive(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)

        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=100)
            if len(pages) == 0:
                console.print(f"[red]No search results found for query {query}")
                return
            search_results = SearchResults.from_pages(source, media_type, pages)

        if platform.system() == "Windows":  # simple term menu not supported for windows
            from pick import pick

            choices = pick(
                search_results.results,
                title=(
                    f"{source.capitalize()} {media_type} search.\n"
                    "Press SPACE to select, RETURN to download, CTRL-C to exit."
                ),
                multiselect=True,
                min_selection_count=1,
            )
            assert isinstance(choices, list)

            await self.add_all_by_id(
                [(source, media_type, item.id) for item, _ in choices],
            )

        else:
            from simple_term_menu import TerminalMenu

            menu = TerminalMenu(
                search_results.summaries(),
                preview_command=search_results.preview,
                preview_size=0.5,
                title=(
                    f"Results for {media_type} '{query}' from {source.capitalize()}\n"
                    "SPACE - select, ENTER - download, ESC - exit"
                ),
                cycle_cursor=True,
                clear_screen=True,
                multi_select=True,
            )
            chosen_ind = menu.show()
            if chosen_ind is None:
                console.print("[yellow]No items chosen. Exiting.")
            else:
                choices = search_results.get_choices(chosen_ind)
                await self.add_all_by_id(
                    [(source, item.media_type(), item.id) for item in choices],
                )

    async def search_take_first(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=1)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        assert len(search_results.results) > 0
        first = search_results.results[0]
        await self.add_by_id(source, first.media_type(), first.id)

    async def search_output_file(
        self, source: str, media_type: str, query: str, filepath: str, limit: int
    ):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=limit)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        file_contents = json.dumps(search_results.as_list(source), indent=4)
        async with aiofiles.open(filepath, "w") as f:
            await f.write(file_contents)

        console.print(
            f"Wrote [purple]{len(search_results.results)}[/purple] results to [cyan]{filepath} as JSON!"
        )

    async def resolve_lastfm(self, playlist_url: str):
        """Resolve a last.fm playlist."""
        c = self.config.session.lastfm
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingLastfmPlaylist(
            playlist_url,
            client,
            fallback_client,
            self.config,
            self.database,
        )
        playlist = await pending_playlist.resolve()

        if playlist is not None:
            self.media.append(playlist)

    async def resolve_spotify(self, playlist_url: str):
        """Resolve a Spotify playlist."""
        c = self.config.session.spotify
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingSpotifyPlaylist(
            playlist_url,
            c.client_id,
            c.client_secret,
            client,
            fallback_client,
            self.config,
            self.database,
        )
        playlist = await pending_playlist.resolve()

        if playlist is not None:
            self.media.append(playlist)

    async def run_watched_playlists(self, url_filter: str | None = None) -> None:
        """Check all enabled watched playlists and download any new tracks.

        Handles per-playlist quality levels, track-number continuation, and
        metadata config overrides (``set_playlist_to_album``, ``renumber_tracks``).

        Multi-quality playlists are resolved and ripped immediately inside this
        method (one pass per quality level) so that the correct quality setting
        is active during each download.  Single-quality playlists are added to
        ``self.media`` for the caller's ``rip()`` call.

        Args:
            url_filter: If given, only the playlist with this exact URL is
                processed.  Useful for one-off manual checks.
        """
        from ..watch import WatchlistManager

        manager = WatchlistManager()
        targets = [
            p
            for p in manager.playlists
            if p.enabled and (url_filter is None or p.url == url_filter)
        ]

        if not targets:
            console.print("[yellow]No enabled playlists in the watchlist.")
            return

        source_cfgs = (
            self.config.session.qobuz,
            self.config.session.tidal,
            self.config.session.deezer,
            self.config.session.soundcloud,
        )

        for watched in targets:
            console.print(f"\n[bold cyan]Checking:[/bold cyan] {watched.display_name()}")
            try:
                playlist_name, current_tracks = await self._fetch_watched_tracks(watched.url)

                snapshot = manager.load_snapshot(watched.url)
                new_tracks = manager.get_new_tracks(watched.url, current_tracks)
                start_pos = len(snapshot["tracks"]) + 1 if snapshot else 1

                if not new_tracks:
                    console.print(
                        f"  [green]No new tracks in [bold]{playlist_name}[/bold]"
                    )
                    manager.save_snapshot(watched.url, playlist_name, current_tracks)
                    continue

                console.print(
                    f"  [yellow]{len(new_tracks)} new track(s) in "
                    f"[bold]{playlist_name}[/bold] (starting at #{start_pos}) — downloading…"
                )

                client = await self.get_logged_in_client(watched.source)
                fallback_client = (
                    await self.get_logged_in_client(watched.fallback_source)
                    if watched.fallback_source
                    else None
                )

                # Apply per-playlist metadata overrides
                orig_set_album = self.config.session.metadata.set_playlist_to_album
                orig_renumber = self.config.session.metadata.renumber_playlist_tracks
                if watched.set_playlist_to_album is not None:
                    self.config.session.metadata.set_playlist_to_album = (
                        watched.set_playlist_to_album
                    )
                if watched.renumber_tracks is not None:
                    self.config.session.metadata.renumber_playlist_tracks = (
                        watched.renumber_tracks
                    )

                qualities = watched.qualities if watched.qualities else [None]
                multi_quality = len(qualities) > 1

                for quality in qualities:
                    # Apply quality override
                    orig_qualities = {}
                    if quality is not None:
                        for src in source_cfgs:
                            orig_qualities[id(src)] = src.quality
                            src.quality = quality

                    pending = PendingTrackList(
                        new_tracks,
                        playlist_name,
                        client,
                        fallback_client,
                        self.config,
                        self.database,
                        start_position=start_pos,
                    )
                    result = await pending.resolve()

                    if result is not None:
                        if multi_quality and quality is not None:
                            # Insert Q{n} subfolder inside the playlist folder
                            for track in result.tracks:
                                track.folder = os.path.join(
                                    track.folder, f"Q{quality}"
                                )
                                os.makedirs(track.folder, exist_ok=True)

                        if multi_quality:
                            # Rip immediately so quality setting is still active
                            await result.rip()
                        else:
                            self.media.append(result)

                    # Restore quality
                    if quality is not None:
                        for src in source_cfgs:
                            src.quality = orig_qualities[id(src)]

                # Restore metadata overrides
                self.config.session.metadata.set_playlist_to_album = orig_set_album
                self.config.session.metadata.renumber_playlist_tracks = orig_renumber

                manager.save_snapshot(watched.url, playlist_name, current_tracks)

            except Exception as e:
                logger.error("Error checking %s: %s", watched.display_name(), e)

    async def _fetch_watched_tracks(
        self, spotify_url: str
    ) -> tuple[str, list[tuple[str, str]]]:
        """Fetch current playlist tracks for a watched Spotify playlist."""
        from ..media.playlist import PendingSpotifyPlaylist as _PSP

        fetcher = _PSP.__new__(_PSP)
        fetcher.spotify_url = spotify_url
        fetcher.config = self.config
        return await fetcher._fetch_spotify_playlist()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        # Ensure all client sessions are closed
        for client in self.clients.values():
            if hasattr(client, "session"):
                await client.session.close()

        # close global progress bar manager
        clear_progress()
        # We remove artwork tempdirs here because multiple singles
        # may be able to share downloaded artwork in the same `rip` session
        # We don't know that a cover will not be used again until end of execution
        remove_artwork_tempdirs()
