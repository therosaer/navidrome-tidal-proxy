"""Container entrypoint: fix ownership of the state folder, drop to PUID:PGID, then exec the real command.

If the container is already started as a non-root user (docker `user:`), it simply execs the command.
Running as root is refused unless you explicitly ask for it, so the app itself never runs privileged.
"""
import os
import sys

STATE_DIRS = ("TIDAL_SESSION_FILE", "CACHE_DIR")  # their parent folders hold our writable state


def _chown_tree(path: str, uid: int, gid: int) -> None:
    os.lchown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.lchown(os.path.join(root, name), uid, gid)


def prepare(uid: int, gid: int) -> None:
    for var in STATE_DIRS:
        value = os.environ.get(var)
        if not value:
            continue
        folder = value if var == "CACHE_DIR" else os.path.dirname(value) or "."
        os.makedirs(folder, exist_ok=True)
        st = os.stat(folder)
        if (st.st_uid, st.st_gid) != (uid, gid):
            _chown_tree(folder, uid, gid)


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("usage: entrypoint COMMAND [ARGS...]")
    if os.geteuid() == 0:
        uid, gid = int(os.environ.get("PUID", "1000")), int(os.environ.get("PGID", "1000"))
        if uid == 0 or gid == 0:
            raise SystemExit("Refusing to run as root: set PUID/PGID to your user's ids (for example 1000)")
        prepare(uid, gid)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        os.environ.setdefault("HOME", "/tmp")
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
