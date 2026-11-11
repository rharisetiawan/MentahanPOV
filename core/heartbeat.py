"""Liveness signal for the bot, so "the box was dead for 19 days" can't go unnoticed.

core/health.py and the daily Telegram report run *on the box*, so they
can't report the box's own death. This gives three independent ways to
notice instead:

  - the bot rewrites state/heartbeat.json every few minutes, and on startup
    reads the previous value back to tell you how long it was down;
  - dashboard.py serves that file's age at /heartbeat, which
    scripts/watchdog.py (run from a *different* machine) polls;
  - optionally the bot also pings HEARTBEAT_PING_URL each beat — a
    healthchecks.io-style dead-man's switch alerts when pings stop, which
    is the one signal that still works when the whole network path is gone.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import config

log = logging.getLogger(__name__)


def heartbeat_file() -> Path:
    return config.state_file.parent / "heartbeat.json"


def beat(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    path = heartbeat_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": now.isoformat()}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.exception("[heartbeat] couldn't write %s", path)


def last_beat() -> datetime | None:
    try:
        raw = json.loads(heartbeat_file().read_text(encoding="utf-8"))["ts"]
        ts = datetime.fromisoformat(raw)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def age_seconds(now: datetime | None = None) -> float | None:
    ts = last_beat()
    if ts is None:
        return None
    return ((now or datetime.now(timezone.utc)) - ts).total_seconds()


def ping(url: str | None = None) -> bool:
    """GET the dead-man's-switch URL. Never raises — a failed ping must not
    take down the bot's own job loop."""
    url = url or config.heartbeat_ping_url
    if not url:
        return False
    try:
        requests.get(url, timeout=10).raise_for_status()
        return True
    except requests.RequestException as exc:
        # The URL is itself the secret for hosted check services (it holds
        # the check's uuid), so — like the Graph API call sites — never put
        # the exception text (which embeds it) into a log line.
        log.warning("[heartbeat] ping failed (%s)", type(exc).__name__)
        return False


def humanize(seconds: float) -> str:
    """'3 hari 4 jam', '2 jam 10 menit', '45 menit', 'kurang dari semenit'."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} hari {hours} jam" if hours else f"{days} hari"
    if hours:
        return f"{hours} jam {minutes} menit" if minutes else f"{hours} jam"
    if minutes:
        return f"{minutes} menit"
    return "kurang dari semenit"
