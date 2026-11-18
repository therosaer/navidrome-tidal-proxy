"""Interactive first-run setup: python -m navidrome_tidal_proxy.setup [--skip-login]

Asks a few questions, writes a .env, runs the one-time Tidal login and finishes with the doctor.
Inside Docker settings come from your compose file, so it only does the login and the doctor.
"""
import os
import shlex
import sys
from pathlib import Path

from . import doctor
from .config import load_env

MODES = ("merge", "fallback", "off")
QUALITIES = ("hi_res_lossless", "high_lossless", "low_320k", "low_96k")


def ask(prompt: str, default: str = "", choices: tuple = ()) -> str:
    while True:
        hint = f" [{default}]" if default else ""
        answer = input(f"{prompt}{hint}: ").strip() or default
        if not choices or answer in choices:
            return answer
        print(f"  Please choose one of: {', '.join(choices)}")


def yes(prompt: str, default: bool = False) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = ["# Written by navidrome_tidal_proxy.setup. See .env.example for every option.\n"]
    lines += [f"{k}={shlex.quote(v)}\n" for k, v in values.items() if v != ""]
    path.write_text("".join(lines))
    path.chmod(0o600)


def gather() -> dict[str, str]:
    v: dict[str, str] = {}
    while True:
        url = ask("Navidrome address", os.environ.get("NAVIDROME_URL", "http://localhost:4533"))
        version, why = doctor.ping_navidrome(url)
        if version:
            print(f"  Found Navidrome {version}.")
            break
        print(f"  Could not use {url}: {why}")
        if yes("  Use it anyway?"):
            break
    v["NAVIDROME_URL"] = url
    v["PORT"] = ask("Port for this proxy", os.environ.get("PORT", "4534"))
    v["HOST"] = "0.0.0.0" if yes("Let other devices on your network connect? (otherwise only this computer)") else "127.0.0.1"
    v["TIDAL_MODE"] = ask("Tidal mode (merge = add Tidal to every search, fallback = only when nothing local, off)",
                          os.environ.get("TIDAL_MODE", "merge"), MODES)
    v["TIDAL_QUALITY"] = ask("Tidal quality", os.environ.get("TIDAL_QUALITY", "hi_res_lossless"), QUALITIES)
    if yes("Save starred Tidal tracks into your music folder? (downloading may violate Tidal's terms)"):
        while True:
            lib = ask("  Navidrome music folder (full path)")
            if lib and Path(lib).is_dir():
                v["LIBRARY_DIR"], v["SAVE_ON_STAR"] = lib, "1"
                break
            print("  That folder does not exist.")
    users = ask("Only these Navidrome users may use Tidal (comma-separated, empty = everyone with a login)")
    if users:
        v["TIDAL_USERS"] = users
    return v


def main(argv: list[str]) -> int:
    load_env()
    skip_login = "--skip-login" in argv
    print("navidrome-tidal-proxy setup\n")
    if Path("/.dockerenv").exists():
        print("Running in Docker: settings come from your compose file, so only the Tidal login and a check follow.\n")
    else:
        env_path = Path(".env")
        if env_path.exists() and not yes(f"{env_path} already exists. Overwrite it?"):
            print("Keeping the existing .env.\n")
        else:
            values = gather()
            write_env(env_path, values)
            os.environ.update(values)
            print(f"\nWrote {env_path.resolve()} (owner-only).\n")
    session = Path(os.environ.get("TIDAL_SESSION_FILE", "./tidal-session-pkce.json"))
    if skip_login:
        print("Skipping the Tidal login (--skip-login).\n")
    elif session.exists() and yes(f"A Tidal login already exists at {session}. Keep it?", True):
        print("Keeping it.\n")
    else:
        print("Tidal login: open the link below, sign in, then paste the page address you end up on.\n")
        from .backends import TidalApiBackend
        TidalApiBackend(str(session))
        print("Logged in.\n")
    print("Checking everything...\n")
    code = doctor.main()
    if code == 0:
        print("\nAll set. Start the proxy with:  python -m navidrome_tidal_proxy")
        print("Then point your Subsonic client at the proxy with your normal Navidrome login.")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
