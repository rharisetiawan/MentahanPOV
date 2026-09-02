"""TikTok remote worker — runs on the campus server, not the HG680.

The HG680 is a weak ARM board that can't run Playwright/Chromium, so it
hands TikTok jobs to this box instead. See distributors/tiktok_remote.py's
docstring for the full protocol; this is side 2 of it:

    1. The HG680 posts "TIKTOK_JOB <job_id>\\n<video_url>\\n<caption>" into
       the shared "MentahanPOV Jobs" Telegram group using its *main* bot.
    2. This script long-polls that same group using a *separate* worker
       bot token (TIKTOK_WORKER_BOT_TOKEN) — a different token so it never
       fights telegram_bot.py's own poller on the HG680 for the same
       update stream.
    3. On a job, it downloads the video, runs the existing
       distributors/tiktok.py Playwright flow completely unchanged, and
       replies "TIKTOK_DONE <job_id> <url>" or
       "TIKTOK_FAILED <job_id> <error>" into the group.
    4. telegram_bot.py (on the HG680) picks that reply up and writes it to
       a local file that distributors/tiktok_remote.py is polling.

Setup on this box (see DEPLOYMENT.md -> "TikTok remote worker"):
    pip install -r requirements-worker.txt
    playwright install --with-deps chromium
    python -m distributors.tiktok login   # one-time, headed, saves cookies
    python worker_service.py              # long-running

Only needs this repo, a Telegram bot token, and Playwright — none of the
Google/Gemini setup the main pipeline needs, since it only ever calls
distributors/tiktok.py.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

import requests

from config import config
from distributors import tiktok

log = logging.getLogger("mentahanpov.worker")

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_S = 50


def _api(method: str, **params: object) -> dict:
    resp = requests.post(
        f"{API_BASE}/bot{config.tiktok_worker_bot_token}/{method}",
        data=params,
        timeout=POLL_TIMEOUT_S + 20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    return data["result"]


def _reply(text: str) -> None:
    _api("sendMessage", chat_id=config.tiktok_worker_group_chat_id, text=text)


def _download(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def _handle_job(job_id: str, video_url: str, caption: str) -> None:
    log.info("[worker] job %s: downloading %s", job_id, video_url)
    with tempfile.TemporaryDirectory(prefix="tiktok-job-") as tmp:
        video_path = Path(tmp) / f"{job_id}.mp4"
        try:
            _download(video_url, video_path)
            log.info("[worker] job %s: posting to TikTok", job_id)
            result = tiktok.post(video_path, caption)
        except Exception as exc:  # noqa: BLE001 — must always report back
            log.exception("[worker] job %s failed", job_id)
            # Single line only — it rides inside one Telegram message.
            err = f"{type(exc).__name__}: {exc}".replace("\n", " ")
            _reply(f"TIKTOK_FAILED {job_id} {err}")
            return
    _reply(f"TIKTOK_DONE {job_id} {result.get('url', '')}")
    log.info("[worker] job %s done", job_id)


def run() -> None:
    if not config.tiktok_worker_bot_token:
        raise SystemExit("TIKTOK_WORKER_BOT_TOKEN is empty.")
    if not config.tiktok_worker_group_chat_id:
        raise SystemExit("TIKTOK_WORKER_GROUP_CHAT_ID is empty.")

    log.info(
        "[worker] watching group %s for TikTok jobs",
        config.tiktok_worker_group_chat_id,
    )
    offset = None
    while True:
        try:
            updates = _api(
                "getUpdates",
                timeout=POLL_TIMEOUT_S,
                offset=offset,
                allowed_updates='["message"]',
            )
        except Exception:  # noqa: BLE001 — network hiccups shouldn't kill the loop
            log.exception("[worker] getUpdates failed, retrying")
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text") or ""
            if chat_id != config.tiktok_worker_group_chat_id:
                continue
            if not text.startswith("TIKTOK_JOB "):
                continue
            parts = text.split("\n", 2)
            if len(parts) != 3:
                log.warning("[worker] malformed job message: %r", text)
                continue
            job_id = parts[0].removeprefix("TIKTOK_JOB ").strip()
            video_url, caption = parts[1], parts[2]
            _handle_job(job_id, video_url, caption)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    sys.exit(run())
