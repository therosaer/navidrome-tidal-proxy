"""One-time Tidal login: python -m navidrome_tidal_proxy.login"""
import os

from .backends import TidalApiBackend
from .config import load_env

load_env()
TidalApiBackend(os.environ.get("TIDAL_SESSION_FILE", "./tidal-session-pkce.json"))
print("Logged in; session saved.")
