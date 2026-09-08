"""Instagram Stories publisher (Graph API v19+).

Same two-step container flow as Reels, but with `media_type=STORIES` and
no caption — stories don't carry one. Stories expire after 24h, so this is
a reach/traffic booster on top of the permanent Reel, not a replacement.

Instagram fetches the bytes from `post_url` itself, so that URL must point
at the watermarked posting copy (see distributors/instagram.py).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config
from distributors.meta_graph import graph_url, wait_container_finished

log = logging.getLogger(__name__)
PLATFORM = "instagram_story"


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=4, max=30))
def post(
    video_path: Path, caption: str, *, story_url: str | None = None, **_: Any
) -> dict[str, str]:
    if not config.ig_user_id or not config.fb_page_access_token:
        raise RuntimeError("IG_USER_ID / FB_PAGE_ACCESS_TOKEN missing.")
    if not story_url:
        raise RuntimeError("Instagram stories need a public URL for the story copy.")

    log.info("[instagram_story] creating story container for %s", video_path.name)
    create = requests.post(
        graph_url(f"{config.ig_user_id}/media"),
        data={
            "media_type": "STORIES",
            "video_url": story_url,
            "access_token": config.fb_page_access_token,
        },
        timeout=120,
    )
    create.raise_for_status()
    creation_id = create.json().get("id")
    if not creation_id:
        raise RuntimeError(f"IG story container returned no id: {create.json()}")

    wait_container_finished(creation_id)

    publish = requests.post(
        graph_url(f"{config.ig_user_id}/media_publish"),
        data={"creation_id": creation_id, "access_token": config.fb_page_access_token},
        timeout=120,
    )
    publish.raise_for_status()
    media_id = publish.json().get("id")
    if not media_id:
        raise RuntimeError(f"IG story publish returned no id: {publish.json()}")

    return {
        "id": media_id,
        "url": f"https://www.instagram.com/stories/{config.ig_username or 'me'}/",
    }
