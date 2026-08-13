"""Render a watermarked, feed-sized "posting copy" of a raw master video.

The untouched master goes to Drive; this copy (capped at 1080p long edge,
re-encoded, thin logo in the corner) is what actually gets posted to
social platforms.

The watermark is always applied via ffmpeg's `overlay` filter on a PNG —
never `drawtext`, since minimal ffmpeg builds (this project's included)
are often compiled without freetype/fontconfig and drawtext silently
isn't available. If no logo exists yet at WATERMARK_LOGO_PATH, one is
generated once with Pillow as a placeholder; drop a real logo PNG at that
same path any time to replace it, no code changes needed.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from config import config

log = logging.getLogger(__name__)

_PLACEHOLDER_TEXT = "MentahanPOV"


def _load_font(size: int):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _generate_placeholder_logo(path: Path) -> None:
    from PIL import Image, ImageDraw

    log.warning(
        "[watermark] no logo at %s; generating a placeholder — replace this "
        "file with your real logo PNG whenever it's ready, no code changes needed",
        path,
    )
    width, height = 640, 160
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(72)
    bbox = draw.textbbox((0, 0), _PLACEHOLDER_TEXT, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((width - tw) / 2 - bbox[0], (height - th) / 2 - bbox[1]),
        _PLACEHOLDER_TEXT,
        font=font,
        fill=(255, 255, 255, 255),  # fully opaque here; ffmpeg controls "tipis" opacity
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _ensure_logo() -> Path:
    logo_path = config.watermark_logo_path
    if not logo_path.exists():
        _generate_placeholder_logo(logo_path)
    return logo_path


def make_posting_copy(video_path: Path, *, output_path: Path) -> Path:
    """Render a watermarked, feed-sized copy of `video_path` at `output_path`."""
    logo_path = _ensure_logo()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    scale_filter = (
        "scale='if(gt(iw,ih),min(1920,iw),-2)':'if(gt(iw,ih),-2,min(1920,ih))'"
    )
    filter_complex = (
        f"[0:v]{scale_filter}[base];"
        f"[1:v]format=rgba,colorchannelmixer=aa={config.watermark_opacity},"
        f"scale={config.watermark_width_px}:-1[wm];"
        "[base][wm]overlay=W-w-24:H-h-24"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(logo_path),
        "-filter_complex",
        filter_complex,
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_path),
    ]

    log.info("[watermark] rendering posting copy -> %s", output_path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg watermark render failed:\n{result.stderr[-4000:]}")
    return output_path
