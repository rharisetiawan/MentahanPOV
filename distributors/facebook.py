"""Facebook Page video uploader (Graph API v19+, resumable upload).

Uses the resumable upload-session protocol (upload_phase=start|transfer|
finish) rather than a single multipart POST. The simple non-resumable
path silently isn't viable for real phone footage: confirmed 2026-09-04,
a 117.8 MB clip got `413 Request Entity Too Large` — well under the 1 GB
this module used to assume was the cutoff. facebook_story.py already
uses a (differently-shaped) resumable protocol for the same reason; this
mirrors that for regular Page video posts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config
from distributors.meta_graph import graph_url

log = logging.getLogger(__name__)
PLATFORM = "facebook"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def post(video_path: Path, caption: str, **_: Any) -> dict[str, str]:
    if not config.fb_page_id or not config.fb_page_access_token:
        raise RuntimeError("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN missing.")

    token = config.fb_page_access_token
    file_size = video_path.stat().st_size
    videos_endpoint = graph_url(f"{config.fb_page_id}/videos")
    log.info(
        "[facebook] uploading %s (%.1f MB) to page %s",
        video_path.name,
        file_size / 1048576,
        config.fb_page_id,
    )

    # Phase 1 — reserve an upload session and the first chunk's byte range.
    start = requests.post(
        videos_endpoint,
        data={
            "upload_phase": "start",
            "file_size": file_size,
            "access_token": token,
        },
        timeout=60,
    )
    start.raise_for_status()
    started = start.json()
    upload_session_id = started.get("upload_session_id")
    video_id = started.get("video_id")
    if not upload_session_id or not video_id:
        raise RuntimeError(f"Facebook upload start phase incomplete: {started}")

    # Phase 2 — transfer chunks. Facebook's response after each chunk
    # dictates the *next* chunk's exact byte range via start_offset/
    # end_offset — the client doesn't get to pick chunk size.
    transfer_endpoint = graph_url(video_id)
    start_offset = int(started.get("start_offset", 0))
    end_offset = int(started.get("end_offset", 0))
    with video_path.open("rb") as fh:
        while start_offset != end_offset:
            fh.seek(start_offset)
            chunk = fh.read(end_offset - start_offset)
            resp = requests.post(
                transfer_endpoint,
                data={
                    "upload_phase": "transfer",
                    "upload_session_id": upload_session_id,
                    "start_offset": start_offset,
                    "access_token": token,
                },
                files={"video_file_chunk": chunk},
                timeout=120,
            )
            resp.raise_for_status()
            progress = resp.json()
            if "start_offset" not in progress:
                # No offsets back means this transfer covered the whole
                # file in one shot (common for anything under ~a few
                # hundred MB) — nothing left to send.
                break
            new_start = int(progress["start_offset"])
            if new_start <= start_offset:
                raise RuntimeError(
                    f"Facebook upload stalled at offset {start_offset}: {progress}"
                )
            start_offset = new_start
            end_offset = int(progress.get("end_offset", start_offset))

    # Phase 3 — publish.
    finish = requests.post(
        videos_endpoint,
        data={
            "upload_phase": "finish",
            "upload_session_id": upload_session_id,
            "description": caption,
            "access_token": token,
        },
        timeout=60,
    )
    finish.raise_for_status()
    finished = finish.json()
    if not finished.get("success", True):
        raise RuntimeError(f"Facebook upload finish phase failed: {finished}")

    return {
        "id": video_id,
        "url": f"https://www.facebook.com/{config.fb_page_id}/videos/{video_id}",
    }
