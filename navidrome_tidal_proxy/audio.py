"""Tidal lossless streams arrive as many DASH segments; stitch them into one seekable FLAC file."""
import asyncio
from pathlib import Path

import httpx


async def build_flac(urls: list[str], dest: Path, concurrency: int = 8) -> None:
    """Download segments (a few at a time, written in order) and remux them to a FLAC file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw, tmp = dest.with_suffix(".m4a.part"), dest.with_suffix(".flac.part")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            async def get(u: str) -> bytes:
                r = await c.get(u)
                r.raise_for_status()
                return r.content
            with raw.open("wb") as f:
                for i in range(0, len(urls), concurrency):  # bounded memory: one batch at a time
                    for part in await asyncio.gather(*(get(u) for u in urls[i:i + concurrency])):
                        f.write(part)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c", "copy", "-f", "flac", str(tmp),
            stdin=asyncio.subprocess.DEVNULL)
        if await proc.wait() != 0:
            raise RuntimeError("ffmpeg could not remux the Tidal segments")
        tmp.rename(dest)
    finally:
        raw.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


def prune(cache_dir: Path, max_bytes: int) -> None:
    """Delete least-recently-used cached tracks until the cache fits; the newest file always stays."""
    files = sorted((p for p in cache_dir.glob("*.flac")), key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for i, f in enumerate(files):
        try:
            total += f.stat().st_size
            if i > 0 and total > max_bytes:
                f.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
