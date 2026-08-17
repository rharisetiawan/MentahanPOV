"""Facebook Page video Stories publisher (Graph API v19+).

Stories don't go through /{page}/videos — they use a separate three-phase
protocol on /{page}/video_stories:

    1. upload_phase=start          -> { video_id, upload_url }
    2. POST the bytes to upload_url (rupload host, OAuth header)
    3. upload_phase=finish         -> published story

The bytes are sent from the local watermarked copy rather than by handing
Facebook a URL, so publishing doesn't depend on Drive staying reachable.
Stories expire after 24h.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config

log = logging.getLogger(__name__)
PLATFORM = "facebook_story"


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{config.graph_api_version}/{path}"


def _ok(resp: requests.Response) -> bool:
    """True unless the body explicitly reports failure.

    The rupload host doesn't always answer with JSON, and a 200 with a
    non-JSON body is a success there — so only an explicit `success: false`
    (or an `error` object) counts as a failure.
    """
    try:
        payload = resp.json()
    except ValueError:
        return True
    if "error" in payload:
        return False
    return payload.get("success", True) is not False


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=4, max=30))
def post(video_path: Path, caption: str, **_: Any) -> dict[str, str]:
    if not config.fb_page_id or not config.fb_page_access_token:
        raise RuntimeError("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN missing.")

    token = config.fb_page_access_token
    endpoint = _graph(f"{config.fb_page_id}/video_stories")

    # Phase 1 — reserve a video id and get the upload host.
    start = requests.post(
        endpoint,
        data={"upload_phase": "start", "access_token": token},
        timeout=60,
    )
    start.raise_for_status()
    started = start.json()
    video_id = started.get("video_id")
    upload_url = started.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError(f"FB story start phase incomplete: {started}")

    # Phase 2 — push the bytes. This host wants the token in an OAuth
    # header and the size up front; it is not a normal Graph endpoint.
    file_size = video_path.stat().st_size
    log.info(
        "[facebook_story] uploading %s (%.1f MB)",
        video_path.name,
        file_size / 1048576,
    )
    # Passing the open handle (not bytes) lets requests set Content-Length
    # from fstat and stream the body — this box has ~1.8 GB of RAM, so a
    # 60s 1080p clip should never be slurped into memory first.
    with video_path.open("rb") as fh:
        transfer = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(file_size),
            },
            data=fh,
            timeout=900,
        )
    transfer.raise_for_status()
    if not _ok(transfer):
        raise RuntimeError(f"FB story byte transfer failed: {transfer.text[:500]}")

    # Phase 3 — publish.
    finish = requests.post(
        endpoint,
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "access_token": token,
        },
        timeout=120,
    )
    finish.raise_for_status()
    if not _ok(finish):
        raise RuntimeError(f"FB story finish phase failed: {finish.text[:500]}")

    return {
        "id": str(video_id),
        "url": f"https://www.facebook.com/{config.fb_page_id}/",
    }
