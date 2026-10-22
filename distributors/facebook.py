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
import time
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config
from distributors.meta_graph import check, describe_http_error, graph_url

log = logging.getLogger(__name__)
PLATFORM = "facebook"


_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))


# Each phase is retried on its own, never the whole upload: a "finish"
# whose reply got lost may already have published the video, and starting
# over from phase 1 is how that turned into a duplicate post.


@_retry
def _start(endpoint: str, file_size: int, token: str) -> dict[str, Any]:
    start = requests.post(
        endpoint,
        data={"upload_phase": "start", "file_size": file_size, "access_token": token},
        timeout=60,
    )
    check(start, "Facebook upload start")
    started = start.json()
    if not started.get("upload_session_id") or not started.get("video_id"):
        raise RuntimeError(f"Facebook upload start phase incomplete: {started}")
    return started


@_retry
def _transfer(
    endpoint: str, session_id: str, start_offset: int, chunk: bytes, token: str
) -> dict[str, Any]:
    # Re-sending the same chunk at the same offset is harmless.
    resp = requests.post(
        endpoint,
        data={
            "upload_phase": "transfer",
            "upload_session_id": session_id,
            "start_offset": start_offset,
            "access_token": token,
        },
        files={"video_file_chunk": chunk},
        timeout=120,
    )
    check(resp, "Facebook chunk transfer")
    return resp.json()


def _video_exists(video_id: str, token: str) -> bool:
    """Did an earlier "finish" go through even though its reply was lost?"""
    try:
        r = requests.get(
            graph_url(video_id),
            params={"fields": "id,status", "access_token": token},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return False
    status = (data.get("status") or {}).get("video_status")
    return str(data.get("id")) == str(video_id) and status not in ("error", "expired")


def _finish(
    endpoint: str, session_id: str, video_id: str, caption: str, token: str
) -> None:
    problem = ""
    for attempt in range(1, 4):
        try:
            finish = requests.post(
                endpoint,
                data={
                    "upload_phase": "finish",
                    "upload_session_id": session_id,
                    "description": caption,
                    "access_token": token,
                },
                timeout=60,
            )
            finish.raise_for_status()
            finished = finish.json()
            if finished.get("success", True):
                return
            problem = f"reply: {finished}"
        except requests.RequestException as exc:
            problem = describe_http_error(exc)
        except ValueError:
            problem = "reply was not JSON"
        if _video_exists(video_id, token):
            log.warning(
                "[facebook] finish reply lost (%s) but video %s exists; not retrying",
                problem,
                video_id,
            )
            return
        log.warning("[facebook] finish attempt %d/3 failed: %s", attempt, problem)
        if attempt < 3:
            time.sleep(5 * attempt)
    raise RuntimeError(f"Facebook upload finish phase failed: {problem}")


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
    started = _start(videos_endpoint, file_size, token)
    upload_session_id = started["upload_session_id"]
    video_id = started["video_id"]

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
            progress = _transfer(
                transfer_endpoint, upload_session_id, start_offset, chunk, token
            )
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
    _finish(videos_endpoint, upload_session_id, video_id, caption, token)

    return {
        "id": video_id,
        "url": f"https://www.facebook.com/{config.fb_page_id}/videos/{video_id}",
    }
