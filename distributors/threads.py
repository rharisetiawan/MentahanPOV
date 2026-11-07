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
from core import threads_token
from distributors.meta_graph import check, find_recent_media, publish_container

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
        try:
            r = requests.get(
                _graph(creation_id),
                params={
                    "fields": "status,error_message",
                    "access_token": threads_token.current(),
                },
                timeout=30,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            # See distributors/meta_graph.py's wait_container_finished for
            # why this can't just re-raise `exc` (URL embeds access_token).
            http_status = getattr(exc.response, "status_code", None)
            detail = f"HTTP {http_status}" if http_status is not None else type(exc).__name__
            raise RuntimeError(
                f"Threads container {creation_id} check failed ({detail})"
            ) from None
        payload = r.json()
        status = payload.get("status")
        log.info("[threads] container %s status=%s", creation_id, status)
        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Threads container {creation_id} failed: {payload}")
        time.sleep(5)
    raise TimeoutError(f"Threads container {creation_id} not FINISHED after {timeout}s")


# Creating a container is safe to repeat (an unpublished one just expires);
# publishing is not, so it is retried separately in publish_container().
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
def _create_container(post_url: str, text: str) -> str:
    create = requests.post(
        _graph(f"{config.threads_user_id}/threads"),
        data={
            "media_type": "VIDEO",
            "video_url": post_url,
            "text": text,
            "access_token": threads_token.current(),
        },
        timeout=120,
    )
    check(create, "Threads container creation")
    creation_id = create.json().get("id")
    if not creation_id:
        raise RuntimeError(f"Threads container creation returned no id: {create.json()}")
    return creation_id


def post(
    video_path: Path, caption: str, *, post_url: str | None = None, **_: Any
) -> dict[str, str]:
    if not config.threads_user_id or not threads_token.current():
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
    creation_id = _create_container(post_url, text)
    _wait_finished(creation_id)

    token = threads_token.current()
    media_id = publish_container(
        publish_url=_graph(f"{config.threads_user_id}/threads_publish"),
        status_url=_graph(creation_id),
        status_field="status",
        creation_id=creation_id,
        token=token,
        label="threads",
    )
    if media_id is None:
        found = find_recent_media(
            _graph(f"{config.threads_user_id}/threads"), token, "text", text
        )
        if found is None:
            # It IS live; only the id (for insights) is unknown.
            return {"id": "", "url": "https://www.threads.net/"}
        media_id, permalink = found
        if permalink:
            return {"id": media_id, "url": permalink}

    # Resolve permalink (best effort).
    perma_url: str | None = None
    try:
        meta = requests.get(
            _graph(media_id),
            params={"fields": "permalink", "access_token": token},
            timeout=30,
        ).json()
        perma_url = meta.get("permalink")
    except (requests.RequestException, ValueError):
        pass

    return {
        "id": media_id,
        "url": perma_url or f"https://www.threads.net/t/{media_id}",
    }
