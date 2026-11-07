"""The full performance for YouTube: 1920x1080, thumbnail and upload text.

Landscape on purpose. YouTube treats a vertical video of up to three minutes
as a Short, and some recordings are that short, so a 16:9 frame keeps every
one of them a regular video. The vertical recording sits in the middle on a
blurred copy of itself, never cropped, with the same series frame as the
reels: hook, name card, logo, progress bar and score prompt.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from doctor_idol.catalog import Finalist, Segment, Settings
from doctor_idol.cover import PURPLE, _wrap, series_tag
from doctor_idol.render import BAR_HEIGHT, BRAND_PURPLE, FPS, LOGO_MARGIN, LOGO_PATH, Card, plan_cards

WIDTH, HEIGHT = 1920, 1080
THUMB_WIDTH, THUMB_HEIGHT = 1280, 720
# Where the centred vertical video starts on the thumbnail: text stays left of it.
THUMB_TEXT_RIGHT = (THUMB_WIDTH - THUMB_HEIGHT * 9 // 16) // 2 - 16
THUMB_MARGIN = 36
CARD_MAX_WIDTH = WIDTH - round(WIDTH * 0.05) - 24


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip())


def long_cut(finalist: Finalist, duration: float) -> Segment:
    """The `full` range from finalists.toml, or the whole recording."""
    if finalist.full is None:
        return Segment(0.0, duration)
    return Segment(finalist.full.start, min(finalist.full.end, duration))


def build_filter(cards: list[Card], settings: Settings, total: float) -> str:
    """Inputs: the recording, the logo, then one per card."""
    m = LOGO_MARGIN
    parts = [
        f"[0:v]fps={FPS},split[bg][fg]",
        f"[bg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},boxblur=40:4,eq=brightness=-0.12[bgb]",
        f"[fg]scale=-2:{HEIGHT}:flags=lanczos[fgs]",
        "[bgb][fgs]overlay=(W-w)/2:0,setsar=1,format=yuv420p[base]",
        f"[1:v]format=rgba,scale={settings.logo_width_px}:-1,"
        f"colorchannelmixer=aa={settings.logo_opacity:g},split[wm][wms]",
        "[wms]colorchannelmixer=rr=0:gg=0:bb=0:aa=0.45,boxblur=6:1[shadow]",
        f"[base][shadow]overlay=W-w-{m - 4}:{m + 5}[b1]",
        f"[b1][wm]overlay=W-w-{m}:{m}[marked]",
    ]
    prev = "marked"
    for j, card in enumerate(cards):
        fade_out = max(0.0, card.dur - 0.4)
        x = "(W-w)/2" if card.centred else "round(W*0.05)"
        parts += [
            f"[{j + 2}:v]format=rgba,fade=t=in:st=0:d=0.3:alpha=1,"
            f"fade=t=out:st={fade_out:g}:d=0.4:alpha=1,setpts=PTS+{card.start:.3f}/TB[c{j}]",
            f"[{prev}][c{j}]overlay=x={x}:y=round(H*0.10):eof_action=pass[o{j}]",
        ]
        prev = f"o{j}"
    if settings.progress_bar:
        parts += [
            f"color=c={BRAND_PURPLE}:s={WIDTH}x{BAR_HEIGHT}:r={FPS}:d={total:.3f}[bar]",
            f"[{prev}][bar]overlay=x=-w+w*t/{total:.3f}:y=0:eof_action=pass[vout]",
        ]
    else:
        parts.append(f"[{prev}]null[vout]")
    parts.append(
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,"
        "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]"
    )
    return ";".join(parts)


def build_command(
    settings: Settings,
    *,
    source: Path,
    output: Path,
    cut: Segment,
    cards: list[tuple[Card, Path]] = (),
) -> list[str]:
    inputs = ["-ss", f"{cut.start:g}", "-t", f"{cut.length:g}", "-i", str(source), "-i", str(LOGO_PATH)]
    for card, png in cards:
        inputs += ["-loop", "1", "-t", f"{card.dur:g}", "-i", str(png)]
    graph = build_filter([c for c, _ in cards], settings, cut.length)
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-r", str(FPS),
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]


def render_youtube(finalist: Finalist, settings: Settings, *, source: Path, out_dir: Path) -> Path:
    """Render video.mp4 into out_dir (overwriting it) and return its path."""
    from core.watermark import render_overlay_card

    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "video.mp4"
    cut = long_cut(finalist, probe_duration(source))
    # plan_cards times the cards against the segments, so hand it the full cut.
    planned = plan_cards(dataclasses.replace(finalist, segments=(cut,)), settings)
    cards = [
        (card, render_overlay_card(
            list(card.rows), video_size=(WIDTH, HEIGHT), path=out_dir / f"card-{card.name}.png",
            max_width=CARD_MAX_WIDTH,
        ))
        for card in planned
    ]
    cmd = build_command(settings, source=source, output=output, cut=cut, cards=cards)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg gagal untuk {finalist.slug}:\n{proc.stderr[-1500:]}")
    return output


def compose_thumbnail(frame: Path, finalist: Finalist, settings: Settings, out: Path) -> Path:
    """Series pill, name and song in the blurred space left of the singer."""
    from PIL import Image, ImageDraw

    from core.watermark import _load_brand_font

    img = Image.open(frame).convert("RGBA")
    # Trim the progress bar off the top: a half-filled line means nothing on a thumbnail.
    img = img.crop((0, BAR_HEIGHT, img.width, img.height)).resize((THUMB_WIDTH, THUMB_HEIGHT))
    shade = Image.new("L", (THUMB_WIDTH, 1))
    for x in range(THUMB_WIDTH):
        shade.putpixel((x, 0), max(0, 190 - int(190 * x / (THUMB_TEXT_RIGHT * 1.15))))
    black = Image.new("RGBA", img.size, (0, 0, 0, 255))
    black.putalpha(shade.resize(img.size))
    img.alpha_composite(black)

    draw = ImageDraw.Draw(img)
    pill_font = _load_brand_font(24)
    name_font = _load_brand_font(50)
    song_font = _load_brand_font(30)
    small_font = _load_brand_font(22)
    width = THUMB_TEXT_RIGHT - THUMB_MARGIN

    name_lines = _wrap(draw, finalist.name or "[nama finalis]", name_font, width)
    song_lines = _wrap(draw, finalist.song, song_font, width) if finalist.song else []
    pill = series_tag(finalist, settings)
    pad_x, pad_y = 16, 9
    pill_h = pill_font.size + 2 * pad_y
    name_h = int(name_font.size * 1.1)
    song_h = int(song_font.size * 1.2)
    block = pill_h + 22 + len(name_lines) * name_h + 18 + len(song_lines) * song_h + 18 + small_font.size

    y = (THUMB_HEIGHT - block) // 2
    pill_w = int(draw.textlength(pill, font=pill_font)) + 2 * pad_x
    draw.rounded_rectangle(
        (THUMB_MARGIN, y, THUMB_MARGIN + pill_w, y + pill_h), radius=pill_h // 2, fill=(*PURPLE, 240)
    )
    draw.text((THUMB_MARGIN + pad_x, y + pad_y - 3), pill, font=pill_font, fill="white")
    y += pill_h + 22
    for line in name_lines:
        draw.text((THUMB_MARGIN, y), line, font=name_font, fill="white")
        y += name_h
    y += 18
    for line in song_lines:
        draw.text((THUMB_MARGIN, y), line, font=song_font, fill=(255, 255, 255, 235))
        y += song_h
    y += 18
    kind = "Rekaman utuh" if finalist.kind == "announcement" else "Penampilan utuh"
    draw.text((THUMB_MARGIN, y), f"{kind} · fancam", font=small_font, fill=(255, 255, 255, 200))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, quality=92)
    return out


def make_thumbnail(video: Path, finalist: Finalist, settings: Settings) -> Path:
    """Write thumbnail.jpg next to the video, from a moment past the opening cards."""
    at = probe_duration(video) * 0.45
    frame = video.with_name("thumbnail-frame.png")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", str(frame)],
        check=True,
    )
    try:
        return compose_thumbnail(frame, finalist, settings, video.with_name("thumbnail.jpg"))
    finally:
        frame.unlink(missing_ok=True)
