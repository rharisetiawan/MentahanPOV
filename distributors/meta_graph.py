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


def describe_http_error(exc: requests.RequestException) -> str:
    """A short, token-free description that keeps Meta's own error message.

    `str(exc)` is unusable twice over: for GETs it embeds the request URL,
    access_token included, and either way it drops the JSON body where
    Meta says what was actually wrong ("media not ready", "invalid token").
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return type(exc).__name__
    detail = f"HTTP {resp.status_code}"
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        return detail
    msg = err.get("error_user_msg") or err.get("message")
    return f"{detail}: {msg}" if msg else detail


def check(resp: requests.Response, what: str) -> None:
    """raise_for_status(), but with describe_http_error()'s message."""
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"{what} failed ({describe_http_error(exc)})") from None


def container_status(status_url: str, token: str, field: str) -> str | None:
    """Best-effort read of a container's status; None if it can't be read."""
    try:
        r = requests.get(
            status_url, params={"fields": field, "access_token": token}, timeout=30
        )
        r.raise_for_status()
        return r.json().get(field)
    except (requests.RequestException, ValueError):
        return None


def publish_container(
    *,
    publish_url: str,
    status_url: str,
    status_field: str,
    creation_id: str,
    token: str,
    label: str,
    attempts: int = 3,
) -> str | None:
    """Publish a FINISHED container without ever publishing it twice.

    Retrying the *whole* post (new container + publish) after a lost reply
    is how a timeout turns into a duplicate Reel/Thread. Here only the
    publish call is retried, always on the same container, and before each
    retry the container is asked whether the last attempt went through.

    Returns the media id, or None when the container says PUBLISHED but
    the reply carrying the id was lost (see find_recent_media()).
    """
    problem = ""
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(
                publish_url,
                data={"creation_id": creation_id, "access_token": token},
                timeout=120,
            )
            r.raise_for_status()
            media_id = r.json().get("id")
            if media_id:
                return str(media_id)
            problem = "reply had no id"
        except requests.RequestException as exc:
            problem = describe_http_error(exc)
        except ValueError:
            problem = "reply was not JSON"
        if container_status(status_url, token, status_field) == "PUBLISHED":
            log.warning(
                "[%s] publish reply lost (%s) but container %s is PUBLISHED; "
                "not publishing again",
                label,
                problem,
                creation_id,
            )
            return None
        log.warning("[%s] publish attempt %d/%d failed: %s", label, attempt, attempts, problem)
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise RuntimeError(f"{label} publish of container {creation_id} failed: {problem}")


def find_recent_media(
    list_url: str, token: str, text_field: str, text: str
) -> tuple[str, str | None] | None:
    """(id, permalink) of the newest post whose text starts like `text`.

    Only used after publish_container() returned None, to recover the id
    insights need. Best effort: None if nothing matches or the call fails.
    """
    want = text.strip()[:80]
    try:
        r = requests.get(
            list_url,
            params={
                "fields": f"id,permalink,{text_field}",
                "limit": 5,
                "access_token": token,
            },
            timeout=30,
        )
        r.raise_for_status()
        items = r.json().get("data") or []
    except (requests.RequestException, ValueError):
        return None
    for item in items:
        if (item.get(text_field) or "").strip()[:80] == want and item.get("id"):
            return str(item["id"]), item.get("permalink")
    return None
