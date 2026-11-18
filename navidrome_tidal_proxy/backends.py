"""Tidal backend. Everything returns Subsonic-shaped dicts so the proxy stays backend-agnostic.

IDs: tidal-track-<n>, tidal-album-<n>, tidal-artist-<n>.
Uses the unofficial `tidalapi` library; it is synchronous, so the proxy calls it via threads.
"""
from __future__ import annotations

import random
import re
import threading
from pathlib import Path
from typing import Protocol

PREFIX = "tidal-"
_LRC = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")
_WIMP = re.compile(r"\[wimpLink [^\]]*\](.*?)\[/wimpLink\]", re.S)


def _clean(text: str) -> str:
    """Tidal bios/reviews embed [wimpLink ...]Name[/wimpLink] markup; keep just the name."""
    return _WIMP.sub(r"\1", text or "")


def parse_id(s: str) -> tuple[str, str]:
    _, kind, raw = s.split("-", 2)
    return kind, raw


class Backend(Protocol):
    def search(self, query: str, limit: int) -> dict: ...  # {"song": [...], "album": [...], "artist": [...]}
    def get_song(self, id: str) -> dict: ...
    def get_album(self, id: str) -> dict: ...  # includes "song": [...]
    def get_artist(self, id: str) -> dict: ...  # includes "album": [...]
    def artist_albums_by_name(self, name: str) -> list[dict]: ...  # exact-name Tidal artist's full catalogue
    def stream_url(self, id: str) -> str | list[str]: ...
    def cover_url(self, id: str, size: int | None) -> str: ...
    def favorites(self) -> dict: ...  # {"song": [...], "album": [...], "artist": [...]}
    def star(self, id: str) -> None: ...
    def unstar(self, id: str) -> None: ...
    def playlists(self) -> list[dict]: ...
    def get_playlist(self, id: str) -> dict: ...  # includes "entry": [...]
    def top_songs(self, artist_name: str, count: int) -> list[dict]: ...
    def artist_info(self, id: str) -> dict: ...  # {"biography": str, "similarArtist": [...]}
    def similar_songs(self, id: str, count: int) -> list[dict]: ...
    def album_info(self, id: str) -> dict: ...  # {"notes": str}
    def lyrics(self, id: str) -> dict | None: ...  # {"text": str, "synced": [(ms, line)]}
    def lyrics_by_name(self, artist: str, title: str) -> dict | None: ...
    def album_list(self, kind: str, count: int) -> list[dict]: ...  # "newest" | "random" recommendations


def _year(album) -> int | None:
    try:
        return album.year
    except Exception:
        return None


def song_dict(t) -> dict:
    alb, art = t.album, t.artist
    return {
        "id": f"{PREFIX}track-{t.id}", "parent": f"{PREFIX}album-{alb.id}", "isDir": False,
        "title": t.name, "album": alb.name, "artist": art.name, "track": getattr(t, "track_num", None),
        "year": _year(alb), "coverArt": f"{PREFIX}album-{alb.id}", "duration": t.duration,
        "suffix": "flac", "contentType": "audio/flac", "type": "music", "isVideo": False,
        "albumId": f"{PREFIX}album-{alb.id}", "artistId": f"{PREFIX}artist-{art.id}",
        "artists": [{"id": f"{PREFIX}artist-{a.id}", "name": a.name} for a in (getattr(t, "artists", None) or [art])],
    }


def album_dict(a) -> dict:
    return {
        "id": f"{PREFIX}album-{a.id}", "name": a.name, "title": a.name, "isDir": True,
        "artist": a.artist.name, "artistId": f"{PREFIX}artist-{a.artist.id}",
        "coverArt": f"{PREFIX}album-{a.id}", "songCount": getattr(a, "num_tracks", None),
        "duration": getattr(a, "duration", None), "year": _year(a),
    }


def artist_dict(a) -> dict:
    return {"id": f"{PREFIX}artist-{a.id}", "name": a.name, "coverArt": f"{PREFIX}artist-{a.id}"}


def playlist_dict(p) -> dict:
    return {
        "id": f"{PREFIX}playlist-{p.id}", "name": p.name, "owner": "tidal", "public": False,
        "songCount": getattr(p, "num_tracks", 0), "duration": getattr(p, "duration", 0),
        "coverArt": f"{PREFIX}playlist-{p.id}", "comment": getattr(p, "description", None) or None,
    }


