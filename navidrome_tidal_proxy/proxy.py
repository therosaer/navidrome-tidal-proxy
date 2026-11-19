"""Subsonic proxy: forwards everything to Navidrome, injects Tidal results where Navidrome has none."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, Response
from pathlib import Path

from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from .audio import build_flac, prune
from .backends import PREFIX, Backend
from .badge import add_badge
from .library import save_track
from .subsonic import failed, ok, render

log = logging.getLogger("tidal-proxy")
FWD_REQ = {"range", "if-range", "if-none-match", "if-modified-since", "accept", "user-agent", "content-type"}
DROP_RESP = {"transfer-encoding", "connection"}
AUTH_KEYS = {"u", "p", "t", "s", "apiKey"}
ID_KEYS = ("id", "albumId", "artistId")
NOOP_WRITES = {"scrobble", "setRating"}
INFO_KEYS = {"getArtistInfo2": "artistInfo2", "getArtistInfo": "artistInfo", "getSimilarSongs2": "similarSongs2",
             "getSimilarSongs": "similarSongs", "getAlbumInfo2": "albumInfo", "getAlbumInfo": "albumInfo"}
# Only ids we minted ourselves are ever used to build cache paths or backend calls (blocks "../" tricks).
TID_RE = re.compile(r"^tidal-(track|album|artist|playlist)-[A-Za-z0-9_-]{1,64}$")
KIND_FOR = {"stream": "track", "download": "track", "getSong": "track", "getAlbum": "album", "getArtist": "artist",
            "getPlaylist": "playlist", "getArtistInfo2": "artist", "getArtistInfo": "artist",
            "getAlbumInfo2": "album", "getAlbumInfo": "album", "getSimilarSongs2": "track", "getSimilarSongs": "track", "getLyricsBySongId": "track"}
MAX_FAILS, FAIL_WINDOW = 10, 300  # failed logins per client IP per window before Tidal calls are refused
_SECRET = re.compile(r"(?<=[?&])(p|t|s|apiKey|password)=[^&\s]*")


class _Redact(logging.Filter):
    """Keep passwords and replayable auth tokens out of uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        if isinstance(a, tuple) and len(a) >= 3 and isinstance(a[2], str):
            record.args = (*a[:2], _SECRET.sub(r"\1=REDACTED", a[2]), *a[3:])
        return True


def _norm(s) -> str:
    return re.sub(r"[\W_]+", "", str(s or "").casefold())


_DEDUPE_KEYS = {"song": lambda x: (_norm(x.get("title")), _norm(x.get("artist"))),
                "album": lambda x: (_norm(x.get("name") or x.get("title")), _norm(x.get("artist"))),
                "artist": lambda x: (_norm(x.get("name")),)}


def _relevant(kind: str, x: dict, query: str) -> bool:
    """Tidal's search returns fuzzy/related hits (e.g. other artists when you search one name).
    Keep only results where every query word appears in the item's own names."""
    if kind == "song":
        parts = [x.get("title"), x.get("album"), x.get("artist"), *[a.get("name") for a in x.get("artists", [])]]
    elif kind == "album":
        parts = [x.get("name"), x.get("artist")]
    else:
        parts = [x.get("name")]
    hay = _norm(" ".join(str(p or "") for p in parts))
    if kind == "artist":  # "adele hello" should still surface Adele (whole words only, so "baba" != "babasha")
        name = set(re.findall(r"\w+", str(x.get("name") or "").casefold()))
        if name and name <= set(re.findall(r"\w+", query.casefold())):
            return True
    return all(_norm(w) in hay for w in query.split() if _norm(w))


def _structured(artist: str, title: str, ly: dict) -> dict:
    """OpenSubsonic structuredLyrics: a synced version and/or a plain-text version."""
    base = {"displayArtist": artist, "displayTitle": title, "lang": "xxx", "offset": 0}
    out = []
    if ly.get("synced"):
        out.append({**base, "synced": True, "line": [{"start": ms, "value": v} for ms, v in ly["synced"]]})
    if ly.get("text"):
        out.append({**base, "synced": False, "line": [{"value": v} for v in ly["text"].splitlines()]})
    return {"structuredLyrics": out}


