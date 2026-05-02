"""Facebook Page video uploader (Graph API v19+, file upload).

Uses the simple non-resumable upload path. For files >1 GB, switch to the
resumable upload session (`upload_phase=start|transfer|finish`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config

log = logging.getLogger(__name__)
PLATFORM = "facebook"


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{config.graph_api_version}/{path}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def post(video_path: Path, caption: str, **_: Any) -> dict[str, str]:
    if not config.fb_page_id or not config.fb_page_access_token:
        raise RuntimeError("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN missing.")

    log.info("[facebook] uploading %s to page %s", video_path.name, config.fb_page_id)
    with video_path.open("rb") as fh:
        resp = requests.post(
            _graph(f"{config.fb_page_id}/videos"),
            data={
                "description": caption,
                "access_token": config.fb_page_access_token,
            },
            files={"source": (video_path.name, fh, "video/mp4")},
            timeout=600,
        )
    resp.raise_for_status()
    payload = resp.json()
    video_id = payload.get("id")
    if not video_id:
        raise RuntimeError(f"Facebook upload returned no id: {payload}")
    return {
        "id": video_id,
        "url": f"https://www.facebook.com/{config.fb_page_id}/videos/{video_id}",
    }
