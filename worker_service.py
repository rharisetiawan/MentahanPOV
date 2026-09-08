"""TikTok remote worker — runs on the campus server, not the HG680.

The HG680 is a weak ARM board that can't run Playwright/Chromium, so it
hands TikTok jobs to this box instead, over plain HTTP on the Tailscale
mesh both boxes join (see DEPLOYMENT.md -> "TikTok remote worker"):

    1. The HG680 (distributors/tiktok_remote.py) POSTs {video_url,
       caption, secret} to this box's /tiktok-job endpoint.
    2. This service downloads the video, runs the existing
       distributors/tiktok.py Playwright flow completely unchanged, and
       responds with {"status": "ok", "url": ...} or
       {"status": "error", "error": ...} — synchronously, in the same
       HTTP request.

(Earlier version long-polled a shared Telegram group using a second bot
token instead. Dropped 2026-09-04: Telegram bots silently never receive
messages sent by *other* bots, confirmed via direct Bot API testing, so
that hand-off never actually worked in either direction — this direct
HTTP-over-Tailscale approach replaces it entirely.)

Setup on this box (see DEPLOYMENT.md -> "TikTok remote worker"):
    pip install -r requirements-worker.txt
    playwright install --with-deps chromium
    python -m distributors.tiktok login   # one-time, headed, saves cookies
    python worker_service.py              # long-running

Only needs this repo, Tailscale, and Playwright — none of the
Google/Gemini setup the main pipeline needs, since it only ever calls
distributors/tiktok.py.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from config import config
from distributors import tiktok

log = logging.getLogger("mentahanpov.worker")


def _download(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.info("[http] %s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        if self.path != "/tiktok-job":
            self._send_json(404, {"status": "error", "error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"status": "error", "error": "invalid JSON body"})
            return

        if config.tiktok_worker_shared_secret and payload.get(
            "secret"
        ) != config.tiktok_worker_shared_secret:
            self._send_json(403, {"status": "error", "error": "bad secret"})
            return

        video_url = payload.get("video_url")
        caption = payload.get("caption", "")
        if not video_url:
            self._send_json(400, {"status": "error", "error": "video_url required"})
            return

        log.info("[worker] job: downloading %s", video_url)
        with tempfile.TemporaryDirectory(prefix="tiktok-job-") as tmp:
            video_path = Path(tmp) / "video.mp4"
            try:
                _download(video_url, video_path)
                log.info("[worker] job: posting to TikTok")
                result = tiktok.post(video_path, caption)
            except Exception as exc:  # noqa: BLE001 — must always report back
                log.exception("[worker] job failed")
                self._send_json(
                    200, {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                )
                return
        log.info("[worker] job done: %s", result.get("url"))
        self._send_json(200, {"status": "ok", "url": result.get("url", "")})


def run() -> None:
    port = config.tiktok_worker_port
    log.info("[worker] listening on 0.0.0.0:%d/tiktok-job", port)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    sys.exit(run())
