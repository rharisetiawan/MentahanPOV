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

# Rounded brand face for the on-video card (SIL OFL, licence next to it):
# the same chunky, friendly shape as the "POV" logo, instead of whatever
# system sans the box happens to have (DejaVu Sans Bold looked stiff).
_BRAND_FONT = Path(__file__).parent.parent / "assets" / "fonts" / "Fredoka.ttf"


def _load_brand_font(size: int):
    from PIL import ImageFont

    try:
        font = ImageFont.truetype(str(_BRAND_FONT), size)
    except OSError:
        return _load_font(size)
    try:
        font.set_variation_by_name("SemiBold")
    except (OSError, ValueError):  # FreeType without variation support
        pass
    return font


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
    # python:slim ships no fonts at all; Pillow >= 10.1 bundles a scalable
    # one, which beats the tiny fixed-size bitmap fallback.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
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


def _fit(width: int, height: int) -> tuple[int, int]:
    """Size after the 1920 long-edge cap (the scale filter below)."""
    if not width or not height:
        return 1080, 1920
    scale = min(1.0, 1920 / max(width, height))
    return max(2, round(width * scale)), max(2, round(height * scale))


def _posting_size(video_path: Path) -> tuple[int, int]:
    """(width, height) the posting copy will have after the 1920 long-edge cap."""
    from core.video_facts import probe_dimensions

    return _fit(*probe_dimensions(video_path))


def _posting_width(video_path: Path) -> int:
    """Width the posting copy will have after the 1920 long-edge cap."""
    return _posting_size(video_path)[0]


_WHITE = (255, 255, 255, 255)
_BRAND_PURPLE = (132, 85, 232)  # sampled from the MentahanPOV avatar

# CARD_STYLE → (pill fill, text + icon colour). "purple": brand-purple pill,
# white text; "white": white pill, purple text — like the avatar itself.
_CARD_STYLES = {
    "purple": ((*_BRAND_PURPLE, 225), _WHITE),
    "white": ((255, 255, 255, 235), (*_BRAND_PURPLE, 255)),
}


def _draw_pin(draw, cx: float, cy: float, size: float, fg, bg) -> None:
    r = size / 2
    head_y = cy - r * 0.45
    draw.ellipse((cx - r, head_y - r, cx + r, head_y + r), fill=fg)
    draw.polygon(
        [
            (cx - r * 0.82, head_y + r * 0.45),
            (cx + r * 0.82, head_y + r * 0.45),
            (cx, head_y + r * 2.1),
        ],
        fill=fg,
    )
    hole = r * 0.38
    draw.ellipse((cx - hole, head_y - hole, cx + hole, head_y + hole), fill=bg)


def _draw_speaker(draw, cx: float, cy: float, size: float, fg, bg) -> None:
    s = size / 2
    left = cx - s
    draw.rounded_rectangle(
        (left, cy - s * 0.35, left + s * 0.5, cy + s * 0.35), radius=s * 0.12, fill=fg
    )
    draw.polygon(
        [
            (left + s * 0.5, cy - s * 0.35),
            (left + s * 1.1, cy - s * 0.85),
            (left + s * 1.1, cy + s * 0.85),
            (left + s * 0.5, cy + s * 0.35),
        ],
        fill=fg,
    )
    width = max(2, round(size * 0.1))
    for k in (0.55, 0.9):
        rad = s * k
        box = (left + s * 1.1 - rad, cy - rad, left + s * 1.1 + rad, cy + rad)
        draw.arc(box, start=-45, end=45, fill=fg, width=width)


