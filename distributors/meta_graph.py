"""Shared helpers for Meta Graph API calls (Facebook Page + Instagram).

facebook.py, facebook_story.py, instagram.py, and instagram_story.py all
talk to the same Graph API host under the same GRAPH_API_VERSION — this is
the one place that URL-building and the Instagram container-polling logic
(identical for feed Reels and Stories) live, so a Graph API version bump or
a polling fix only needs to happen once.
"""

from __future__ import annotations

import logging
import time

import requests

from config import config

log = logging.getLogger(__name__)


def graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{config.graph_api_version}/{path}"


def wait_container_finished(creation_id: str, *, timeout: int = 600) -> None:
    """Poll an Instagram media container until Graph reports it FINISHED.

    Shared by instagram.py (Reels) and instagram_story.py (Stories) — both
    publish through the same create-container → poll → publish flow.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(
                graph_url(creation_id),
                params={
                    "fields": "status_code,status",
                    "access_token": config.fb_page_access_token,
                },
                timeout=30,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            # `exc`'s message embeds the full request URL, access_token
            # included — never let it propagate as-is (it ends up in logs,
            # state/posts.json, and Telegram chat replies). `from None`
            # also drops it from the traceback chain so log.exception()
            # upstream can't print it either.
            http_status = getattr(exc.response, "status_code", None)
            detail = f"HTTP {http_status}" if http_status is not None else type(exc).__name__
            raise RuntimeError(
                f"IG container {creation_id} check failed ({detail})"
            ) from None
        status = r.json().get("status_code")
        log.info("[instagram] container %s status=%s", creation_id, status)
        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"IG container {creation_id} failed: {r.json()}")
        time.sleep(5)
    raise TimeoutError(f"IG container {creation_id} not FINISHED after {timeout}s")
