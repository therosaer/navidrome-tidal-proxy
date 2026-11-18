"""Copy a Tidal track into the local Navidrome music folder (opt-in; see README for the ToS caveat)."""
import asyncio
import re
from pathlib import Path

import httpx

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def safe_name(s: str) -> str:
    return _BAD.sub("_", s or "").strip(" .")[:120] or "_"


async def save_track(backend, tid: str, src: Path, library: Path) -> Path:
    """Tag `src` (a FLAC/M4A file) and place it at <library>/Tidal/<Artist>/<Album>/NN - Title.ext."""
    song = await asyncio.to_thread(backend.get_song, tid)
    root = library.resolve()
    folder = (root / "Tidal" / safe_name(song["artist"]) / safe_name(song["album"])).resolve()
    folder.relative_to(root)  # refuse anything that would land outside the library
    ext = src.suffix or ".flac"
    dest = folder / f"{int(song.get('track') or 0):02d} - {safe_name(song['title'])}{ext}"
    if dest.exists():
        return dest
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{dest.stem}.part{ext}"
    tags = {"title": song["title"], "artist": song["artist"], "album_artist": song["artist"],
            "album": song["album"], "track": song.get("track"), "date": song.get("year")}
    meta = [a for k, v in tags.items() if v not in (None, "") for a in ("-metadata", f"{k}={v}")]
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:a", "-c", "copy", "-map_metadata", "-1",
        *meta, str(tmp), stdin=asyncio.subprocess.DEVNULL)
    try:
        if await proc.wait() != 0:
            raise RuntimeError("ffmpeg failed while tagging the saved track")
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)
    cover = folder / "cover.jpg"
    if not cover.exists():
        try:
            url = await asyncio.to_thread(backend.cover_url, song["albumId"], 1280)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code == 200:
                cover.write_bytes(r.content)
        except Exception:
            pass  # cover is best-effort
    return dest