def _draw_search(draw, cx: float, cy: float, size: float, fg, bg) -> None:
    r = size * 0.32
    lx, ly = cx - size * 0.1, cy - size * 0.1
    width = max(2, round(size * 0.14))
    draw.ellipse((lx - r, ly - r, lx + r, ly + r), outline=fg, width=width)
    draw.line(
        (lx + r * 0.7, ly + r * 0.7, cx + size * 0.42, cy + size * 0.42),
        fill=fg,
        width=width,
    )
    # Round the handle's end so it matches the font's soft corners.
    end = width / 2
    hx, hy = cx + size * 0.42, cy + size * 0.42
    draw.ellipse((hx - end, hy - end, hx + end, hy + end), fill=fg)


_ICONS = {"pin": _draw_pin, "speaker": _draw_speaker, "search": _draw_search}


def render_overlay_card(
    rows: list[tuple[str, str]],
    *,
    video_size: tuple[int, int],
    path: Path,
    style: str | None = None,
) -> Path:
    """Stack of "icon + text" pills as one transparent PNG sized for the video.

    Styled after the MentahanPOV logo: rounded brand font, brand colours
    (CARD_STYLE), soft drop shadow. Icons are drawn as shapes, not emoji:
    the box has no colour-emoji font, and a missing glyph would render as
    an empty square.
    """
    from PIL import Image, ImageDraw, ImageFilter

    fill, fg = _CARD_STYLES.get(style or config.card_style, _CARD_STYLES["purple"])
    font_px = max(16, round(min(video_size) * 0.036))
    font = _load_brand_font(font_px)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pad_x, pad_y = round(font_px * 0.75), round(font_px * 0.42)
    icon_w, gap = round(font_px * 0.8), round(font_px * 0.38)
    row_gap = round(font_px * 0.3)
    # Room around the pills for the shadow to fall into.
    blur = max(2, round(font_px * 0.18))
    margin = blur * 3

    pills = []
    for icon, text in rows:
        left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
        # Height from the font's own ascent/descent, not this text's ink, so
        # rows with and without descenders ("Malang" vs "Sound on") match.
        ascent, descent = font.getmetrics()
        height = ascent + descent + 2 * pad_y
        width = pad_x + icon_w + gap + (right - left) + pad_x
        pills.append((icon, text, left, width, height))

    total_w = max(p[3] for p in pills) + 2 * margin
    total_h = sum(p[4] for p in pills) + row_gap * (len(pills) - 1) + 2 * margin
    shadow = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    card = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    sdraw, draw = ImageDraw.Draw(shadow), ImageDraw.Draw(card)
    y = margin
    for icon, text, left, width, height in pills:
        box = (margin, y, margin + width - 1, y + height - 1)
        sdraw.rounded_rectangle(
            (box[0], box[1] + blur, box[2], box[3] + blur), radius=height // 2, fill=(0, 0, 0, 90)
        )
        draw.rounded_rectangle(box, radius=height // 2, fill=fill)
        _ICONS[icon](draw, margin + pad_x + icon_w / 2, y + height / 2, icon_w * 0.9, fg, fill)
        draw.text(
            (margin + pad_x + icon_w + gap - left, y + pad_y),
            text,
            font=font,
            fill=fg,
        )
        y += height + row_gap
    img = shadow.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(card)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def render_location_label(text: str, *, video_size: tuple[int, int], path: Path) -> Path:
    """A single "📍 place" pill (see render_overlay_card)."""
    return render_overlay_card([("pin", text)], video_size=video_size, path=path)


def hook_start(start_s: float | None, duration: float) -> float:
    """Where the posting copy starts: Gemini's hook moment, within reason.

    Skipping a dull opening is the point; skipping most of the clip is not —
    the hook is only honoured in the first 40%, at least 1s in, and when at
    least 6s of clip remain.
    """
    if not config.hook_trim or not start_s or not duration:
        return 0.0
    if 1.0 <= start_s <= duration * 0.4 and duration - start_s >= 6:
        return round(float(start_s), 2)
    return 0.0


def _build_graph(
    *,
    length: float,
    fps: float,
    has_audio: bool,
    wm_width: int,
    card_secs: float | None,
) -> tuple[str, list[str]]:
    """The filter graph and its -map arguments."""
    scale = "scale='if(gt(iw,ih),min(1920,iw),-2)':'if(gt(iw,ih),-2,min(1920,ih))'"
    xf = float(config.loop_crossfade_s)
    loop = config.seamless_loop and length >= 6 and xf > 0
    parts: list[str] = []

    if loop:
        # Seamless loop: blend the tail into the opening, then drop the
        # first `xf` seconds, so the last frame flows straight into the
        # first one. Shorts counts every replay as a view, and an ambient
        # clip with no visible seam gets replayed without anyone noticing.
        # (Constant frame rate first: xfade refuses variable-rate input. The
        # head needs it again after setpts — ffmpeg 7.x marks a setpts output
        # as rate 1/0, and xfade then rejects the pair outright.)
        rate = f"fps={fps:g}" if fps else "fps=30"
        offset = max(0.1, length - xf - 0.05)
        parts += [
            f"[0:v]{scale},{rate},setsar=1,split=2[va][vb]",
            f"[vb]trim=0:{xf:g},setpts=PTS-STARTPTS,{rate}[vh]",
            f"[va][vh]xfade=transition=fade:duration={xf:g}:offset={offset:g}[vx]",
            f"[vx]trim=start={xf:g},setpts=PTS-STARTPTS[base]",
        ]
    else:
        parts.append(f"[0:v]{scale},setsar=1[base]")

    parts += [
        f"[1:v]format=rgba,colorchannelmixer=aa={config.watermark_opacity},"
        f"scale={wm_width}:-1[wm]",
        "[base][wm]overlay=(W-w)/2:(H-h)/2[marked]",
    ]
    if card_secs:
        parts += [
            f"[2:v]format=rgba,fade=t=in:st=0:d=0.4:alpha=1,"
            f"fade=t=out:st={card_secs - 0.6:g}:d=0.6:alpha=1[card]",
            "[marked][card]overlay=x=round(W*0.05):y=round(H*0.08):eof_action=pass[vout]",
        ]
    else:
        parts.append("[marked]null[vout]")
    maps = ["-map", "[vout]"]

    if has_audio:
        if loop:
            # Same seam as the video: the body (opening `xf` dropped) fades
            # out over its last `xf` seconds while the opening fades in on
            # top of it. Built from afade+amix rather than acrossfade, which
            # on ffmpeg 7.x emits only a few ms when fed from asplit.
            tail = max(0.0, length - 2 * xf)
            parts += [
                "[0:a]asplit=2[aa][ab]",
                f"[aa]atrim=start={xf:g},asetpts=PTS-STARTPTS,"
                f"afade=t=out:st={tail:g}:d={xf:g}[abody]",
                f"[ab]atrim=0:{xf:g},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={xf:g},adelay={round(tail * 1000)}:all=1[ahead]",
                "[abody][ahead]amix=inputs=2:duration=first:normalize=0[a1]",
            ]
        else:
            parts.append("[0:a]anull[a1]")
        # Phone ambience is usually recorded quietly; -14 LUFS is where the
        # platforms normalise to, so a quiet clip no longer sounds weak
        # next to everything else in the feed.
        parts.append(
            "[a1]loudnorm=I=-14:TP=-1.5:LRA=11[aout]" if config.loudnorm else "[a1]anull[aout]"
        )
        maps += ["-map", "[aout]"]
    return ";".join(parts), maps


def make_posting_copy(
    video_path: Path,
    *,
    output_path: Path,
    label: str | None = None,
    start_s: float | None = None,
    keyword: str | None = None,
) -> Path:
    """Render a watermarked, feed-sized copy of `video_path` at `output_path`.

    The master is never touched — it goes to Drive as-is, un-watermarked and
    un-recompressed — so this copy exists purely for the social platforms,
    and is tuned for the first two seconds that decide whether people swipe:

    - starts at Gemini's hook moment (`start_s`, see hook_start());
    - a card fades in top-left for LOCATION_LABEL_SECONDS: the search
      phrase (`keyword`, e.g. "mentahan hujan malam" — TikTok and Instagram
      read on-screen text for search), the place (`label`, e.g.
      "Jl. Basuki Rahmat · Malang") and a "Sound on" hint — the ambient
      audio is the point and feeds scroll muted. Top-left because the
      platforms' own UI covers the bottom;
    - loops seamlessly and has its loudness normalised (_build_graph).
    """
    from core.video_facts import probe_stream_info

    logo_path = _ensure_logo()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    info = probe_stream_info(video_path)
    size = _fit(info.width, info.height)
    start = hook_start(start_s, info.duration)
    length = max(0.0, info.duration - start)

    rows: list[tuple[str, str]] = []
    if keyword and config.search_label:
        rows.append(("search", keyword))
    if label:
        rows.append(("pin", label))
    if config.sound_on_hint and info.has_audio and config.sound_on_text:
        rows.append(("speaker", config.sound_on_text))
    card_png = output_path.with_suffix(".label.png")
    card_secs = max(1.5, float(config.location_label_seconds)) if rows else None
    card_inputs: list[str] = []
    if rows:
        render_overlay_card(rows, video_size=size, path=card_png)
        card_inputs = ["-loop", "1", "-t", f"{card_secs:g}", "-i", str(card_png)]

    graph, maps = _build_graph(
        length=length,
        fps=info.fps,
        has_audio=info.has_audio,
        wm_width=max(2, round(size[0] * config.watermark_width_pct)),
        card_secs=card_secs,
    )
    cmd = [
        "ffmpeg",
        "-y",
        *(["-ss", f"{start:g}"] if start else []),
        "-i",
        str(video_path),
        "-i",
        str(logo_path),
        *card_inputs,
        "-filter_complex",
        graph,
        *maps,
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "veryfast",
        # 8-bit 4:2:0: a 10-bit (HDR) phone master would otherwise come out
        # as High 10 profile, which Meta/YouTube re-transcode or reject.
        "-pix_fmt",
        "yuv420p",
        # moov atom up front so platforms that fetch by URL (Instagram via
        # the Drive staging link) can start reading before the end arrives.
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        # loudnorm upsamples internally; bring it back to a normal rate.
        "-ar",
        "48000",
        str(output_path),
    ]

    if start:
        log.info("[watermark] starting the posting copy at the hook, %.1fs in", start)
    log.info("[watermark] rendering posting copy -> %s", output_path)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        card_png.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg watermark render failed:\n{result.stderr[-4000:]}")
    return output_path


# Instagram rejects story videos longer than this; Facebook's limit is
# higher, but one trimmed file keeps both platforms on identical footage.
STORY_MAX_S = 60


def make_story_copy(posting_copy: Path, *, output_path: Path, duration_s: int) -> Path:
    """Return a story-safe cut of `posting_copy` (<= STORY_MAX_S).

    Stories carry exactly the same frame as the feed post — the centred
    logo and nothing else. An earlier version burned a call-to-action
    banner in here; it looked like a dialog box pasted over the footage
    and was cut. If a story ever needs a caption, it belongs in
    Instagram's own sticker, not rendered into the pixels.

    Clips already within the limit are reused untouched, so most runs skip
    this entirely; longer ones are cut with a stream copy — no re-encode,
    so no second generation of quality loss.
    """
    if duration_s <= STORY_MAX_S:
        return posting_copy

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(posting_copy),
        "-t",
        str(STORY_MAX_S),
        "-c",
        "copy",
        str(output_path),
    ]
    log.info(
        "[watermark] clip is %ss; trimming to %ss for stories -> %s",
        duration_s,
        STORY_MAX_S,
        output_path,
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg story trim failed:\n{result.stderr[-4000:]}")
    return output_path