def _mark(obj, ids: set, stamp: str):
    """Add `starred` to every dict whose id is a Tidal favourite."""
    if isinstance(obj, dict):
        if obj.get("id") in ids:
            obj["starred"] = stamp
        for v in obj.values():
            _mark(v, ids, stamp)
    elif isinstance(obj, list):
        for v in obj:
            _mark(v, ids, stamp)
    return obj


def _existing(path: str) -> Path:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _trim(d: dict, n: int = 1000) -> None:
    if len(d) > n:
        d.clear()


def make_app(navidrome_url: str, backend: Backend, *, mode: str = "merge", stream_mode: str = "redirect",
             limit: int = 200, upstream: httpx.AsyncClient | None = None,
             cache_dir: str = "./cache", cache_max_bytes: int = 4 * 10**9, badge: bool = True,
             prefetch: bool = True, library_dir: str | None = None, save_on_star: bool = False,
             allowed_users: set[str] | None = None, save_min_free_bytes: int = 5 * 10**9,
             max_save_tracks: int = 200) -> FastAPI:
    app = FastAPI(title="navidrome-tidal-proxy", docs_url=None, redoc_url=None, openapi_url=None)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full request URLs (with credentials) at INFO
    allowed = {u.casefold() for u in (allowed_users or set())}
    fails: dict[str, list[float]] = {}
    search_cache: dict[tuple, tuple[float, dict]] = {}
    nd = upstream or httpx.AsyncClient(base_url=navidrome_url.rstrip("/"), timeout=30)
    auth_cache: dict[tuple, float] = {}
    logging.getLogger("uvicorn.access").addFilter(_Redact())
    cache, builds, prefetched, tasks = Path(cache_dir), {}, {}, set()
    artist_cache: dict[str, tuple[float, list]] = {}
    album_cache: dict[str, tuple[float, list]] = {}
    fav = {"at": 0.0, "data": {"song": [], "album": [], "artist": []}, "ids": set()}
    audio_slots, save_lock = asyncio.Semaphore(3), asyncio.Lock()

    def spawn(coro):
        t = asyncio.ensure_future(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async def forward(request: Request, path: str, params: list[tuple[str, str]], body: bytes) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() in FWD_REQ}
        # We relay raw bytes, so only let Navidrome compress if this client asked for it
        # (httpx would otherwise add its own gzip and hand plain clients like curl a gzip body).
        headers["accept-encoding"] = request.headers.get("accept-encoding") or "identity"
        req = nd.build_request(request.method, path, params=params, content=body or None, headers=headers)
        try:
            r = await nd.send(req, stream=True)
        except httpx.RequestError:
            log.warning("Navidrome unreachable for %s", path)
            resp = render(dict(params).get("f"), failed(0, "Navidrome is unavailable"))
            resp.status_code = 502
            return resp
        out = {k: v for k, v in r.headers.items() if k.lower() not in DROP_RESP}
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out,
                                 background=BackgroundTask(r.aclose))

    def user_ok(params) -> bool:
        """Optional allowlist (TIDAL_USERS): only these Navidrome users may touch the Tidal account."""
        return not allowed or dict(params).get("u", "").casefold() in allowed

    async def check_auth(params: list[tuple[str, str]], request: Request) -> bool:
        """Valid Navidrome credentials? Failed attempts are throttled per client IP."""
        key = tuple(sorted((k, v) for k, v in params if k in AUTH_KEYS))
        if auth_cache.get(key, 0) > time.time():
            return True
        ip, now = (request.client.host if request.client else "?"), time.time()
        recent = [t for t in fails.get(ip, []) if now - t < FAIL_WINDOW]
        if len(recent) >= MAX_FAILS:
            return False
        try:
            r = await nd.get("/rest/ping", params=[*key, ("c", "tidal-proxy"), ("v", "1.16.1"), ("f", "json")])
            good = r.json()["subsonic-response"]["status"] == "ok"
        except Exception:
            good = False
        if good:
            _trim(auth_cache)
            auth_cache[key] = now + 60
        else:
            _trim(fails)
            fails[ip] = [*recent, now]
        return good

    async def local_json(name: str, params) -> dict | None:
        """Ask Navidrome for the same call as JSON; None means 'just pass the original through'."""
        lp = [(k, v) for k, v in params if k not in ("f", "callback")] + [("f", "json")]
        try:
            r = await nd.get(f"/rest/{name}", params=lp)
            body = r.json()["subsonic-response"]
            return body if body["status"] == "ok" else None
        except Exception:
            return None

    async def favs() -> tuple[dict, set]:
        if time.time() - fav["at"] > 60:
            try:
                data = await asyncio.to_thread(backend.favorites)
                fav.update(at=time.time(), data=data, ids={x["id"] for v in data.values() for x in v})
            except Exception:
                log.exception("Could not load Tidal favourites")
                fav["at"] = time.time() - 50  # retry soon, but don't hammer
        return fav["data"], fav["ids"]

    async def marked(payload: dict) -> dict:
        _, ids = await favs()
        return _mark(payload, ids, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    async def search(endpoint: str, params, m: str) -> Response | None:
        d = dict(params)
        q = d.get("query", "").strip('"').strip()
        if m == "off" or not q:
            return None
        key = "searchResult3" if endpoint == "search3" else "searchResult2"
        local = await local_json(endpoint, params)
        if local is None:
            return None  # let Navidrome's own (error) response through untouched
        res = local.setdefault(key, {})
        want_tidal = m == "merge" or not res.get("song")
        offsets = [d.get(k, "0") for k in ("artistOffset", "albumOffset", "songOffset")]
        if want_tidal and all(o == "0" for o in offsets):
            try:
                ck = (q.casefold(), limit)
                hit = search_cache.get(ck)
                if not hit or time.time() - hit[0] > 60:  # clients often repeat the same query several times
                    _trim(search_cache, 200)
                    hit = search_cache[ck] = (time.time(), await asyncio.to_thread(backend.search, q, limit))
                t = hit[1]
                for k, n in (("artist", "artistCount"), ("album", "albumCount"), ("song", "songCount")):
                    have = {_DEDUPE_KEYS[k](x) for x in res.get(k, [])}  # skip what the library already has
                    extra = [x for x in t[k] if _relevant(k, x, q) and _DEDUPE_KEYS[k](x) not in have]
                    res[k] = res.get(k, []) + extra[: int(d.get(n, 20))]  # Subsonic default is 20
                await marked(res)
            except Exception:
                log.exception("Tidal search failed; returning local results only")
        return render(d.get("f"), local)

    async def starred(name: str, params) -> Response | None:
        local = await local_json(name, params)
        if local is None:
            return None
        data, _ = await favs()
        res = local.setdefault("starred2" if name == "getStarred2" else "starred", {})
        for k in ("artist", "album", "song"):
            res[k] = res.get(k, []) + data[k]
        await marked(res)
        return render(dict(params).get("f"), local)

    async def playlists(params) -> Response | None:
        local = await local_json("getPlaylists", params)
        if local is None:
            return None
        try:
            mine = await asyncio.to_thread(backend.playlists)
            local.setdefault("playlists", {}).setdefault("playlist", []).extend(mine)
        except Exception:
            log.exception("Could not load Tidal playlists")
        return render(dict(params).get("f"), local)

    async def top_songs(params) -> Response | None:
        d = dict(params)
        local = await local_json("getTopSongs", params)
        if local is None or (local.get("topSongs") or {}).get("song") or not d.get("artist"):
            return None
        try:
            songs = await asyncio.to_thread(backend.top_songs, d["artist"], int(d.get("count", 50)))
            if songs:
                local["topSongs"] = await marked({"song": songs})
                return render(d.get("f"), local)
        except Exception:
            log.exception("Tidal top songs failed")
        return None

    async def artist_merge(params) -> Response | None:
        """A local artist's page also lists that artist's Tidal albums you don't own."""
        local = await local_json("getArtist", params)
        art = (local or {}).get("artist")
        if not art or not art.get("name"):
            return None
        key = _norm(art["name"])
        hit = artist_cache.get(key)
        if not hit or time.time() - hit[0] > 600:
            try:
                hit = (time.time(), await asyncio.to_thread(backend.artist_albums_by_name, art["name"]))
            except Exception:
                log.exception("Tidal artist lookup failed")
                return None
            _trim(artist_cache, 500)
            artist_cache[key] = hit
        have = {_DEDUPE_KEYS["album"](x) for x in art.get("album", [])}
        extra = [x for x in hit[1] if _DEDUPE_KEYS["album"](x) not in have]
        if not extra:
            return None
        art["album"] = art.get("album", []) + (await marked(extra))
        art["albumCount"] = len(art["album"])
        return render(dict(params).get("f"), local)

    async def album_list_merge(name: str, params) -> Response | None:
        """Home-screen album lists (newest / random) get a few Tidal suggestions mixed in."""
        d = dict(params)
        kind = d.get("type")
        if kind not in ("newest", "random") or d.get("offset", "0") != "0":
            return None
        local = await local_json(name, params)
        if local is None:
            return None
        res = local.setdefault("albumList2" if name == "getAlbumList2" else "albumList", {})
        albums, size = res.get("album", []), int(d.get("size", 10))
        hit = album_cache.get(kind)
        if not hit or time.time() - hit[0] > 600:
            try:
                hit = album_cache[kind] = (time.time(), await asyncio.to_thread(backend.album_list, kind, 30))
            except Exception:
                log.exception("Tidal album list failed")
                return None
        have = {_DEDUPE_KEYS["album"](x) for x in albums}
        extra = [x for x in hit[1] if _DEDUPE_KEYS["album"](x) not in have]
        k = min(len(extra), max(1, size // 4))
        if k == 0:
            return None
        mix = albums[: max(0, size - k)] + random.sample(extra, k)
        if kind == "random":
            random.shuffle(mix)
        res["album"] = mix
        return render(d.get("f"), local)

    async def lyrics_fallback(name: str, params) -> Response | None:
        """Navidrome has no lyrics for this song: look them up on Tidal by artist and title."""
        d = dict(params)
        local = await local_json(name, params)
        if local is None:
            return None
        if name == "getLyrics":
            artist, title = d.get("artist", ""), d.get("title", "")
            if (local.get("lyrics") or {}).get("value") or not (artist and title):
                return None
        else:
            if (local.get("lyricsList") or {}).get("structuredLyrics"):
                return None
            sp = [(k, v) for k, v in params if k in AUTH_KEYS | {"id", "c", "v"}]
            song = ((await local_json("getSong", sp)) or {}).get("song") or {}
            artist, title = song.get("artist", ""), song.get("title", "")
            if not (artist and title):
                return None
        try:
            ly = await asyncio.to_thread(backend.lyrics_by_name, artist, title)
        except Exception:
            log.exception("Tidal lyrics lookup failed")
            return None
        if not ly:
            return None
        if name == "getLyrics":
            local["lyrics"] = {"artist": artist, "title": title,
                               "value": ly["text"] or "\n".join(v for _, v in ly["synced"])}
        else:
            local["lyricsList"] = _structured(artist, title, ly)
        return render(d.get("f"), local)

    async def already_local(song: dict, auth: list) -> bool:
        """Is a song with this title and artist already in the Navidrome library?"""
        try:
            r = await nd.get("/rest/search3", params=[*auth, ("c", "tidal-proxy"), ("v", "1.16.1"), ("f", "json"),
                                                      ("query", song["title"]), ("songCount", "50"),
                                                      ("artistCount", "0"), ("albumCount", "0")])
            found = r.json()["subsonic-response"]["searchResult3"].get("song", [])
        except Exception:
            return False  # can't tell: better to save than to silently skip
        want = _DEDUPE_KEYS["song"](song)
        return any(_DEDUPE_KEYS["song"](x) == want for x in found)

    async def resolve(tid: str):
        """Path of a cached FLAC for segmented lossless streams, otherwise the direct URL."""
        path = cache / f"{tid}.flac"
        if path.exists():
            path.touch()
            return path
        url = await asyncio.to_thread(backend.stream_url, tid)
        if not isinstance(url, list):
            return url
        async def build():
            async with audio_slots:  # cap simultaneous downloads/ffmpeg runs, however many clients ask
                await build_flac(url, path)
        task = builds.get(tid) or builds.setdefault(tid, asyncio.ensure_future(build()))
        try:
            await task
        finally:
            builds.pop(tid, None)
        prune(cache, cache_max_bytes)
        return path

    async def prefetch_next(tid: str) -> None:
        """Warm the cache with the next track of the same album so track changes are instant."""
        try:
            song = await asyncio.to_thread(backend.get_song, tid)
            ids = [x["id"] for x in (await asyncio.to_thread(backend.get_album, song["parent"]))["song"]]
            nxt = ids[ids.index(tid) + 1] if tid in ids and ids.index(tid) + 1 < len(ids) else None
            if nxt:
                await resolve(nxt)
        except Exception:
            log.exception("Prefetch failed for %s", tid)

    async def save_to_library(tids: list[str], auth: list) -> None:
        async with save_lock:
            for tid in tids[:max_save_tracks]:
                try:
                    if shutil.disk_usage(_existing(library_dir)).free < save_min_free_bytes:
                        log.warning("Less than %d GB free in %s; not saving more", save_min_free_bytes // 10**9, library_dir)
                        break
                    song = await asyncio.to_thread(backend.get_song, tid)
                    if await already_local(song, auth):
                        log.info("Skipping %s - %s: already in your library", song["artist"], song["title"])
                        continue
                    src = await resolve(tid)
                    if not isinstance(src, Path):
                        log.warning("%s is not a segmented lossless stream; not saving", tid)
                        continue
                    dest = await save_track(backend, tid, src, Path(library_dir))
                    log.info("Saved %s", dest)
                except Exception:
                    log.exception("Could not save %s", tid)
            try:
                await nd.get("/rest/startScan", params=[*auth, ("c", "tidal-proxy"), ("v", "1.16.1"), ("f", "json")])
            except Exception:
                log.exception("Could not trigger a Navidrome scan")

    async def star_unstar(name: str, params, request: Request) -> Response | None:
        tidal_ids = [v for k, v in params if k in ID_KEYS and v.startswith(PREFIX)]
        if not tidal_ids:
            return None
        fmt = dict(params).get("f")
        if not all(TID_RE.match(t) for t in tidal_ids):
            return render(fmt, failed(70, "Unknown Tidal id"))
        if not await check_auth(params, request):
            return render(fmt, failed(40, "Wrong username or password"))
        if not user_ok(params):
            return render(fmt, failed(50, "This user may not use Tidal"))
        try:
            for tid in tidal_ids:
                await asyncio.to_thread(backend.star if name == "star" else backend.unstar, tid)
        except Exception:
            log.exception("Tidal %s failed", name)
            return render(fmt, failed(0, "Tidal request failed"))
        fav["at"] = 0.0
        if name == "star" and save_on_star and library_dir:
            tracks = []
            for tid in tidal_ids:
                kind = tid.split("-", 2)[1]
                if kind == "track":
                    tracks.append(tid)
                elif kind == "album":
                    tracks += [x["id"] for x in (await asyncio.to_thread(backend.get_album, tid))["song"]]
            auth = [(k, v) for k, v in params if k in AUTH_KEYS]
            spawn(save_to_library(tracks, auth))
        rest_params = [(k, v) for k, v in params if not (k in ID_KEYS and v.startswith(PREFIX))]
        if any(k in ID_KEYS for k, _ in rest_params):  # mixed request: forward the local ids to Navidrome
            return await forward(request, f"/rest/{name}", rest_params, b"")
        return render(fmt, ok())

    async def tidal(endpoint: str, tid: str, request: Request, params) -> Response:
        d = dict(params)
        fmt = d.get("f")
        want = KIND_FOR.get(endpoint)
        if not TID_RE.match(tid) or (want and tid.split("-", 2)[1] != want):
            return render(fmt, failed(70, "Unknown Tidal id"))
        if not await check_auth(params, request):
            return render(fmt, failed(40, "Wrong username or password"))
        if not user_ok(params):
            return render(fmt, failed(50, "This user may not use Tidal"))
        try:
            if endpoint in ("stream", "download"):
                res = await resolve(tid)
                if prefetch and isinstance(res, Path) and time.time() - prefetched.get(tid, 0) > 600:
                    prefetched[tid] = time.time()
                    spawn(prefetch_next(tid))
                if isinstance(res, Path):
                    return FileResponse(res, media_type="audio/flac")  # handles Range requests
                return await redirect_or_proxy(res, request)
            if endpoint == "getCoverArt":
                size = int(d["size"]) if d.get("size", "").isdigit() else None
                # Proxy the bytes: some clients (e.g. curl-based ones) refuse redirects.
                img = await asyncio.to_thread(backend.cover_url, tid, size)
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                    r = await c.get(img)
                body, ctype = r.content, r.headers.get("content-type", "image/jpeg")
                if badge and r.status_code == 200:
                    try:
                        body, ctype = await asyncio.to_thread(add_badge, body), "image/jpeg"
                    except Exception:
                        log.exception("Could not badge cover art; serving it unmodified")
                return Response(body, status_code=r.status_code, media_type=ctype)
            if endpoint == "getSong":
                return render(fmt, ok(song=await marked(await asyncio.to_thread(backend.get_song, tid))))
            if endpoint == "getAlbum":
                return render(fmt, ok(album=await marked(await asyncio.to_thread(backend.get_album, tid))))
            if endpoint == "getArtist":
                return render(fmt, ok(artist=await marked(await asyncio.to_thread(backend.get_artist, tid))))
            if endpoint == "getPlaylist":
                return render(fmt, ok(playlist=await marked(await asyncio.to_thread(backend.get_playlist, tid))))
            if endpoint == "getLyricsBySongId":
                song = await asyncio.to_thread(backend.get_song, tid)
                ly = await asyncio.to_thread(backend.lyrics, tid)
                return render(fmt, ok(lyricsList=_structured(song["artist"], song["title"], ly) if ly else {}))
            if endpoint in ("getArtistInfo2", "getArtistInfo"):
                return render(fmt, ok(**{INFO_KEYS[endpoint]: await asyncio.to_thread(backend.artist_info, tid)}))
            if endpoint in ("getAlbumInfo2", "getAlbumInfo"):
                return render(fmt, ok(**{INFO_KEYS[endpoint]: await asyncio.to_thread(backend.album_info, tid)}))
            if endpoint in ("getSimilarSongs2", "getSimilarSongs"):
                songs = await asyncio.to_thread(backend.similar_songs, tid, int(d.get("count", 50)))
                return render(fmt, ok(**{INFO_KEYS[endpoint]: await marked({"song": songs})}))
        except Exception:
            log.exception("Tidal request failed: %s %s", endpoint, tid)
            return render(fmt, failed(0, "Tidal request failed"))
        if endpoint in NOOP_WRITES:
            return render(fmt, ok())
        return render(fmt, failed(70, "Not available for Tidal items"))

    async def redirect_or_proxy(url: str, request: Request) -> Response:
        if stream_mode == "redirect":
            return RedirectResponse(url)
        headers = {k: v for k, v in request.headers.items() if k.lower() in {"range", "if-range"}}
        c = httpx.AsyncClient(timeout=30)
        r = await c.send(c.build_request("GET", url, headers=headers), stream=True)
        out = {k: v for k, v in r.headers.items() if k.lower() in {"content-type", "content-length", "content-range", "accept-ranges"}}

        async def close():
            await r.aclose()
            await c.aclose()
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out, background=BackgroundTask(close))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.api_route("/rest/{endpoint:path}", methods=["GET", "POST", "HEAD"])
    async def rest(endpoint: str, request: Request):
        name = endpoint.removesuffix(".view")
        body = await request.body()
        is_form = bool(body) and "form" in request.headers.get("content-type", "")
        params = list(request.query_params.multi_items())
        if is_form:
            params += parse_qsl(body.decode("utf-8", "replace"))
        m = mode if user_ok(params) else "off"  # users outside TIDAL_USERS only ever see Navidrome
        resp = None
        if name in ("search3", "search2"):
            resp = await search(name, params, m)
        elif m != "off" and name in ("getStarred2", "getStarred"):
            resp = await starred(name, params)
        elif m != "off" and name == "getPlaylists":
            resp = await playlists(params)
        elif m != "off" and name == "getTopSongs":
            resp = await top_songs(params)
        elif name in ("star", "unstar"):
            resp = await star_unstar(name, params, request)
        else:
            tid = next((v for k, v in params if k in ID_KEYS and v.startswith(PREFIX)), None)
            if tid:
                resp = await tidal(name, tid, request, params)
            elif m != "off" and name == "getArtist":
                resp = await artist_merge(params)
            elif m != "off" and name in ("getLyrics", "getLyricsBySongId"):
                resp = await lyrics_fallback(name, params)
            elif m != "off" and name in ("getAlbumList2", "getAlbumList"):
                resp = await album_list_merge(name, params)
        if resp is not None:
            return resp
        return await forward(request, f"/rest/{endpoint}", params, b"" if is_form else body)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
    async def passthrough(path: str, request: Request):
        return await forward(request, "/" + path, list(request.query_params.multi_items()), await request.body())

    return app
