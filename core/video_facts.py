"""Ground-truth facts pulled straight from the video file via ffprobe.

Phones (this project's source: Xiaomi/Android) embed an ISO-6709 location
tag (e.g. "-07.9908+112.6216/") in the container's format tags whenever GPS
was on during recording. We read that directly instead of asking Gemini to
guess it from a screenshot.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_ISO6709 = re.compile(r"^([+-]\d+\.?\d*)([+-]\d+\.?\d*)")


@dataclass
class VideoFacts:
    date: str  # YYYYMMDD
    duration_s: int
    resolution: str  # "480p" / "720p" / "1080p" / "1440p" / "2160p"
    gps: tuple[float, float] | None


def _ffprobe_json(video_path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffprobe not found on PATH. Install ffmpeg (brew install ffmpeg / apt install ffmpeg)."
        ) from exc
    return json.loads(out.stdout)


def _resolution_label(width: int, height: int) -> str:
    long_edge = max(width, height)
    if long_edge >= 3840:
        return "2160p"
    if long_edge >= 2560:
        return "1440p"
    if long_edge >= 1920:
        return "1080p"
    if long_edge >= 1280:
        return "720p"
    return "480p"


def _parse_gps(tags: dict) -> tuple[float, float] | None:
    loc = tags.get("location") or tags.get("location-eng")
    if not loc:
        return None
    m = _ISO6709.match(loc.strip())
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def extract_facts(video_path: Path) -> VideoFacts:
    info = _ffprobe_json(video_path)
    fmt = info.get("format", {})
    tags = fmt.get("tags", {})

    duration_s = round(float(fmt.get("duration", 0)))

    video_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    resolution = _resolution_label(width, height)

    creation_time = tags.get("creation_time")
    if creation_time:
        try:
            dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    date = dt.strftime("%Y%m%d")

    gps = _parse_gps(tags)
    log.info(
        "[facts] date=%s duration=%ss resolution=%s gps=%s",
        date,
        duration_s,
        resolution,
        gps,
    )
    return VideoFacts(date=date, duration_s=duration_s, resolution=resolution, gps=gps)
