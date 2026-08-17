"""Instagram Reels publisher (Graph API v19+).

Flow:
1. POST /{ig_user_id}/media  with media_type=REELS, video_url, caption
   → returns container `creation_id`.
2. Poll /{creation_id}?fields=status_code  until status_code == FINISHED.
3. POST /{ig_user_id}/media_publish  with creation_id  → returns media id.

Requirements:
- Instagram Business or Creator account linked to a Facebook Page.
- `post_url` MUST be publicly reachable over HTTPS and MUST point at the
  watermarked posting copy. Instagram downloads the bytes from that URL
  itself, so it — not the `video_path` argument — decides what actually
  gets published. See core/gdrive.py:direct_download_url for why the
  usercontent/confirm=t form is used rather than a plain Drive link.
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
PLATFORM = "instagram"


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{config.graph_api_version}/{path}"


def _wait_finished(creation_id: str, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            _graph(creation_id),
            params={
                "fields": "status_code,status",
                "access_token": config.fb_page_access_token,
            },
            timeout=30,
        )
        r.raise_for_status()
        status = r.json().get("status_code")
        log.info("[instagram] container %s status=%s", creation_id, status)
        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"IG container {creation_id} failed: {r.json()}")
        time.sleep(5)
    raise TimeoutError(f"IG container {creation_id} not FINISHED after {timeout}s")


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=4, max=30))
def post(
    video_path: Path, caption: str, *, post_url: str | None = None, **_: Any
) -> dict[str, str]:
    if not config.ig_user_id or not config.fb_page_access_token:
        raise RuntimeError("IG_USER_ID / FB_PAGE_ACCESS_TOKEN missing.")
    # `post_url` must point at the WATERMARKED posting copy, not the master.
    # Instagram fetches the bytes from this URL itself, so whatever it points
    # at is what actually gets published — passing the master's Drive link
    # here silently publishes un-watermarked footage.
    if not post_url:
        raise RuntimeError(
            "Instagram requires a public URL for the watermarked posting copy."
        )

    log.info("[instagram] creating container for %s", video_path.name)
    create = requests.post(
        _graph(f"{config.ig_user_id}/media"),
        data={
            "media_type": "REELS",
            "video_url": post_url,
            "caption": caption,
            "access_token": config.fb_page_access_token,
        },
        timeout=120,
    )
    create.raise_for_status()
    creation_id = create.json().get("id")
    if not creation_id:
        raise RuntimeError(f"IG container creation returned no id: {create.json()}")

    _wait_finished(creation_id)

    publish = requests.post(
        _graph(f"{config.ig_user_id}/media_publish"),
        data={"creation_id": creation_id, "access_token": config.fb_page_access_token},
        timeout=120,
    )
    publish.raise_for_status()
    media_id = publish.json().get("id")
    if not media_id:
        raise RuntimeError(f"IG publish returned no id: {publish.json()}")

    # Resolve permalink (best effort).
    perma_url: str | None = None
    try:
        meta = requests.get(
            _graph(media_id),
            params={"fields": "permalink", "access_token": config.fb_page_access_token},
            timeout=30,
        ).json()
        perma_url = meta.get("permalink")
    except requests.RequestException:
        pass

    return {
        "id": media_id,
        "url": perma_url or f"https://www.instagram.com/reel/{media_id}",
    }
