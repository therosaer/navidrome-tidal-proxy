import os

import uvicorn

from .config import load_env

load_env()

uvicorn.run("navidrome_tidal_proxy.app:create_app", factory=True,
            host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "4534")))
