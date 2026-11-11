"""Keep the Threads long-lived token alive without touching .env.

Threads tokens last 60 days and can be refreshed (once they're a day old)
for another 60. Before this, refreshing meant running
scripts/refresh_threads_token.py on the box and recreating the containers,
and when nobody did, every Threads post started failing.

The bot now refreshes the token itself (daily check, refresh once fewer
than REFRESH_BELOW_DAYS remain) and stores it in THREADS_TOKEN_FILE
(./credentials/threads-token.json, a bind mount the bot can write). That
file wins over THREADS_ACCESS_TOKEN in .env, so no container needs to be
recreated: main.py runs read it fresh, and long-lived processes (bot,
dashboard) call current() instead of caching the .env value.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

from config import config

log = logging.getLogger(__name__)

REFRESH_URL = "https://graph.threads.net/refresh_access_token"
REFRESH_BELOW_DAYS = 30


def _stored(cfg: Any = None) -> dict[str, Any]:
    cfg = cfg or config
    path = getattr(cfg, "threads_token_file", None)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def current(cfg: Any = None) -> str:
    """The token to use right now: the refreshed one if any, else .env's."""
    cfg = cfg or config
    return _stored(cfg).get("access_token") or cfg.threads_access_token


def days_left(now: float | None = None) -> float | None:
    """Days until the stored token expires; None if no refresh has run yet."""
    expires_at = _stored().get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return None
    return (expires_at - (now if now is not None else time.time())) / 86400


def _save(token: str, expires_at: float | None) -> None:
    path = config.threads_token_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {"access_token": token, "expires_at": expires_at, "refreshed_at": time.time()}
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def refresh() -> float | None:
    """Refresh, verify (same account, valid) and store. Returns days left.

    Raises RuntimeError with a token-free reason; the stored token is left
    untouched unless the new one passed every check.
    """
    token = current()
    if not token or not config.threads_user_id:
        raise RuntimeError("THREADS_USER_ID / THREADS_ACCESS_TOKEN not set")
    base = f"https://graph.threads.net/{config.threads_api_version}"
    try:
        r = requests.get(
            REFRESH_URL,
            params={"grant_type": "th_refresh_token", "access_token": token},
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"refresh refused (HTTP {r.status_code})")
        body = r.json()
        new = body.get("access_token")
        if not new:
            raise RuntimeError("refresh reply had no token")
        me = requests.get(
            f"{base}/me", params={"fields": "id", "access_token": new}, timeout=30
        ).json()
    except (requests.RequestException, ValueError) as exc:
        # Never include exc itself: its URL carries the token.
        raise RuntimeError(f"refresh failed ({type(exc).__name__})") from None
    if str(me.get("id")) != str(config.threads_user_id):
        raise RuntimeError("new token belongs to a different account; not stored")

    expires_in = body.get("expires_in")
    expires_at = time.time() + float(expires_in) if expires_in else None
    _save(new, expires_at)
    left = None if expires_at is None else (expires_at - time.time()) / 86400
    log.info(
        "[threads-token] refreshed (%s)",
        "no expiry given" if left is None else f"{left:.0f} days left",
    )
    return left


def needs_refresh(now: float | None = None) -> bool:
    left = days_left(now)
    return left is None or left < REFRESH_BELOW_DAYS
