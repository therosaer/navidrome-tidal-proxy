FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY navidrome_tidal_proxy ./navidrome_tidal_proxy
ENV HOST=0.0.0.0 PORT=4535 TIDAL_SESSION_FILE=/state/tidal-session-pkce.json CACHE_DIR=/state/cache \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PORT'], timeout=3)"
# Starts as root only to fix /state ownership, then drops to PUID:PGID (default 1000:1000) before running the app.
ENTRYPOINT ["python", "-m", "navidrome_tidal_proxy.entrypoint"]
CMD ["python", "-m", "navidrome_tidal_proxy"]
