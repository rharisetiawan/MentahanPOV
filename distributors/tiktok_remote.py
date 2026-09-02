"""TikTok publisher that hands the job to a remote worker over Telegram.

This box (weak ARM, low RAM — see distributors/tiktok.py's docstring)
can't run Playwright/Chromium itself, so posting is delegated to a
separate x86 machine dedicated to it. The two boxes are never on the same
network and neither exposes a port to the internet, so the handoff rides
on a channel both can already reach: the Telegram Bot API.

Protocol, all inside one Telegram group ("MentahanPOV Jobs" — see
DEPLOYMENT.md -> "TikTok remote worker" for how it's set up) that both
the main bot and a dedicated worker bot belong to, with the worker bot's
Group Privacy turned OFF so it receives every message in the group, not
just ones addressed to it:

1. This module posts "TIKTOK_JOB <job_id>\\n<video_url>\\n<caption>" to
   the group using the *main* bot's token (a plain sendMessage call —
   never polls that token, since telegram_bot.py's own long-running
   Application already owns its update stream and a second poller would
   just steal its updates).
2. worker_service.py, running on the remote box, is the one actually
   polling the group via the *worker* bot's token. It downloads the
   video, runs the same distributors/tiktok.py Playwright flow locally,
   and replies "TIKTOK_DONE <job_id> <url>" or
   "TIKTOK_FAILED <job_id> <error>" into the group via its own token.
3. telegram_bot.py has a handler (handle_tiktok_job_result) watching that
   same group for those reply patterns, which writes the result to
   state/tiktok_jobs/<job_id>.json the moment it arrives.
4. This module polls that local JSON file — not Telegram at all — until
   it appears or TIKTOK_REMOTE_TIMEOUT_S elapses. Two processes on the
   same machine watching a file avoids needing a second Telegram poller
   on either token.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from config import config

log = logging.getLogger(__name__)
PLATFORM = "tiktok"

JOBS_DIR = Path(__file__).parent.parent / "state" / "tiktok_jobs"


def post(
    video_path: Path, caption: str, *, post_url: str | None = None, **_: Any
) -> dict[str, str]:
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty — can't reach the worker.")
    if not config.tiktok_worker_group_chat_id:
        raise RuntimeError(
            "TIKTOK_WORKER_GROUP_CHAT_ID is empty. See DEPLOYMENT.md -> "
            "'TikTok remote worker' for setup."
        )
    if not post_url:
        raise RuntimeError(
            "TikTok remote worker needs a public URL for the video "
            "(post_url) — it downloads the bytes itself, same as Instagram."
        )

    job_id = uuid.uuid4().hex[:12]
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = JOBS_DIR / f"{job_id}.json"

    log.info("[tiktok_remote] handing off job %s to worker", job_id)
    resp = requests.post(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
        data={
            "chat_id": config.tiktok_worker_group_chat_id,
            "text": f"TIKTOK_JOB {job_id}\n{post_url}\n{caption}",
        },
        timeout=30,
    )
    resp.raise_for_status()
    if not resp.json().get("ok"):
        raise RuntimeError(f"Failed to hand off TikTok job: {resp.json()}")

    deadline = time.time() + config.tiktok_remote_timeout_s
    while time.time() < deadline:
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result_path.unlink(missing_ok=True)
            if result.get("status") == "ok":
                return {"id": job_id, "url": result.get("url", "")}
            raise RuntimeError(f"TikTok worker reported failure: {result.get('error')}")
        time.sleep(5)

    raise TimeoutError(
        f"TikTok worker didn't reply to job {job_id} within "
        f"{config.tiktok_remote_timeout_s}s — it may still complete later; "
        "check the 'MentahanPOV Jobs' Telegram group."
    )
