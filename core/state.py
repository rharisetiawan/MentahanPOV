"""Idempotent post state — JSON file keyed by SHA1 of the source video path.

Each entry tracks per-platform status so reruns skip what already succeeded
and only retry the platforms that previously failed.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def video_key(video_path: Path) -> str:
    return hashlib.sha1(str(video_path.resolve()).encode("utf-8")).hexdigest()[:12]


def _load(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(state_file: Path, data: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_file)


def get(state_file: Path, video_path: Path) -> dict[str, Any]:
    with _LOCK:
        data = _load(state_file)
        return data.get(video_key(video_path), {})


def update(state_file: Path, video_path: Path, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the entry for this video and persist."""
    with _LOCK:
        data = _load(state_file)
        key = video_key(video_path)
        entry = data.get(key, {"video_path": str(video_path), "platforms": {}})
        entry.setdefault("platforms", {})
        for k, v in patch.items():
            if k == "platforms" and isinstance(v, dict):
                entry["platforms"].update(v)
            else:
                entry[k] = v
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        data[key] = entry
        _save(state_file, data)
        return entry


def mark_platform(
    state_file: Path,
    video_path: Path,
    platform: str,
    *,
    status: str,
    url: str | None = None,
    error: str | None = None,
) -> None:
    update(
        state_file,
        video_path,
        {
            "platforms": {
                platform: {
                    "status": status,
                    "url": url,
                    "error": error,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            }
        },
    )


def already_succeeded(state_file: Path, video_path: Path, platform: str) -> bool:
    entry = get(state_file, video_path)
    return entry.get("platforms", {}).get(platform, {}).get("status") == "ok"


def video_path_for_key(state_file: Path, key: str) -> Path | None:
    """Reverse lookup: state key -> the video path it was recorded under.

    Used by telegram_bot.py's confirm/edit/cancel flow — a button's
    callback_data only has room for the short key, not a full path, so a
    tap has to look the real path back up here rather than carrying it.
    """
    with _LOCK:
        data = _load(state_file)
    entry = data.get(key)
    if not entry or "video_path" not in entry:
        return None
    return Path(entry["video_path"])
