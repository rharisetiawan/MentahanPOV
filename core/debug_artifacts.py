"""Hand a platform's failure screenshot from wherever it was taken to the chat.

The TikTok automation runs on a different machine (campus worker) from the
bot, and used to leave its "what did the page look like when it failed"
screenshot in that machine's /tmp — so diagnosing a failure meant SSHing
there first. Now the screenshot travels back with the error (see
worker_service.py / distributors/tiktok_remote.py), main.py parks it here
keyed by video, and telegram_bot.py sends it into the chat next to the
failure message, then deletes it.

Best-effort throughout: a screenshot is a diagnostic, and failing to move
one must never turn a recoverable posting error into a different one.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from config import config

log = logging.getLogger(__name__)

# Telegram rejects photos above 10 MB; a full-page screenshot of a long
# page can exceed that, and base64 inflates it by a third on the wire too.
MAX_BYTES = 8 * 1024 * 1024


def debug_dir() -> Path:
    return config.state_file.parent / "debug"


def path_for(key: str, platform: str) -> Path:
    return debug_dir() / f"{key}.{platform}.png"


def save_bytes(key: str, platform: str, data: bytes) -> Path | None:
    if not data or len(data) > MAX_BYTES:
        return None
    try:
        dest = path_for(key, platform)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest
    except OSError:
        log.exception("[debug] couldn't save %s screenshot", platform)
        return None


def save_copy(key: str, platform: str, src: Path) -> Path | None:
    try:
        if not src.exists() or src.stat().st_size > MAX_BYTES:
            return None
        dest = path_for(key, platform)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest
    except OSError:
        log.exception("[debug] couldn't copy %s screenshot", platform)
        return None


def take(key: str, platform: str) -> Path | None:
    """The parked screenshot if one exists. The caller deletes it after use."""
    path = path_for(key, platform)
    return path if path.exists() else None


def clear(key: str, platform: str) -> None:
    try:
        path_for(key, platform).unlink(missing_ok=True)
    except OSError:
        log.warning("[debug] couldn't clear old %s screenshot", platform)


def prune(max_age_days: int = 7) -> int:
    """Drop screenshots nobody collected (e.g. the run was started from the
    CLI, not the bot). Returns how many were removed."""
    removed = 0
    cutoff = time.time() - max_age_days * 86400
    try:
        for path in debug_dir().glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
