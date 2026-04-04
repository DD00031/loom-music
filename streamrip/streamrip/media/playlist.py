import asyncio
import html
import logging
import os
import random
import re
from contextlib import ExitStack
from dataclasses import dataclass

import aiohttp
from rich.text import Text

from .. import progress
from ..client import Client
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filepath
from ..metadata import (
    AlbumMetadata,
    Covers,
    PlaylistMetadata,
    SearchResults,
    TrackMetadata,
)
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .artwork import download_artwork
from .media import Media, Pending
from .track import Track

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class PendingPlaylistTrack(Pending):
    id: str
    client: Client
    config: Config
    folder: str
    playlist_name: str
    position: int
    db: Database

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(f"Track ({self.id}) already logged in database. Skipping.")
            return None
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Could not stream track {self.id}: {e}")
            return None

        album = AlbumMetadata.from_track_resp(resp, self.client.source)
        if album is None:
            logger.error(
                f"Track ({self.id}) not available for stream on {self.client.source}",
            )
            self.db.set_failed(self.client.source, "track", self.id)
            return None
        meta = TrackMetadata.from_resp(album, self.client.source, resp)
        if meta is None:
            logger.error(
                f"Track ({self.id}) not available for stream on {self.client.source}",
            )
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        c = self.config.session.metadata
        if c.renumber_playlist_tracks:
            meta.tracknumber = self.position
        if c.set_playlist_to_album:
            album.album = self.playlist_name

        quality = self.config.session.get_source(self.client.source).quality
        try:
            embedded_cover_path, downloadable = await asyncio.gather(
                self._download_cover(album.covers, self.folder),
                self.client.get_downloadable(self.id, quality),
            )
        except NonStreamableError as e:
            logger.error(f"Error fetching download info for track {self.id}: {e}")
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        return Track(
            meta,
            downloadable,
            self.config,
            self.folder,
            embedded_cover_path,
            self.db,
        )

    async def _download_cover(self, covers: Covers, folder: str) -> str | None:
        embed_path, _ = await download_artwork(
            self.client.session,
            folder,
            covers,
            self.config.session.artwork,
            for_playlist=True,
        )
        return embed_path


