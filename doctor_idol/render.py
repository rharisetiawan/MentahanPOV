"""Cut a finalist's live recording into one vertical reel (1080x1920).

Same look for every finalist, so the series reads as one set:

- the chosen segments (finalists.toml) joined with a short video + audio
  crossfade, so a cut in the middle of a phrase doesn't click;
- upscaled to 1080x1920 (crowd recordings are often 480p); a recording
  that isn't already 9:16 sits on a blurred copy of itself instead of
  being cropped;
- the POV logo top-right, small and nearly opaque, with a soft shadow.
  Not centred like the street-POV pipeline (core/watermark.py): centre
  screen is the singer;
- a name card (name · city, song · artist) fading in top-left for the
  first seconds, in the MentahanPOV card style;
- loudness normalised to -14 LUFS, where the platforms normalise to.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from doctor_idol.catalog import Finalist, Settings

WIDTH, HEIGHT = 1080, 1920
FPS = 30
LOGO_MARGIN = 40
REPO_ROOT = Path(__file__).resolve().parent.parent
LOGO_PATH = REPO_ROOT / "assets" / "watermark-logo.png"


def is_vertical(width: int, height: int, tolerance: float = 0.04) -> bool:
    """True when the recording is (close to) 9:16 and can simply be scaled + cropped."""
    if not width or not height:
        return True
    return abs(width / height - WIDTH / HEIGHT) <= tolerance


def card_rows(finalist: Finalist) -> list[tuple[str, str]]:
    rows = []
    if finalist.label:
        rows.append(("pin", finalist.label))
    if finalist.song:
        rows.append(("speaker", " · ".join(p for p in (finalist.song, finalist.artist) if p)))
    return rows


def _fit(i: int, vertical: bool) -> str:
    if vertical:
        return (
            f"[{i}:v]fps={FPS},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase"
            f":flags=lanczos,crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p,settb=AVTB[v{i}]"
        )
    return (
        f"[{i}:v]fps={FPS},split[bg{i}][fg{i}];"
        f"[bg{i}]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},boxblur=40:4[bgb{i}];"
        f"[fg{i}]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos[fgs{i}];"
        f"[bgb{i}][fgs{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p,settb=AVTB[v{i}]"
    )


def build_filter(
    finalist: Finalist,
    settings: Settings,
    *,
    vertical: bool,
    has_card: bool,
) -> str:
    """The -filter_complex graph. Inputs: one per segment, then the logo, then the card."""
    segs = finalist.segments
    n = len(segs)
    xf = settings.transition_s
    parts: list[str] = []
    for i in range(n):
        parts.append(_fit(i, vertical))
        parts.append(
            f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]"
        )

    if n == 1:
        parts += ["[v0]null[base]", "[a0]anull[amain]"]
    elif xf > 0:
        # Each crossfade overlaps the next segment by `xf`, so the clip gets
        # (n-1)*xf shorter than the sum of its segments.
        vprev, aprev, offset = "v0", "a0", 0.0
        for i in range(1, n):
            offset += segs[i - 1].length - xf
            vout = "base" if i == n - 1 else f"vx{i}"
            aout = "amain" if i == n - 1 else f"ax{i}"
            parts.append(
                f"[{vprev}][v{i}]xfade=transition=fade:duration={xf:g}:offset={offset:.3f}[{vout}]"
            )
            parts.append(f"[{aprev}][a{i}]acrossfade=d={xf:g}[{aout}]")
            vprev, aprev = vout, aout
    else:
        pairs = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{pairs}concat=n={n}:v=1:a=1[base][amain]")

    logo = n
    m = LOGO_MARGIN
    parts += [
        f"[{logo}:v]format=rgba,scale={settings.logo_width_px}:-1,"
        f"colorchannelmixer=aa={settings.logo_opacity:g},split[wm][wms]",
        "[wms]colorchannelmixer=rr=0:gg=0:bb=0:aa=0.45,boxblur=6:1[shadow]",
        f"[base][shadow]overlay=W-w-{m - 4}:{m + 5}[b1]",
        f"[b1][wm]overlay=W-w-{m}:{m}[marked]",
    ]
    if has_card:
        secs = settings.name_card_s
        parts += [
            f"[{logo + 1}:v]format=rgba,fade=t=in:st=0:d=0.4:alpha=1,"
            f"fade=t=out:st={secs - 0.6:g}:d=0.6:alpha=1[card]",
            "[marked][card]overlay=x=round(W*0.05):y=round(H*0.10):eof_action=pass[vout]",
        ]
    else:
        parts.append("[marked]null[vout]")
    parts.append("[amain]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    return ";".join(parts)


def build_command(
    finalist: Finalist,
    settings: Settings,
    *,
    source: Path,
    output: Path,
    vertical: bool,
    card_png: Path | None,
) -> list[str]:
    inputs: list[str] = []
    for seg in finalist.segments:
        # Input seeking per segment: each segment is its own stream, which
        # xfade/acrossfade need (feeding them from split/asplit misbehaves).
        inputs += ["-ss", f"{seg.start:g}", "-t", f"{seg.length:g}", "-i", str(source)]
    inputs += ["-i", str(LOGO_PATH)]
    if card_png:
        inputs += ["-loop", "1", "-t", f"{settings.name_card_s:g}", "-i", str(card_png)]
    graph = build_filter(finalist, settings, vertical=vertical, has_card=card_png is not None)
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-r", str(FPS),
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]


def probe_size(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:stream_side_data=rotation",
         "-of", "csv=p=0:s=x", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    numbers = [int(x) for x in re.findall(r"-?\d+", out)]
    width, height = numbers[0], numbers[1]
    # A phone video stored landscape with a 90° rotation flag plays upright.
    if len(numbers) > 2 and abs(numbers[2]) in {90, 270}:
        width, height = height, width
    return width, height


def render(
    finalist: Finalist,
    settings: Settings,
    *,
    source: Path,
    out_dir: Path,
) -> Path:
    """Render reels.mp4 into out_dir (overwriting it) and return its path."""
    if not finalist.segments:
        raise ValueError(f"{finalist.slug}: belum ada segments di finalists.toml")
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "reels.mp4"
    card_png = None
    rows = card_rows(finalist)
    if settings.name_card and rows:
        from core.watermark import render_overlay_card

        card_png = render_overlay_card(rows, video_size=(WIDTH, HEIGHT), path=out_dir / "card.png")
    width, height = probe_size(source)
    cmd = build_command(
        finalist, settings, source=source, output=output,
        vertical=is_vertical(width, height), card_png=card_png,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg gagal untuk {finalist.slug}:\n{proc.stderr[-1500:]}")
    return output
