"""Self-check: python -m navidrome_tidal_proxy.doctor

Reads the same environment variables as the proxy and prints OK / WARN / FAIL lines with a fix for each problem.
Exit status is 1 if anything failed. It never starts the Tidal login flow and never changes anything.
"""
import os
import shutil
import sys
from pathlib import Path

import httpx

from .config import load_env

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def report(level: str, msg: str, fix: str = "") -> None:
    results.append((level, msg, fix))
    print(f"[{level}] {msg}")
    if fix:
        print(f"       -> {fix}")


def check_settings() -> None:
    env = os.environ.get
    bad = [f"{k}={env(k)!r}" for k, allowed in (("TIDAL_MODE", ("merge", "fallback", "off")),
                                                ("STREAM_MODE", ("redirect", "proxy")),
                                                ("TIDAL_QUALITY", ("low_96k", "low_320k", "high_lossless", "hi_res_lossless")))
           if env(k) and env(k) not in allowed]
    if bad:
        report(FAIL, "Invalid settings: " + ", ".join(bad), "Fix them in your .env / compose file (see README table).")
    else:
        report(OK, f"Settings look valid (mode={env('TIDAL_MODE', 'merge')}, quality={env('TIDAL_QUALITY', 'hi_res_lossless')})")
    if env("SAVE_ON_STAR") == "1" and not env("LIBRARY_DIR"):
        report(FAIL, "SAVE_ON_STAR=1 but LIBRARY_DIR is not set", "Set LIBRARY_DIR to your Navidrome music folder.")
    report(OK, f"Running as uid={os.getuid()} gid={os.getgid()}")


def check_ffmpeg() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        report(FAIL, f"Missing: {', '.join(missing)}", "Install ffmpeg (the Docker image includes it).")
    else:
        report(OK, "ffmpeg found")


def ping_navidrome(url: str) -> tuple[str | None, str]:
    """(version, "") if a Subsonic server answers at url, else (None, reason)."""
    try:
        r = httpx.get(f"{url.rstrip('/')}/rest/ping", params={"u": "doctor", "p": "x", "v": "1.16.1", "c": "doctor", "f": "json"},
                      timeout=8)
        body = r.json()["subsonic-response"]
    except httpx.HTTPError as e:
        return None, f"cannot reach it ({type(e).__name__})"
    except Exception:
        return None, "it answered, but not like a Subsonic server (check the URL has no /rest suffix)"
    return str(body.get("serverVersion", body.get("version", "?"))), ""


def check_navidrome() -> None:
    url = os.environ.get("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
    version, why = ping_navidrome(url)
    if version is None:
        hint = ("In Docker, put this container on Navidrome's network and use its container name; a container often "
                "cannot reach the host's published ports.")
        report(FAIL, f"Navidrome at {url}: {why}", hint)
    else:
        report(OK, f"Navidrome reachable at {url} (server version {version})")


def check_dir(label: str, path: str, need_gb: float | None = None) -> None:
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".doctor-write-test"
        probe.write_text("x")
        probe.unlink()
    except OSError as e:
        report(FAIL, f"{label} {path} is not writable ({e.strerror})",
               "Fix ownership/permissions; in Docker set PUID/PGID to the owner of that folder.")
        return
    free = shutil.disk_usage(p).free / 1e9
    if need_gb and free < need_gb:
        report(WARN, f"{label} {path} has only {free:.1f} GB free (want {need_gb:g}+)",
               "Free some space or lower CACHE_MAX_GB / raise SAVE_MIN_FREE_GB.")
    else:
        report(OK, f"{label} {path} writable ({free:.1f} GB free)")


def check_tidal() -> None:
    sess_path = Path(os.environ.get("TIDAL_SESSION_FILE", "./tidal-session-pkce.json"))
    if not sess_path.exists():
        report(FAIL, f"No Tidal login at {sess_path}", "Run: python -m navidrome_tidal_proxy.login")
        return
    mode = sess_path.stat().st_mode & 0o777
    if mode & 0o077:
        report(WARN, f"{sess_path} is readable by others (mode {mode:o})", f"chmod 600 {sess_path}")
    try:
        import tidalapi
        s = tidalapi.Session()
        s.load_session_from_file(sess_path)
        if not s.check_login():
            raise RuntimeError("login rejected")
    except Exception as e:
        report(FAIL, f"Tidal login is not valid ({e})", "Delete the session file and run the login again.")
        return
    report(OK, f"Tidal login valid (country {getattr(s, 'country_code', '?')}, PKCE={getattr(s, 'is_pkce', '?')})")
    try:
        sub = s.request.request("GET", f"users/{s.user.id}/subscription").json()
        report(OK, f"Subscription: {sub.get('subscription', {}).get('type', '?')}, "
                   f"highest quality {sub.get('highestSoundQuality', '?')}")
    except Exception:
        report(WARN, "Could not read your Tidal subscription")
    want = os.environ.get("TIDAL_QUALITY", "hi_res_lossless")
    try:
        s.audio_quality = getattr(tidalapi.Quality, want)
        track = s.search("love", models=[tidalapi.media.Track], limit=1)["tracks"][0]
        got = str(track.get_stream().audio_quality)
    except Exception as e:
        report(WARN, f"Could not test a stream ({type(e).__name__})")
        return
    if want in ("high_lossless", "hi_res_lossless") and "LOSSLESS" not in got and "HI_RES" not in got:
        report(FAIL, f"Asked for {want} but Tidal returned {got}",
               "Tidal caps the device-code login at AAC. Delete the session file and run the PKCE login again; "
               "also check your plan includes lossless.")
    else:
        report(OK, f"Test stream quality: {got}")


def main() -> int:
    load_env()
    print("navidrome-tidal-proxy doctor\n")
    check_settings()
    check_ffmpeg()
    check_navidrome()
    check_tidal()
    check_dir("Cache folder", os.environ.get("CACHE_DIR", "./cache"), float(os.environ.get("CACHE_MAX_GB", "4")))
    if os.environ.get("LIBRARY_DIR"):
        check_dir("Library folder", os.environ["LIBRARY_DIR"], float(os.environ.get("SAVE_MIN_FREE_GB", "5")))
    fails = sum(1 for lvl, *_ in results if lvl == FAIL)
    warns = sum(1 for lvl, *_ in results if lvl == WARN)
    print(f"\n{len(results) - fails - warns} ok, {warns} warning(s), {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
