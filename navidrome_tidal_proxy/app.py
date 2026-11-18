import logging
import os

from .backends import TidalApiBackend
from .config import load_env
from .proxy import make_app


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) == "1"


def create_app():
    load_env()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(levelname)s %(name)s: %(message)s")
    mode = os.environ.get("TIDAL_MODE", "merge")
    stream_mode = os.environ.get("STREAM_MODE", "redirect")
    library_dir, save_on_star = os.environ.get("LIBRARY_DIR") or None, _flag("SAVE_ON_STAR")
    if mode not in ("merge", "fallback", "off"):
        raise SystemExit(f"TIDAL_MODE must be merge, fallback or off (got {mode!r})")
    if stream_mode not in ("redirect", "proxy"):
        raise SystemExit(f"STREAM_MODE must be redirect or proxy (got {stream_mode!r})")
    if save_on_star and not library_dir:
        raise SystemExit("SAVE_ON_STAR=1 needs LIBRARY_DIR")
    backend = TidalApiBackend(os.environ.get("TIDAL_SESSION_FILE", "./tidal-session-pkce.json"),
                              os.environ.get("TIDAL_QUALITY", "hi_res_lossless"))
    users = {u.strip() for u in os.environ.get("TIDAL_USERS", "").split(",") if u.strip()}
    return make_app(os.environ.get("NAVIDROME_URL", "http://localhost:4533"), backend,
                    mode=mode, stream_mode=stream_mode,
                    limit=int(os.environ.get("TIDAL_LIMIT", "200")),
                    cache_dir=os.environ.get("CACHE_DIR", "./cache"),
                    cache_max_bytes=int(float(os.environ.get("CACHE_MAX_GB", "4")) * 10**9),
                    badge=os.environ.get("TIDAL_BADGE", "1") != "0",
                    prefetch=os.environ.get("PREFETCH", "1") != "0",
                    library_dir=library_dir, save_on_star=save_on_star,
                    allowed_users=users, save_min_free_bytes=int(float(os.environ.get("SAVE_MIN_FREE_GB", "5")) * 10**9),
                    max_save_tracks=int(os.environ.get("MAX_SAVE_TRACKS", "200")))
