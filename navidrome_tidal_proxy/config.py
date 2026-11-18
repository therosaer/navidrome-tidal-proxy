"""Tiny .env loader so you don't have to `source` it. Real environment variables always win."""
import os
import shlex
from pathlib import Path


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            parts = shlex.split(value, comments=True)
            value = parts[0] if parts else ""
        except ValueError:
            value = value.strip()
        os.environ.setdefault(key.strip(), value)
