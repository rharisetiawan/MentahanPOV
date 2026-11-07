"""cover.jpg: the same cover layout for every finalist, so the profile grid reads as one series.

A frame from the reel itself (logo already on it), a dark fade at the
bottom, then: purple series pill ("POV KURSI PENONTON #1"), the name big,
city · song, and "fancam" in small print. Everything sits inside the
middle 3:4 of the 9:16 frame, which is what the Instagram grid shows.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from doctor_idol.catalog import Finalist, Settings

WIDTH, HEIGHT = 1080, 1920
SAFE_BOTTOM = HEIGHT - (HEIGHT - WIDTH * 4 // 3) // 2 - 60  # bottom of the 3:4 grid crop, minus air
MARGIN_X = 72
PURPLE = (132, 85, 232)


def cover_time(finalist: Finalist, settings: Settings) -> float:
    """A moment after the opening cards and before the score prompt."""
    total = finalist.reel_length(settings.transition_s)
    after_cards = settings.hook_s + (settings.name_card_s if settings.name_card else 0)
    before_score = total - settings.score_s
    if before_score > after_cards:
        return round((after_cards + before_score) / 2, 2)
    return round(total / 2, 2)


def series_tag(finalist: Finalist, settings: Settings) -> str:
    tag = settings.series.upper()
    return f"{tag} #{finalist.episode}" if finalist.episode else tag


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        trial = f"{lines[-1]} {word}" if lines else word
        if lines and draw.textlength(trial, font=font) <= width:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines


def compose(frame: Path, finalist: Finalist, settings: Settings, out: Path) -> Path:
    from PIL import Image, ImageDraw

    from core.watermark import _load_brand_font

    img = Image.open(frame).convert("RGBA").resize((WIDTH, HEIGHT))
    shade = Image.new("L", (1, HEIGHT))
    top = int(HEIGHT * 0.45)
    for y in range(HEIGHT):
        shade.putpixel((0, y), 0 if y < top else min(215, int(215 * (y - top) / (HEIGHT * 0.35))))
    black = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 255))
    black.putalpha(shade.resize((WIDTH, HEIGHT)))
    img.alpha_composite(black)

    draw = ImageDraw.Draw(img)
    pill_font = _load_brand_font(40)
    name_font = _load_brand_font(92)
    line_font = _load_brand_font(46)
    small_font = _load_brand_font(34)
    text_w = WIDTH - 2 * MARGIN_X

    name_lines = _wrap(draw, finalist.name or finalist.slug, name_font, text_w)
    detail = " · ".join(p for p in (finalist.city, finalist.song) if p)
    small = f"{settings.event} {settings.season} · fancam penonton"
    pill = series_tag(finalist, settings)

    # Lay out bottom-up from the grid's safe bottom edge.
    y = SAFE_BOTTOM
    y -= small_font.size
    small_y = y
    y -= 28 + (line_font.size if detail else 0)
    detail_y = y
    y -= 24 + len(name_lines) * int(name_font.size * 1.08)
    name_y = y
    pad_x, pad_y = 26, 14
    pill_h = pill_font.size + 2 * pad_y
    y -= 28 + pill_h
    pill_y = y

    pill_w = int(draw.textlength(pill, font=pill_font)) + 2 * pad_x
    draw.rounded_rectangle(
        (MARGIN_X, pill_y, MARGIN_X + pill_w, pill_y + pill_h), radius=pill_h // 2, fill=(*PURPLE, 240)
    )
    draw.text((MARGIN_X + pad_x, pill_y + pad_y - 4), pill, font=pill_font, fill="white")
    for i, line in enumerate(name_lines):
        draw.text((MARGIN_X, name_y + i * int(name_font.size * 1.08)), line, font=name_font, fill="white")
    if detail:
        draw.text((MARGIN_X, detail_y), detail, font=line_font, fill=(255, 255, 255, 235))
    draw.text((MARGIN_X, small_y), small, font=small_font, fill=(255, 255, 255, 190))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, quality=92)
    return out


def make_cover(reel: Path, finalist: Finalist, settings: Settings) -> Path:
    """Write cover.jpg next to the reel."""
    frame = reel.with_name("cover-frame.png")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_time(finalist, settings):g}",
         "-i", str(reel), "-frames:v", "1", str(frame)],
        check=True,
    )
    try:
        return compose(frame, finalist, settings, reel.with_name("cover.jpg"))
    finally:
        frame.unlink(missing_ok=True)
