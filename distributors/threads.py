"""Threads publisher (Threads API).

Threads is a separate Meta product from the Facebook/Instagram Graph API
used by the other distributors here: different host (graph.threads.net,
not graph.facebook.com), different OAuth flow, different access token.
FB_PAGE_ACCESS_TOKEN / IG_USER_ID do NOT work here — see README → "Threads"
for how to get THREADS_USER_ID / THREADS_ACCESS_TOKEN.

Flow (same two-step container shape as Instagram, different host):
1. POST /{threads_user_id}/threads  with media_type=VIDEO, video_url, text
   → returns container `creation_id`.
2. Poll /{creation_id}?fields=status  until status == FINISHED.
3. POST /{threads_user_id}/threads_publish  with creation_id  → returns id.

Requirements:
- A Threads **Professional** (Business or Creator) account — same
  restriction as Instagram; a personal Threads profile has no publishing API.
- `post_url` MUST be publicly reachable over HTTPS and MUST point at the
  watermarked posting copy — Threads fetches the bytes itself, same as
  Instagram (see distributors/instagram.py's docstring for why the master
  is never handed over directly).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config

log = logging.getLogger(__name__)
PLATFORM = "threads"

# The API has historically capped a single text post at 500 characters
# (independent of the longer limit the Threads app itself allows) —
# truncate defensively rather than let a run fail entirely over caption
# length, same approach as distributors/youtube.py's title/description cap.
TEXT_MAX_CHARS = 500


def _graph(path: str) -> str:
    return f"https://graph.threads.net/{config.threads_api_version}/{path}"


def _wait_finished(creation_id: str, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            _graph(creation_id),
            params={
                "fields": "status,error_message",
                "access_token": config.threads_access_token,
            },
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        status = payload.get("status")
        log.info("[threads] container %s status=%s", creation_id, status)
        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Threads container {creation_id} failed: {payload}")
        time.sleep(5)
    raise TimeoutError(f"Threads container {creation_id} not FINISHED after {timeout}s")


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=4, max=30))
def post(
    video_path: Path, caption: str, *, post_url: str | None = None, **_: Any
) -> dict[str, str]:
    if not config.threads_user_id or not config.threads_access_token:
        raise RuntimeError(
            "THREADS_USER_ID / THREADS_ACCESS_TOKEN missing. See README → 'Threads'."
        )
    # `post_url` must point at the WATERMARKED posting copy, not the master —
    # same reasoning as distributors/instagram.py: Threads fetches the bytes
    # from this URL itself, so whatever it points at is what gets published.
    if not post_url:
        raise RuntimeError(
            "Threads requires a public URL for the watermarked posting copy."
        )

    text = caption.strip()
    if len(text) > TEXT_MAX_CHARS:
        text = text[: TEXT_MAX_CHARS - 1].rstrip() + "…"

    log.info("[threads] creating container for %s", video_path.name)
    create = requests.post(
        _graph(f"{config.threads_user_id}/threads"),
        data={
            "media_type": "VIDEO",
            "video_url": post_url,
            "text": text,
            "access_token": config.threads_access_token,
        },
        timeout=120,
    )
    create.raise_for_status()
    creation_id = create.json().get("id")
    if not creation_id:
        raise RuntimeError(f"Threads container creation returned no id: {create.json()}")

    _wait_finished(creation_id)

    publish = requests.post(
        _graph(f"{config.threads_user_id}/threads_publish"),
        data={
            "creation_id": creation_id,
            "access_token": config.threads_access_token,
        },
        timeout=120,
    )
    publish.raise_for_status()
    media_id = publish.json().get("id")
    if not media_id:
        raise RuntimeError(f"Threads publish returned no id: {publish.json()}")

    # Resolve permalink (best effort).
    perma_url: str | None = None
    try:
        meta = requests.get(
            _graph(media_id),
            params={
                "fields": "permalink",
                "access_token": config.threads_access_token,
            },
            timeout=30,
        ).json()
        perma_url = meta.get("permalink")
    except requests.RequestException:
        pass

    return {
        "id": media_id,
        "url": perma_url or f"https://www.threads.net/t/{media_id}",
    }
