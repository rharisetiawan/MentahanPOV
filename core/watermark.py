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


def _posting_width(video_path: Path) -> int:
    """Width the posting copy will have after the 1920 long-edge cap."""
    from core.video_facts import probe_dimensions

    w, h = probe_dimensions(video_path)
    if not w or not h:
        return 1080
    if w > h:
        return min(1920, w)
    capped_h = min(1920, h)
    return max(2, round(w * capped_h / h))


def make_posting_copy(video_path: Path, *, output_path: Path) -> Path:
    """Render a watermarked, feed-sized copy of `video_path` at `output_path`.

    The master is never touched — it goes to Drive as-is, un-watermarked and
    un-recompressed — so this copy exists purely for the social platforms.
    """
    logo_path = _ensure_logo()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wm_width = max(2, round(_posting_width(video_path) * config.watermark_width_pct))
    scale_filter = (
        "scale='if(gt(iw,ih),min(1920,iw),-2)':'if(gt(iw,ih),-2,min(1920,ih))'"
    )
    filter_complex = (
        f"[0:v]{scale_filter}[base];"
        f"[1:v]format=rgba,colorchannelmixer=aa={config.watermark_opacity},"
        f"scale={wm_width}:-1[wm];"
        "[base][wm]overlay=(W-w)/2:(H-h)/2"
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


# Instagram rejects story videos longer than this; Facebook's limit is
# higher, but one trimmed file keeps both platforms on identical footage.
STORY_MAX_S = 60

_CTA_HEADLINE = "FILE MENTAH GRATIS"
_CTA_SUBLINE = "tanpa watermark · link di bio"


def _render_cta_banner(path: Path, video_w: int) -> Path:
    """Draw the story call-to-action strip as a PNG.

    The Stories API accepts a video and nothing else — no caption, no text
    sticker, no link. So the only way a story can advertise the archive is
    to have the words baked into the pixels.
    """
    from PIL import Image, ImageDraw

    width = int(video_w * 0.88)
    pad = int(width * 0.055)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    def _fit(text: str, start_px: int) -> "object":
        """Largest font size at which `text` still clears the padding."""
        inner = width - pad * 2
        size = start_px
        while size > 8:
            font = _load_font(size)
            box = probe.textbbox((0, 0), text, font=font)
            if box[2] - box[0] <= inner:
                return font
            size -= 2
        return _load_font(8)

    head_font = _fit(_CTA_HEADLINE, int(width * 0.085))
    sub_font = _fit(_CTA_SUBLINE, int(width * 0.045))

    hb = probe.textbbox((0, 0), _CTA_HEADLINE, font=head_font)
    sb = probe.textbbox((0, 0), _CTA_SUBLINE, font=sub_font)
    gap = int(pad * 0.35)
    height = pad * 2 + (hb[3] - hb[1]) + gap + (sb[3] - sb[1])

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = int(height * 0.28)
    draw.rounded_rectangle(
        [(0, 0), (width - 1, height - 1)], radius=radius, fill=(0, 0, 0, 150)
    )
    y = pad
    draw.text(
        ((width - (hb[2] - hb[0])) / 2 - hb[0], y - hb[1]),
        _CTA_HEADLINE,
        font=head_font,
        fill=(255, 255, 255, 255),
    )
    y += (hb[3] - hb[1]) + gap
    draw.text(
        ((width - (sb[2] - sb[0])) / 2 - sb[0], y - sb[1]),
        _CTA_SUBLINE,
        font=sub_font,
        fill=(235, 235, 235, 255),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def make_story_copy(
    posting_copy: Path,
    *,
    output_path: Path,
    duration_s: int,
    video_width: int,
) -> Path:
    """Render a story-ready cut of `posting_copy`: <= STORY_MAX_S, with CTA.

    Always re-encodes, because the call-to-action has to be burned in —
    a stream copy can't composite. The trim is applied in the same pass so
    long clips only get decoded once.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    banner = _render_cta_banner(
        output_path.parent / "story-cta.png", video_width or 1080
    )

    # Sit the banner clear of Instagram's bottom chrome (the reply bar and
    # profile row eat roughly the lowest 18% of a story).
    filter_complex = "[0:v][1:v]overlay=(W-w)/2:H-h-(H*0.22)"
    cmd = ["ffmpeg", "-y", "-i", str(posting_copy), "-i", str(banner)]
    if duration_s > STORY_MAX_S:
        cmd += ["-t", str(STORY_MAX_S)]
        log.info("[watermark] clip is %ss; story cut to %ss", duration_s, STORY_MAX_S)
    cmd += [
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

    log.info("[watermark] rendering story copy -> %s", output_path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg story render failed:\n{result.stderr[-4000:]}")
    return output_path
