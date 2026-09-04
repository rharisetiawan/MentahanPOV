"""TikTok publisher that hands the job to a remote worker over HTTP.

This box (weak ARM, low RAM — see distributors/tiktok.py's docstring)
can't run Playwright/Chromium itself, so posting is delegated to a
separate x86 machine dedicated to it (see DEPLOYMENT.md -> "TikTok remote
worker"). Both boxes join the same Tailscale mesh, so this just makes a
plain HTTP request to worker_service.py's endpoint on the other side —
no port forwarding or public exposure needed, Tailscale handles that.

(Earlier version of this relayed through a shared Telegram group instead,
using two separate bot tokens. Dropped 2026-09-04 after discovering
Telegram bots silently never receive messages sent by *other* bots —
confirmed via direct Bot API testing, not just this project's own code —
which meant that design never actually worked in either direction.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from config import config

PLATFORM = "tiktok"


def post(
    video_path: Path, caption: str, *, post_url: str | None = None, **_: Any
) -> dict[str, str]:
    if not config.tiktok_worker_host:
        raise RuntimeError(
            "TIKTOK_WORKER_HOST is empty. See DEPLOYMENT.md -> "
            "'TikTok remote worker' for setup."
        )
    if not post_url:
        raise RuntimeError(
            "TikTok remote worker needs a public URL for the video "
            "(post_url) — it downloads the bytes itself, same as Instagram."
        )

    resp = requests.post(
        f"http://{config.tiktok_worker_host}:{config.tiktok_worker_port}/tiktok-job",
        json={
            "video_url": post_url,
            "caption": caption,
            "secret": config.tiktok_worker_shared_secret,
        },
        # The worker does the download + Playwright post synchronously
        # before responding, so this timeout has to cover the whole job,
        # not just a network round trip.
        timeout=config.tiktok_remote_timeout_s,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != "ok":
        raise RuntimeError(f"TikTok worker reported failure: {result.get('error')}")
    return {"url": result.get("url", "")}
