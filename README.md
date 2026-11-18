# Navidrome + Tidal Subsonic proxy

> **Unofficial hobby project.** Not affiliated with or endorsed by Tidal or Navidrome. It talks to Tidal's private API
> through the community [`tidalapi`](https://github.com/tamland/python-tidal) library. Use it only with your own
> subscription, at your own risk, and check Tidal's terms of service (see [Disclaimer](#disclaimer)).

A Subsonic-compatible proxy that sits in front of [Navidrome](https://www.navidrome.org). Your existing Subsonic clients
(Symfonium, Feishin, Amperfy, DSub, dromify-omarchy, ...) point at the proxy instead of Navidrome and need no changes.
Everything is forwarded to Navidrome; the proxy adds your Tidal catalogue to searches, artist pages, favourites and
playlists, and streams Tidal tracks (lossless FLAC) through the normal Subsonic API.

```
client ──> proxy :4535 ──> Navidrome :4533
                 └──────> Tidal (search, browse, favourites, playlists, stream, cover art)
```

## Features

- **Search:** Tidal results are added after your local ones. Anything you already own is removed (matched on normalised
  title/name + artist), and irrelevant fuzzy hits from Tidal are filtered out.
- **Artist pages:** a local artist's page also lists their Tidal albums, EPs, singles and compilations that you don't own.
  Top songs, bio, similar artists, similar songs (track radio) and album reviews come from Tidal when Navidrome has none.
- **Lossless streaming:** FLAC from Tidal, stitched from DASH segments into one seekable file (cached, with Range support).
  The next track of the album is prefetched so track changes are instant.
- **Favourites and playlists:** your Tidal favourites merge into `getStarred2`; starring or unstarring a Tidal track,
  album or artist updates your Tidal account. Your Tidal playlists appear in the playlist list (read-only).
- **Lyrics:** synced (LRC) and plain lyrics for Tidal tracks via `getLyricsBySongId`/`getLyrics`; local tracks keep
  Navidrome's lyrics and only fall back to Tidal when it has none.
- **Album lists:** `newest` and `random` album lists get a few Tidal suggestions mixed in (never albums you own).
- **Cover art:** Tidal covers get a small "T" badge so they are distinguishable from local music (can be disabled).
- **Save to local library (opt-in):** starring a Tidal track also copies it into your Navidrome music folder, tagged,
  and triggers a rescan. Tracks you already own are skipped.
- **Both JSON and XML** Subsonic responses, with or without the `.view` suffix.

## Requirements

- Python 3.11+ and `ffmpeg` (or just Docker, which bundles both)
- A running Navidrome and a Tidal subscription (HiFi/lossless tier for FLAC)

## Quick start (without Docker)

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python -m navidrome_tidal_proxy.setup   # asks a few questions, writes .env, logs in to Tidal, runs the doctor
venv/bin/python -m navidrome_tidal_proxy         # serve on $HOST:$PORT
```

Prefer to do it by hand? `cp .env.example .env`, edit it, then run `python -m navidrome_tidal_proxy.login` and start
the proxy. A `.env` in the working directory is loaded automatically; real environment variables take precedence.

In your client use `http://<host>:<port>` as the server address with your **normal Navidrome username and password**.

### Tidal login

`login` prints a URL. Open it, sign in to Tidal, and you will land on a page that says "Oops". That is expected: copy the
full URL from the address bar and paste it back into the terminal. The session is saved to `TIDAL_SESSION_FILE`
(owner-only permissions) and refreshed tokens are written back to it.

The PKCE login matters: the plain device-code login is capped by Tidal at AAC 320 kbps even on lossless plans.

## Docker

The image is Alpine-based (about 230 MB). A prebuilt multi-architecture image (amd64 and arm64) is published to GitHub Container Registry on every release, so
there is nothing to build. Download [`docker-compose.yml`](docker-compose.yml) into an empty folder, set `NAVIDROME_URL`
(and the music path if you use save-to-library), then:

```bash
mkdir state
docker compose run --rm -it navidrome-tidal-proxy python -m navidrome_tidal_proxy.login   # one-time Tidal login
docker compose up -d
docker compose exec navidrome-tidal-proxy python -m navidrome_tidal_proxy.doctor           # optional self-check
```

**Users and permissions (`PUID` / `PGID`).** Set them in the compose file to the user and group that own your music folder
(`id -u` / `id -g`). The container starts as root only long enough to fix the ownership of `./state`, then drops to that
user before the app runs, so the app itself never runs as root. The same ids are used to write saved tracks into your
library, so they must be allowed to write there. `PUID=0` is refused.

The compose file is otherwise hardened: read-only filesystem, all capabilities dropped except the few needed for that
ownership fix, memory and process limits, and a health check on `/healthz`. Update later with `docker compose pull && docker compose up -d`. Prefer to build it yourself? Clone the
repository and swap `image:` for `build: .`.

Put the proxy on the same Docker network as Navidrome and use its container name in `NAVIDROME_URL`
(for example `http://navidrome:4533`). A container often cannot reach the *host's* published ports, which shows up as
requests that hang until they time out.

## Doctor

`python -m navidrome_tidal_proxy.doctor` (in Docker: `docker compose exec navidrome-tidal-proxy python -m
navidrome_tidal_proxy.doctor`) checks your setup using the same environment variables as the proxy and prints OK / WARN /
FAIL with a suggested fix for each problem: settings, `ffmpeg`, whether Navidrome is reachable, whether the Tidal login is
valid, your subscription, the quality Tidal actually returns for a test stream (it catches the AAC-only login mistake),
and whether the cache and library folders are writable with enough free space. It never starts a login and changes nothing.
The exit status is 1 if anything failed.

## Configuration

All settings are environment variables (see [`.env.example`](.env.example)).

| Variable | Default | Meaning |
|---|---|---|
| `NAVIDROME_URL` | `http://localhost:4533` | Where Navidrome lives |
| `HOST` / `PORT` | `127.0.0.1` / `4534` | Listen address (the Docker image uses `0.0.0.0` / `4535`) |
| `TIDAL_MODE` | `merge` | `merge`: always add Tidal results minus duplicates; `fallback`: only when Navidrome finds no songs; `off`: pure passthrough |
| `TIDAL_QUALITY` | `hi_res_lossless` | `low_96k`, `low_320k`, `high_lossless` or `hi_res_lossless` |
| `TIDAL_SESSION_FILE` | `./tidal-session-pkce.json` | Saved Tidal login (a secret) |
| `TIDAL_LIMIT` | `200` | Tidal hits fetched per search before de-duplication (max about 300) |
| `STREAM_MODE` | `redirect` | `redirect`: 302 to Tidal's CDN for direct streams; `proxy`: relay the bytes |
| `CACHE_DIR` / `CACHE_MAX_GB` | `./cache` / `4` | Stitched FLAC cache; least recently used files are deleted beyond the cap |
| `PREFETCH` | `1` | Prefetch the next album track (`0` disables) |
| `TIDAL_BADGE` | `1` | "T" badge on Tidal covers (`0` disables) |
| `TIDAL_USERS` | empty | Comma-separated Navidrome usernames allowed to use Tidal (empty = every valid Navidrome user) |
| `LIBRARY_DIR` / `SAVE_ON_STAR` | empty / `0` | Opt-in save-to-library, see below |
| `SAVE_MIN_FREE_GB` / `MAX_SAVE_TRACKS` | `5` / `200` | Stop saving below this much free space; cap tracks saved per star |
| `LOG_LEVEL` | `INFO` | Python log level |
| `PUID` / `PGID` | `1000` / `1000` | Docker only: user and group the app runs as (see [Docker](#docker)) |

## How it behaves

- Tidal items use ids like `tidal-track-123`, `tidal-album-45`, `tidal-artist-6`, `tidal-playlist-<uuid>`. Handled calls:
  `search2/3`, `getArtist`, `getAlbum`, `getSong`, `getPlaylists`, `getPlaylist`, `getStarred(2)`, `star`, `unstar`,
  `getLyrics`, `getLyricsBySongId`, `getAlbumList(2)`, `getTopSongs`, `getArtistInfo(2)`, `getAlbumInfo(2)`, `getSimilarSongs(2)`, `getCoverArt`, `stream`, `download`.
  Everything else (and all non-`/rest` paths, such as the Navidrome web UI) passes straight through.
- Tidal is only consulted after Navidrome accepts the client's credentials. Scrobbles and ratings for Tidal items are
  accepted and ignored; unsupported calls on Tidal ids return Subsonic error 70.
- Covers are returned as image bytes, not redirects, because some clients (for example curl-based ones) refuse redirects.
- Only the first page of search results gets Tidal items (offsets above zero return local results only).

## Save to local library (opt-in)

Set `LIBRARY_DIR` to your Navidrome music folder and `SAVE_ON_STAR=1`. Starring a Tidal track (or album) then also copies
the FLAC to `LIBRARY_DIR/Tidal/<Artist>/<Album>/NN - Title.flac`, with tags and a `cover.jpg`, and starts a Navidrome scan
(the starring user must be allowed to start scans). Once saved, the track is local, so search stops showing the Tidal copy.

- Only lossless (segmented) streams can be saved.
- Saving stops when the library disk has less than `SAVE_MIN_FREE_GB` free; at most `MAX_SAVE_TRACKS` tracks per star.
- Unstarring never deletes a saved file. Before saving, the proxy searches Navidrome and skips tracks it already has
  (matched on normalised title and artist, so different spellings can still slip through).
- **Downloading from Tidal may violate its terms of service.** Off by default; enable it only if you accept that.

## Security

- **Authentication is Navidrome's.** The proxy has no accounts. Failed logins on Tidal calls are throttled per client IP
  (10 per 5 minutes).
- **Everyone with a Navidrome login can use your Tidal account** unless you set `TIDAL_USERS`. Starring changes your Tidal
  favourites and, if enabled, writes files; restrict it on shared servers.
- **Use HTTPS or a private network.** Subsonic clients put credentials in the URL, so over plain HTTP anyone on the
  network can read them. Run it behind a TLS reverse proxy, or reach it over a VPN such as Tailscale (traffic between
  tailnet devices is encrypted). Access logs mask `p`, `t`, `s` and `apiKey`, but a proxy in front of this one may not.
- **Your Tidal session file is a secret.** Keep it out of git and backups. It is created with `0600` permissions.
- Ids are validated before they are used for cache paths or backend calls, and downloads, concurrency and cache size are capped.

## Known limitations

- Built on an unofficial library and Tidal's private API; it can break without notice when Tidal changes things.
- Hi-res (24-bit) streams may arrive in a different layout; only the segmented FLAC path is stitched and cached.
- The Tidal client is serialised behind one lock, so one slow Tidal call delays others. Fine for a few users.
- Behind a reverse proxy, all clients share one IP for login throttling unless the proxy is on a trusted network.
- Tidal playlists are read-only, there is no scrobbling to Tidal, and some tracks have no year (saved files then lack a date tag).

## Troubleshooting

Run the [doctor](#doctor) first; it finds most problems and says how to fix them.

- **Requests hang, then time out (Docker):** the proxy cannot reach Navidrome. Use a shared Docker network and the container name.
- **Streams are AAC 320 instead of FLAC:** you used the device-code login. Delete the session file and run `login` again.
- **No Tidal results:** check `TIDAL_MODE` is not `off`, your user is in `TIDAL_USERS` if set, and the container log for
  "Tidal search failed".
- **Covers or artists missing in a client:** some clients cache aggressively; clear that client's image cache.

## Disclaimer

This project is not endorsed by Tidal or Navidrome. Tidal's terms restrict use of their service outside official
applications and prohibit copying content. You are responsible for how you use this software and for complying with the
terms of every service it touches.

## License

MIT, see [LICENSE](LICENSE).