@dataclass(slots=True)
class Playlist(Media):
    name: str
    config: Config
    client: Client
    tracks: list[PendingPlaylistTrack]

    async def preprocess(self):
        progress.add_title(self.name)

    async def postprocess(self):
        progress.remove_title(self.name)

    async def download(self):
        track_resolve_chunk_size = 20

        async def _resolve_download(item: PendingPlaylistTrack):
            try:
                track = await item.resolve()
                if track is None:
                    return
                await track.rip()
            except Exception as e:
                logger.error(f"Error downloading track: {e}")

        batches = self.batch(
            [_resolve_download(track) for track in self.tracks],
            track_resolve_chunk_size,
        )

        for batch in batches:
            results = await asyncio.gather(*batch, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Batch processing error: {result}")

    @staticmethod
    def batch(iterable, n=1):
        total = len(iterable)
        for ndx in range(0, total, n):
            yield iterable[ndx : min(ndx + n, total)]


@dataclass(slots=True)
class PendingPlaylist(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Playlist | None:
        try:
            resp = await self.client.get_metadata(self.id, "playlist")
        except NonStreamableError as e:
            logger.error(
                f"Playlist {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = PlaylistMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error creating playlist: {e}")
            return None
        name = meta.name
        parent = self.config.session.downloads.folder
        folder = os.path.join(parent, clean_filepath(name))
        tracks = [
            PendingPlaylistTrack(
                id,
                self.client,
                self.config,
                folder,
                name,
                position + 1,
                self.db,
            )
            for position, id in enumerate(meta.ids())
        ]
        return Playlist(name, self.config, self.client, tracks)


@dataclass(slots=True)
class PendingLastfmPlaylist(Pending):
    lastfm_url: str
    client: Client
    fallback_client: Client | None
    config: Config
    db: Database

    @dataclass(slots=True)
    class Status:
        found: int
        failed: int
        total: int

        def text(self) -> Text:
            return Text.assemble(
                "Searching for last.fm tracks (",
                (f"{self.found} found", "bold green"),
                ", ",
                (f"{self.failed} failed", "bold red"),
                ", ",
                (f"{self.total} total", "bold"),
                ")",
            )

    async def resolve(self) -> Playlist | None:
        try:
            playlist_title, titles_artists = await self._parse_lastfm_playlist(
                self.lastfm_url,
            )
        except Exception as e:
            logger.error("Error occured while parsing last.fm page: %s", e)
            return None

        requests = []

        s = self.Status(0, 0, len(titles_artists))
        if self.config.session.cli.progress_bars:
            with console.status(s.text(), spinner="moon") as status:

                def callback():
                    status.update(s.text())

                for title, artist in titles_artists:
                    requests.append(self._make_query(f"{title} {artist}", s, callback))
                results: list[tuple[str | None, bool]] = await asyncio.gather(*requests)
        else:

            def callback():
                pass

            for title, artist in titles_artists:
                requests.append(self._make_query(f"{title} {artist}", s, callback))
            results: list[tuple[str | None, bool]] = await asyncio.gather(*requests)

        parent = self.config.session.downloads.folder
        folder = os.path.join(parent, clean_filepath(playlist_title))

        pending_tracks = []
        for pos, (id, from_fallback) in enumerate(results, start=1):
            if id is None:
                logger.warning(f"No results found for {titles_artists[pos-1]}")
                continue

            if from_fallback:
                assert self.fallback_client is not None
                client = self.fallback_client
            else:
                client = self.client

            pending_tracks.append(
                PendingPlaylistTrack(
                    id,
                    client,
                    self.config,
                    folder,
                    playlist_title,
                    pos,
                    self.db,
                ),
            )

        return Playlist(playlist_title, self.config, self.client, pending_tracks)

    async def _make_query(
        self,
        query: str,
        search_status: Status,
        callback,
    ) -> tuple[str | None, bool]:
        """Search for a track with the main source, and use fallback source
        if that fails.

        Args:
        ----
            query (str): Query to search
            s (Status):
            callback: function to call after each query completes

        Returns: A 2-tuple, where the first element contains the ID if it was found,
        and the second element is True if the fallback source was used.
        """
        with ExitStack() as stack:
            # ensure `callback` is always called
            stack.callback(callback)
            pages = await self.client.search("track", query, limit=1)
            if len(pages) > 0:
                logger.debug(f"Found result for {query} on {self.client.source}")
                search_status.found += 1
                return (
                    SearchResults.from_pages(self.client.source, "track", pages)
                    .results[0]
                    .id
                ), False

            if self.fallback_client is None:
                logger.debug(f"No result found for {query} on {self.client.source}")
                search_status.failed += 1
                return None, False

            pages = await self.fallback_client.search("track", query, limit=1)
            if len(pages) > 0:
                logger.debug(f"Found result for {query} on {self.client.source}")
                search_status.found += 1
                return (
                    SearchResults.from_pages(
                        self.fallback_client.source,
                        "track",
                        pages,
                    )
                    .results[0]
                    .id
                ), True

            logger.debug(f"No result found for {query} on {self.client.source}")
            search_status.failed += 1
        return None, True

    async def _parse_lastfm_playlist(
        self,
        playlist_url: str,
    ) -> tuple[str, list[tuple[str, str]]]:
        """From a last.fm url, return the playlist title, and a list of
        track titles and artist names.

        Each page contains 50 results, so `num_tracks // 50 + 1` requests
        are sent per playlist.

        :param url:
        :type url: str
        :rtype: tuple[str, list[tuple[str, str]]]
        """
        logger.debug("Fetching lastfm playlist")

        title_tags = re.compile(r'<a\s+href="[^"]+"\s+title="([^"]+)"')
        re_total_tracks = re.compile(r'data-playlisting-entry-count="(\d+)"')
        re_playlist_title_match = re.compile(
            r'<h1 class="playlisting-playlist-header-title">([^<]+)</h1>',
        )

        def find_title_artist_pairs(page_text):
            info: list[tuple[str, str]] = []
            titles = title_tags.findall(page_text)  # [2:]
            for i in range(0, len(titles) - 1, 2):
                info.append((html.unescape(titles[i]), html.unescape(titles[i + 1])))
            return info

        async def fetch(session: aiohttp.ClientSession, url, **kwargs):
            async with session.get(url, **kwargs) as resp:
                return await resp.text("utf-8")

        # Create new session so we're not bound by rate limit
        verify_ssl = getattr(self.config.session.downloads, "verify_ssl", True)
        connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
        connector = aiohttp.TCPConnector(**connector_kwargs)

        async with aiohttp.ClientSession(connector=connector) as session:
            page = await fetch(session, playlist_url)
            playlist_title_match = re_playlist_title_match.search(page)
            if playlist_title_match is None:
                raise Exception("Error finding title from response")

            playlist_title: str = html.unescape(playlist_title_match.group(1))

            title_artist_pairs: list[tuple[str, str]] = find_title_artist_pairs(page)

            total_tracks_match = re_total_tracks.search(page)
            if total_tracks_match is None:
                raise Exception("Error parsing lastfm page: %s", page)
            total_tracks = int(total_tracks_match.group(1))

            remaining_tracks = total_tracks - 50  # already got 50 from 1st page
            if remaining_tracks <= 0:
                return playlist_title, title_artist_pairs

            last_page = (
                1 + int(remaining_tracks // 50) + int(remaining_tracks % 50 != 0)
            )
            requests = []
            for page in range(2, last_page + 1):
                requests.append(fetch(session, playlist_url, params={"page": page}))
            results = await asyncio.gather(*requests)

        for page in results:
            title_artist_pairs.extend(find_title_artist_pairs(page))

        return playlist_title, title_artist_pairs

    async def _make_query_mock(
        self,
        _: str,
        s: Status,
        callback,
    ) -> tuple[str | None, bool]:
        await asyncio.sleep(random.uniform(1, 20))
        if random.randint(0, 4) >= 1:
            s.found += 1
        else:
            s.failed += 1
        callback()
        return None, False


@dataclass(slots=True)
class PendingSpotifyPlaylist(Pending):
    spotify_url: str
    client_id: str
    client_secret: str
    client: Client
    fallback_client: Client | None
    config: Config
    db: Database

    # Spotify playlist URL pattern: https://open.spotify.com/playlist/{id}
    _PLAYLIST_URL_RE = re.compile(
        r"https?://open\.spotify\.com/playlist/([A-Za-z0-9]+)",
    )

    @dataclass(slots=True)
    class Status:
        found: int
        failed: int
        total: int

        def text(self) -> Text:
            return Text.assemble(
                "Searching for Spotify tracks (",
                (f"{self.found} found", "bold green"),
                ", ",
                (f"{self.failed} failed", "bold red"),
                ", ",
                (f"{self.total} total", "bold"),
                ")",
            )

    async def resolve(self) -> Playlist | None:
        try:
            playlist_title, titles_artists = await self._fetch_spotify_playlist()
        except Exception as e:
            logger.error("Error fetching Spotify playlist: %s", e)
            return None

        requests = []
        s = self.Status(0, 0, len(titles_artists))

        if self.config.session.cli.progress_bars:
            with console.status(s.text(), spinner="moon") as status:

                def callback():
                    status.update(s.text())

                for title, artist in titles_artists:
                    requests.append(self._make_query(f"{title} {artist}", s, callback))
                results: list[tuple[str | None, bool]] = await asyncio.gather(*requests)
        else:

            def callback():
                pass

            for title, artist in titles_artists:
                requests.append(self._make_query(f"{title} {artist}", s, callback))
            results: list[tuple[str | None, bool]] = await asyncio.gather(*requests)

        parent = self.config.session.downloads.folder
        folder = os.path.join(parent, clean_filepath(playlist_title))

        pending_tracks = []
        for pos, (id, from_fallback) in enumerate(results, start=1):
            if id is None:
                logger.warning(f"No results found for {titles_artists[pos - 1]}")
                continue

            if from_fallback:
                assert self.fallback_client is not None
                track_client = self.fallback_client
            else:
                track_client = self.client

            pending_tracks.append(
                PendingPlaylistTrack(
                    id,
                    track_client,
                    self.config,
                    folder,
                    playlist_title,
                    pos,
                    self.db,
                ),
            )

        return Playlist(playlist_title, self.config, self.client, pending_tracks)

    async def _make_query(
        self,
        query: str,
        search_status: Status,
        callback,
    ) -> tuple[str | None, bool]:
        with ExitStack() as stack:
            stack.callback(callback)
            pages = await self.client.search("track", query, limit=1)
            if len(pages) > 0:
                logger.debug(f"Found result for {query} on {self.client.source}")
                search_status.found += 1
                return (
                    SearchResults.from_pages(self.client.source, "track", pages)
                    .results[0]
                    .id
                ), False

            if self.fallback_client is None:
                logger.debug(f"No result found for {query} on {self.client.source}")
                search_status.failed += 1
                return None, False

            pages = await self.fallback_client.search("track", query, limit=1)
            if len(pages) > 0:
                logger.debug(
                    f"Found result for {query} on {self.fallback_client.source}",
                )
                search_status.found += 1
                return (
                    SearchResults.from_pages(
                        self.fallback_client.source,
                        "track",
                        pages,
                    )
                    .results[0]
                    .id
                ), True

            logger.debug(f"No result found for {query} on any source")
            search_status.failed += 1
        return None, True

    async def _fetch_spotify_playlist(
        self,
    ) -> tuple[str, list[tuple[str, str]]]:
        """Fetch playlist name and (title, artist) pairs from the Spotify embed page.

        Parses the ``__NEXT_DATA__`` JSON blob embedded in the playlist's embed
        page — no API credentials or Premium subscription required.
        """
        import json as _json

        m = self._PLAYLIST_URL_RE.match(self.spotify_url)
        if m is None:
            raise Exception(
                f"Could not parse Spotify playlist URL: {self.spotify_url}",
            )
        playlist_id = m.group(1)

        verify_ssl = getattr(self.config.session.downloads, "verify_ssl", True)
        connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
        connector = aiohttp.TCPConnector(**connector_kwargs)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"https://open.spotify.com/embed/playlist/{playlist_id}",
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(
                        f"Failed to fetch Spotify embed page (HTTP {resp.status}): {text}",
                    )
                page_html = await resp.text("utf-8")

        next_data_match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            page_html,
            re.DOTALL,
        )
        if next_data_match is None:
            raise Exception(
                "Could not find __NEXT_DATA__ in Spotify embed page. "
                "Spotify may have changed their page structure.",
            )

        data = _json.loads(next_data_match.group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        playlist_name: str = entity["name"]

        titles_artists: list[tuple[str, str]] = []
        for item in entity.get("trackList", []):
            title = item.get("title", "")
            # subtitle contains artist(s) joined by ",\xa0" — take the first one
            subtitle = item.get("subtitle", "")
            artist = subtitle.split(",\xa0")[0].strip()
            if title:
                titles_artists.append((title, artist))

        return playlist_name, titles_artists


async def fetch_spotify_embed_metadata(
    media_type: str,
    spotify_id: str,
    verify_ssl: bool = True,
) -> tuple[str, str]:
    """Return ``(name, primary_artist)`` for any Spotify item via its embed page.

    Works for ``track``, ``album``, and ``artist`` types without any API
    credentials or Spotify Premium.
    """
    import json as _json

    url = f"https://open.spotify.com/embed/{media_type}/{spotify_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
    connector = aiohttp.TCPConnector(**connector_kwargs)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise Exception(
                    f"Failed to fetch Spotify embed page (HTTP {resp.status})"
                )
            page_html = await resp.text("utf-8")

    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        page_html,
        re.DOTALL,
    )
    if m is None:
        raise Exception("Could not find __NEXT_DATA__ in Spotify embed page")

    entity = _json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    name: str = entity["name"]
    subtitle: str = entity.get("subtitle", "")
    artist = subtitle.split(",\xa0")[0].strip() if subtitle else ""
    return name, artist


@dataclass(slots=True)
class PendingTrackList(Pending):
    """Search for and download a pre-supplied list of (title, artist) pairs.

    Used by the playlist-watch feature to download only the *new* tracks that
    appeared since the last snapshot, without having to re-scrape the source.
    The search-and-download logic is identical to ``PendingLastfmPlaylist``.
    """

    titles_artists: list[tuple[str, str]]
    playlist_name: str
    client: Client
    fallback_client: Client | None
    config: Config
    db: Database
    # When continuing a watched playlist, pass the snapshot track count so that
    # newly added tracks pick up numbering where it left off instead of from 1.
    start_position: int = 1

    @dataclass(slots=True)
    class Status:
        found: int
        failed: int
        total: int

        def text(self) -> Text:
            return Text.assemble(
                "Searching for new tracks (",
                (f"{self.found} found", "bold green"),
                ", ",
                (f"{self.failed} failed", "bold red"),
                ", ",
                (f"{self.total} total", "bold"),
                ")",
            )

    async def resolve(self) -> Playlist | None:
        if not self.titles_artists:
            return None

        s = self.Status(0, 0, len(self.titles_artists))
        requests = []

        if self.config.session.cli.progress_bars:
            with console.status(s.text(), spinner="moon") as status:

                def callback():
                    status.update(s.text())

                for title, artist in self.titles_artists:
                    requests.append(self._make_query(f"{title} {artist}", s, callback))
                results: list[tuple[str | None, bool]] = await asyncio.gather(*requests)
        else:

            def callback():
                pass

            for title, artist in self.titles_artists:
                requests.append(self._make_query(f"{title} {artist}", s, callback))
            results: list[tuple[str | None, bool]] = await asyncio.gather(*requests)

        parent = self.config.session.downloads.folder
        folder = os.path.join(parent, clean_filepath(self.playlist_name))

        pending_tracks = []
        for offset, (item_id, from_fallback) in enumerate(results):
            if item_id is None:
                logger.warning(f"No results found for {self.titles_artists[offset]}")
                continue
            track_client = self.fallback_client if from_fallback else self.client
            assert track_client is not None
            pending_tracks.append(
                PendingPlaylistTrack(
                    item_id,
                    track_client,
                    self.config,
                    folder,
                    self.playlist_name,
                    self.start_position + offset,
                    self.db,
                )
            )

        if not pending_tracks:
            return None
        return Playlist(self.playlist_name, self.config, self.client, pending_tracks)

    async def _make_query(
        self,
        query: str,
        search_status: Status,
        callback,
    ) -> tuple[str | None, bool]:
        with ExitStack() as stack:
            stack.callback(callback)
            pages = await self.client.search("track", query, limit=1)
            if len(pages) > 0:
                search_status.found += 1
                return (
                    SearchResults.from_pages(self.client.source, "track", pages)
                    .results[0]
                    .id
                ), False

            if self.fallback_client is None:
                search_status.failed += 1
                return None, False

            pages = await self.fallback_client.search("track", query, limit=1)
            if len(pages) > 0:
                search_status.found += 1
                return (
                    SearchResults.from_pages(
                        self.fallback_client.source, "track", pages
                    )
                    .results[0]
                    .id
                ), True

            search_status.failed += 1
        return None, True
