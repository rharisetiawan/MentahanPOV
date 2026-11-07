"""Suggest which part of a 3-5 minute live performance to cut into a reel.

A person still decides: the suggestion only saves scrubbing through every
recording. It ranks windows by short-term loudness (EBU R128 "S", 3 s
window), because in a live singing contest the loud sustained stretch is
almost always the chorus / climax. Applause is loud too, so the first and
last seconds of the recording (MC talk, walk-on, cheering) are skipped.
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ametadata print output, a pair of lines per audio frame:
#   frame:12   pts:57600   pts_time:1.2
#   lavfi.r128.S=-17.93
_FRAME_RE = re.compile(r"pts_time:\s*([\d.]+)\s*\n\s*lavfi\.r128\.S=(-?[\d.]+|-?inf|nan)")
SILENCE_LUFS = -70.0


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    loudness: float  # mean short-term loudness over the window, LUFS

    def as_toml(self) -> str:
        return f"[{self.start:g}, {self.end:g}]"


def parse_ebur128(text: str) -> list[float]:
    """Short-term loudness per whole second, from ebur128 + ametadata print."""
    per_second: dict[int, list[float]] = {}
    for match in _FRAME_RE.finditer(text):
        t = float(match.group(1))
        raw = match.group(2)
        value = SILENCE_LUFS if raw.endswith(("inf", "nan")) else max(SILENCE_LUFS, float(raw))
        per_second.setdefault(int(t), []).append(value)
    if not per_second:
        return []
    last = max(per_second)
    return [_mean_lufs(per_second.get(i, [SILENCE_LUFS])) for i in range(last + 1)]


def _mean_lufs(values: list[float]) -> float:
    """Average loudness in the energy domain (averaging dB directly understates peaks)."""
    energy = sum(10 ** (v / 10) for v in values) / len(values)
    return 10 * math.log10(energy) if energy > 0 else SILENCE_LUFS


def measure(video: Path) -> list[float]:
    """Run ffmpeg's ebur128 filter over the audio track (video is ignored, so it's quick)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-i", str(video), "-vn",
         "-af", "ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.S:file=-",
         "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg ebur128 gagal untuk {video}: {proc.stderr[-400:]}")
    return parse_ebur128(proc.stdout)


def best_windows(
    loudness: list[float],
    *,
    length: float = 30.0,
    count: int = 3,
    skip_head: float = 15.0,
    skip_tail: float = 10.0,
) -> list[Window]:
    """The `count` loudest non-overlapping `length`-second windows, loudest first."""
    n = int(round(length))
    first = int(skip_head)
    last_start = len(loudness) - int(skip_tail) - n
    if n <= 0 or last_start < first:
        # Recording too short for the margins: fall back to the whole range.
        first, last_start = 0, max(0, len(loudness) - n)
    if not loudness:
        return []
    energy = [10 ** (v / 10) for v in loudness]
    candidates = []
    for start in range(first, last_start + 1):
        chunk = energy[start : start + n]
        mean = sum(chunk) / len(chunk)
        candidates.append((10 * math.log10(mean) if mean > 0 else SILENCE_LUFS, start))
    candidates.sort(reverse=True)
    picked: list[Window] = []
    for value, start in candidates:
        end = start + n
        if all(end <= w.start or start >= w.end for w in picked):
            picked.append(Window(float(start), float(min(end, len(loudness))), round(value, 1)))
        if len(picked) == count:
            break
    return picked