class TidalApiBackend:
    def __init__(self, session_file: str, quality: str = "hi_res_lossless", pkce: bool = True):
        import tidalapi

        self.t = tidalapi
        self.lock = threading.Lock()
        self.session = tidalapi.Session()
        self.session.audio_quality = getattr(tidalapi.Quality, quality)
        # Reuses the saved login; otherwise prompts once (see login.py). PKCE matters: the default
        # OAuth device login is capped by Tidal to AAC 320 even on HiFi/Max plans.
        if not self.session.login_session_file(Path(session_file), do_pkce=pkce):
            raise RuntimeError("Tidal login failed")
        self._file, self._tok, self._plock = Path(session_file), self.session.access_token, threading.Lock()
        self._file.chmod(0o600)
        for name in ("search", "get_song", "get_album", "get_artist", "artist_albums_by_name", "stream_url",
                     "cover_url", "favorites", "star", "unstar", "playlists", "get_playlist", "top_songs",
                     "artist_info", "similar_songs", "album_info", "lyrics", "lyrics_by_name", "album_list"):
            setattr(self, name, self._persisting(getattr(self, name)))

    def _persisting(self, fn):
        """tidalapi refreshes expired access tokens in memory only; save them so a restart still logs in."""
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            finally:
                with self._plock:
                    if self.session.access_token != self._tok:
                        self.session.save_session_to_file(self._file)
                        self._file.chmod(0o600)
                        self._tok = self.session.access_token
        return wrapper

    def search(self, query: str, limit: int) -> dict:
        t = self.t
        with self.lock:
            r = self.session.search(query, models=[t.media.Track, t.album.Album, t.artist.Artist], limit=limit)
        return {
            "song": [song_dict(x) for x in r["tracks"]],
            "album": [album_dict(x) for x in r["albums"]],
            "artist": [artist_dict(x) for x in r["artists"]],
        }

    def get_song(self, id: str) -> dict:
        with self.lock:
            return song_dict(self.session.track(parse_id(id)[1]))

    def get_album(self, id: str) -> dict:
        with self.lock:
            a = self.session.album(parse_id(id)[1])
            d = album_dict(a)
            d["song"] = [song_dict(x) for x in a.tracks()]
        return d

    def _catalogue(self, a) -> list:
        # Tidal splits an artist's catalogue: studio albums, EPs/singles, and compilations/appears-on.
        albums, seen = [], set()
        for getter in (a.get_albums, a.get_ep_singles, a.get_other):
            try:
                for x in getter(limit=100):
                    if x.id not in seen:
                        seen.add(x.id)
                        albums.append(x)
            except Exception:
                pass
        albums.sort(key=lambda x: _year(x) or 0, reverse=True)
        return albums

    def get_artist(self, id: str) -> dict:
        with self.lock:
            a = self.session.artist(parse_id(id)[1])
            d = artist_dict(a)
            albums = self._catalogue(a)
            d["album"] = [album_dict(x) for x in albums]
            d["albumCount"] = len(albums)
        return d

    def artist_albums_by_name(self, name: str) -> list[dict]:
        with self.lock:
            found = self.session.search(name, models=[self.t.artist.Artist], limit=5)["artists"]
            match = next((a for a in found if a.name.casefold() == name.casefold()), None)
            return [album_dict(x) for x in self._catalogue(match)] if match else []

    def stream_url(self, id: str) -> str | list[str]:
        with self.lock:
            track = self.session.track(parse_id(id)[1])
            try:
                urls = track.get_stream().get_stream_manifest().get_urls()
                return urls[0] if len(urls) == 1 else urls  # many URLs = DASH segments (lossless)
            except Exception:
                pass
            return track.get_url()  # lower-quality direct URL fallback (DASH/hi-res can't be redirected)

    def cover_url(self, id: str, size: int | None) -> str:
        kind, raw = parse_id(id)
        with self.lock:
            obj = {"track": lambda: self.session.track(raw).album, "album": lambda: self.session.album(raw),
                   "playlist": lambda: self.session.playlist(raw)}.get(kind, lambda: self.session.artist(raw))()
            want = size or 640
            sizes = (80, 160, 320, 480, 640, 750, 1080, 1280)  # valid sizes differ per object type
            for dim in [d for d in sizes if d >= want] + sorted((d for d in sizes if d < want), reverse=True):
                try:
                    return obj.image(dim)
                except Exception:
                    continue
            return obj.image()

    def favorites(self) -> dict:
        with self.lock:
            f = self.session.user.favorites
            return {"song": [song_dict(x) for x in f.tracks(limit=500)],
                    "album": [album_dict(x) for x in f.albums(limit=500)],
                    "artist": [artist_dict(x) for x in f.artists(limit=500)]}

    def _fav(self, id: str, add: bool) -> None:
        kind, raw = parse_id(id)
        with self.lock:
            f = self.session.user.favorites
            fn = {"track": ("add_track", "remove_track"), "album": ("add_album", "remove_album"),
                  "artist": ("add_artist", "remove_artist")}.get(kind)
            if fn:
                getattr(f, fn[0 if add else 1])(raw)

    def star(self, id: str) -> None:
        self._fav(id, True)

    def unstar(self, id: str) -> None:
        self._fav(id, False)

    def playlists(self) -> list[dict]:
        with self.lock:
            return [playlist_dict(p) for p in self.session.user.playlists()]

    def get_playlist(self, id: str) -> dict:
        with self.lock:
            p = self.session.playlist(parse_id(id)[1])
            d = playlist_dict(p)
            try:
                tracks = p.tracks(limit=500)
            except TypeError:
                tracks = p.tracks()
            d["entry"] = [song_dict(x) for x in tracks]
        return d

    def top_songs(self, artist_name: str, count: int) -> list[dict]:
        with self.lock:
            found = self.session.search(artist_name, models=[self.t.artist.Artist], limit=1)["artists"]
            if not found or found[0].name.casefold() != artist_name.casefold():
                return []
            return [song_dict(x) for x in found[0].get_top_tracks(limit=count)]

    def artist_info(self, id: str) -> dict:
        with self.lock:
            a = self.session.artist(parse_id(id)[1])
            info = {"biography": "", "similarArtist": []}
            try:
                info["biography"] = _clean(a.get_bio())
            except Exception:
                pass
            try:
                info["similarArtist"] = [artist_dict(x) for x in a.get_similar()]
            except Exception:
                pass
        return info

    def similar_songs(self, id: str, count: int) -> list[dict]:
        with self.lock:
            return [song_dict(x) for x in self.session.track(parse_id(id)[1]).get_track_radio(limit=count)]

    @staticmethod
    def _lyrics_of(track) -> dict | None:
        try:
            ly = track.lyrics()
        except Exception:
            return None
        synced = []
        for line in (getattr(ly, "subtitles", None) or "").splitlines():
            m = _LRC.match(line)
            if m:
                synced.append((int((int(m[1]) * 60 + float(m[2])) * 1000), m[3].strip()))
        text = (getattr(ly, "text", None) or "").strip()
        return {"text": text, "synced": synced} if (text or synced) else None

    def lyrics(self, id: str) -> dict | None:
        with self.lock:
            return self._lyrics_of(self.session.track(parse_id(id)[1]))

    def lyrics_by_name(self, artist: str, title: str) -> dict | None:
        with self.lock:
            hits = self.session.search(f"{artist} {title}", models=[self.t.media.Track], limit=10)["tracks"]
            norm = lambda x: re.sub(r"[\W_]+", "", (x or "").casefold())
            for t in hits:
                if norm(t.name) == norm(title) and norm(t.artist.name) == norm(artist):
                    return self._lyrics_of(t)
        return None

    def album_list(self, kind: str, count: int) -> list[dict]:
        """Tidal's personalised home page: 'new albums' for type=newest, recommendations for type=random."""
        with self.lock:
            cats = [(str(getattr(c, "title", "")).casefold(), [i for i in (getattr(c, "items", None) or [])
                     if type(i).__name__ == "Album"]) for c in self.session.home().categories]
        cats = [(t, a) for t, a in cats if a]
        want = "new albums" if kind == "newest" else "enjoy"
        picked = next((a for t, a in cats if want in t), None)
        if picked is None:
            picked = [x for _, a in cats for x in a]
        picked = list(picked)
        if kind == "random":
            random.shuffle(picked)
        return [album_dict(x) for x in picked[:count]]

    def album_info(self, id: str) -> dict:
        with self.lock:
            try:
                return {"notes": _clean(self.session.album(parse_id(id)[1]).review())}
            except Exception:
                return {"notes": ""}
