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
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config
from distributors.meta_graph import (
    check,
    find_recent_media,
    graph_url,
    publish_container,
    wait_container_finished,
)

log = logging.getLogger(__name__)
PLATFORM = "instagram"


# Creating a container is safe to repeat (an unpublished one just expires);
# publishing is not, so it is retried separately in publish_container().
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
def _create_container(post_url: str, caption: str) -> str:
    create = requests.post(
        graph_url(f"{config.ig_user_id}/media"),
        data={
            "media_type": "REELS",
            "video_url": post_url,
            "caption": caption,
            "access_token": config.fb_page_access_token,
        },
        timeout=120,
    )
    check(create, "IG container creation")
    creation_id = create.json().get("id")
    if not creation_id:
        raise RuntimeError(f"IG container creation returned no id: {create.json()}")
    return creation_id


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
    creation_id = _create_container(post_url, caption)
    wait_container_finished(creation_id)

    token = config.fb_page_access_token
    media_id = publish_container(
        publish_url=graph_url(f"{config.ig_user_id}/media_publish"),
        status_url=graph_url(creation_id),
        status_field="status_code",
        creation_id=creation_id,
        token=token,
        label="instagram",
    )
    if media_id is None:
        found = find_recent_media(
            graph_url(f"{config.ig_user_id}/media"), token, "caption", caption
        )
        if found is None:
            # It IS live; only the id (for insights) is unknown.
            return {"id": "", "url": f"https://www.instagram.com/{config.ig_user_id}"}
        media_id, permalink = found
        if permalink:
            return {"id": media_id, "url": permalink}

    # Resolve permalink (best effort).
    perma_url: str | None = None
    try:
        meta = requests.get(
            graph_url(media_id),
            params={"fields": "permalink", "access_token": token},
            timeout=30,
        ).json()
        perma_url = meta.get("permalink")
    except (requests.RequestException, ValueError):
        pass

    return {
        "id": media_id,
        "url": perma_url or f"https://www.instagram.com/reel/{media_id}",
    }
