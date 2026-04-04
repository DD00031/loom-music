# loom

**Download, tag, and manage your music library — all in one command.**

loom combines a hi-res music downloader with a powerful library manager into a single, intuitive CLI. Download from streaming services, auto-tag against MusicBrainz, and keep your local collection organised — without switching tools.

```
loom url https://open.spotify.com/album/...
loom import ~/Downloads/new-album/
loom list artist:Radiohead
```

---

## What loom does

| Area | What you get |
|---|---|
| **Downloading** | Hi-res audio from Qobuz, Tidal, Deezer, SoundCloud, Spotify, and Last.fm |
| **Tagging** | MusicBrainz auto-tagging with fuzzy matching and interactive confirmation |
| **Library** | SQLite-backed library with a rich query language, path templates, and plugins |
| **Watching** | Automatic playlist tracking — only new tracks are downloaded on each run |
| **Multi-quality** | Download the same release at multiple quality levels simultaneously |

---

## Quick start

```bash
# Clone and install
git clone <this repo> && cd loom
python3.12 -m venv .venv
.venv/bin/pip install -e ./streamrip -e ./beets -e .

# Run
.venv/bin/loom --help
```

### Download
```bash
# Any supported URL — Spotify, Qobuz, Tidal, Deezer, SoundCloud, Last.fm
loom url https://open.spotify.com/album/...
loom url https://www.qobuz.com/...

# Interactive search
loom search qobuz album "Kind of Blue"

# Quality level (0 = low, 4 = hi-res max)
loom -q 3 url https://www.qobuz.com/...

# Multiple qualities at once — each gets its own sub-folder
loom -q 1,3 url https://tidal.com/browse/album/...

# Download into a specific folder
loom -f ~/Music/new url https://...
```

### Library management
```bash
# Import new music with MusicBrainz auto-tagging
loom import ~/Downloads/new-album/

# Query the library
loom list artist:Radiohead
loom list year:2020.. format:FLAC

# Edit metadata
loom modify genre="Post-Rock" artist:Mogwai

# Keep files organised per your path template
loom move

# Re-read tags from disk
loom update
```

### Download and import in one step
```bash
loom --auto-import url https://open.spotify.com/album/...
```
The `--auto-import` flag runs `loom import` on the download folder automatically once the download finishes.

### Playlist watching
```bash
# Watch a Spotify playlist — only new tracks are downloaded on each run
loom watch add https://open.spotify.com/playlist/... -s qobuz

# Run manually
loom watch run

# Install an automatic scheduler (launchd on macOS, cron on Linux)
loom watch install --interval 6   # check every 6 hours

loom watch list
loom watch uninstall
```

### Configuration
```bash
# Download settings (quality defaults, folders, credentials, …)
loom config download open

# Library settings (path templates, plugins, …)
loom config library open
```

---

## All commands

### Downloading
| Command | Description |
|---|---|
| `loom url <urls…>` | Download from one or more URLs |
| `loom search <source> <type> <query>` | Interactive search and download |
| `loom id <source> <type> <id>` | Download by service ID |
| `loom file <path>` | Batch-download from a file of URLs |
| `loom lastfm <url>` | Download a Last.fm playlist |
| `loom spotify <url>` | Download a Spotify playlist, album, or track |

### Library
| Command | Description |
|---|---|
| `loom import [path]` | Import music with MusicBrainz auto-tagging |
| `loom list [query]` | Query and list library items |
| `loom modify [query]` | Edit metadata on matching items |
| `loom move [query]` | Move/copy files to match path template |
| `loom remove [query]` | Remove items from the library |
| `loom update [query]` | Re-read tags from disk |
| `loom write [query]` | Write library metadata back to files |
| `loom stats` | Library statistics |
| `loom fields` | List all available metadata fields |

### Watching
| Command | Description |
|---|---|
| `loom watch add <url>` | Add a playlist to the watchlist |
| `loom watch run` | Check for new tracks and download them |
| `loom watch list` | Show all watched playlists |
| `loom watch install` | Set up automatic scheduler |
| `loom watch uninstall` | Remove the scheduler |

### Config & utilities
| Command | Description |
|---|---|
| `loom config download …` | Manage download config (TOML) |
| `loom config library …` | Manage library config (YAML) |
| `loom database browse <table>` | Inspect the download history database |

---

## Acknowledgements

loom would not exist without the two excellent open-source projects it is built on.

### [streamrip](https://github.com/nathom/streamrip)
> *by Nathan Thomas (nathom)*

streamrip is the engine behind every download in loom. It provides the async download pipeline, per-service API clients (Qobuz, Tidal, Deezer, SoundCloud), Spotify embed scraping, playlist watching, multi-quality support, and all the file-handling plumbing. If you want a standalone downloader without library management, check out streamrip directly.

**License:** GPL-3.0

### [beets](https://beets.io)
> *by Adrian Sampson and the beets contributors*

beets powers everything on the library side of loom. It provides the MusicBrainz auto-tagger, the SQLite library with its expressive query language, path-template-based file organisation, and a rich plugin ecosystem (lyrics, artwork, Last.fm, Discogs, and 60+ more). If you already have a music library and want best-in-class organisation without the downloader, use beets directly.

**License:** MIT

---

## License

loom itself is released under the **MIT License**.
The bundled `streamrip/` source is GPL-3.0 and the bundled `beets/` source is MIT.
