"""MentahanPOV — end-to-end pipeline orchestrator.

Usage:
    python main.py path/to/video.mp4
    python main.py path/to/video.mp4 --platforms youtube,instagram
    python main.py path/to/video.mp4 --dry-run
    python main.py path/to/video.mp4 --skip-gdrive  # use cached URL from state

The orchestrator is idempotent: rerunning skips platforms that previously
succeeded (according to STATE_FILE) and only retries the failed ones.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable

from config import config
from core import gdrive, geocode, gemini, state, video_facts, watermark
from distributors import facebook, instagram, tiktok, youtube

log = logging.getLogger("mentahanpov")

# Platform name → callable(video_path, caption, *, gdrive_url=...)
PLATFORM_REGISTRY: dict[str, Callable[..., dict[str, str]]] = {
    "youtube": youtube.post,
    "facebook": facebook.post,
    "instagram": instagram.post,
    "tiktok": tiktok.post,
}


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mentahanpov")
    p.add_argument("video", type=Path, help="Path to the source video file.")
    p.add_argument(
        "--platforms",
        default=",".join(config.platforms),
        help=f"Comma-separated subset of: {','.join(PLATFORM_REGISTRY)}",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run gdrive + gemini only; skip distribution.",
    )
    p.add_argument(
        "--skip-gdrive", action="store_true", help="Reuse gdrive_url from state file."
    )
    p.add_argument(
        "--skip-gemini", action="store_true", help="Reuse caption from state file."
    )
    p.add_argument(
        "--skip-watermark",
        action="store_true",
        help="Reuse cached posting copy from state file.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-post even if a platform previously succeeded.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def step_geocode(facts: video_facts.VideoFacts) -> tuple[str, str]:
    if not facts.gps:
        log.warning("[geocode] no GPS in video metadata; caption will lack location")
        return "Lokasi tidak tersedia", "-"
    lat, lon = facts.gps
    coordinates = f"{lat:.6f}, {lon:.6f}"
    address = geocode.reverse_geocode(lat, lon) or coordinates
    return address, coordinates


def step_gemini(
    video: Path,
    facts: video_facts.VideoFacts,
    address: str,
    coordinates: str,
    *,
    skip: bool,
) -> gemini.GeminiOutput:
    entry = state.get(config.state_file, video)
    cached = entry.get("gemini", {})
    if skip and entry.get("caption") and cached.get("file_name"):
        log.info("[gemini] reusing cached caption/filename/folder from state")
        return gemini.GeminiOutput(
            caption=entry["caption"],
            file_name=cached["file_name"],
            folder=cached["folder"],
            raw="",
        )
    out = gemini.generate_metadata(
        video,
        date=facts.date,
        address=address,
        coordinates=coordinates,
        duration_s=facts.duration_s,
        resolution=facts.resolution,
    )
    state.update(
        config.state_file,
        video,
        {
            "caption": out.caption,
            "gemini": {"file_name": out.file_name, "folder": out.folder},
        },
    )
    return out


def step_gdrive(
    video: Path, gemini_out: gemini.GeminiOutput, *, skip: bool
) -> str:
    entry = state.get(config.state_file, video)
    if skip and entry.get("gdrive_url"):
        log.info("[gdrive] reusing cached URL: %s", entry["gdrive_url"])
        return entry["gdrive_url"]
    folder_id = gdrive.resolve_category_folder_id(gemini_out.folder)
    info = gdrive.upload_video(
        video, dest_folder_id=folder_id, dest_filename=gemini_out.file_name
    )
    state.update(
        config.state_file,
        video,
        {"gdrive_id": info["id"], "gdrive_url": info["view_url"]},
    )
    return info["view_url"]


def step_watermark(video: Path, *, skip: bool) -> Path:
    entry = state.get(config.state_file, video)
    cached = entry.get("posting_copy_path")
    if skip and cached and Path(cached).exists():
        log.info("[watermark] reusing cached posting copy: %s", cached)
        return Path(cached)
    out_path = config.posting_copy_dir / f"{state.video_key(video)}_post.mp4"
    watermark.make_posting_copy(video, output_path=out_path)
    state.update(config.state_file, video, {"posting_copy_path": str(out_path)})
    return out_path


def step_distribute(
    video: Path,
    post_video: Path,
    caption: str,
    gdrive_url: str,
    platforms: list[str],
    force: bool,
) -> None:
    for name in platforms:
        if name not in PLATFORM_REGISTRY:
            log.error("[%s] unknown platform, skipping", name)
            continue
        if not force and state.already_succeeded(config.state_file, video, name):
            log.info(
                "[%s] already posted previously; skipping (use --force to repost)", name
            )
            continue
        try:
            log.info("[%s] posting…", name)
            result = PLATFORM_REGISTRY[name](post_video, caption, gdrive_url=gdrive_url)
            state.mark_platform(
                config.state_file,
                video,
                name,
                status="ok",
                url=result.get("url"),
                error=None,
            )
            log.info("[%s] OK → %s", name, result.get("url"))
        except Exception as exc:  # noqa: BLE001 — we WANT to keep going.
            log.exception("[%s] FAILED: %s", name, exc)
            state.mark_platform(
                config.state_file,
                video,
                name,
                status="error",
                url=None,
                error=f"{type(exc).__name__}: {exc}",
            )


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    video: Path = args.video.expanduser().resolve()
    if not video.exists():
        log.error("Video not found: %s", video)
        return 2

    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    log.info("Pipeline start: video=%s platforms=%s", video.name, platforms)

    facts = video_facts.extract_facts(video)
    address, coordinates = step_geocode(facts)
    log.info("Location: %s (%s)", address, coordinates)

    gemini_out = step_gemini(video, facts, address, coordinates, skip=args.skip_gemini)
    log.info("Folder: %s | file_name: %s", gemini_out.folder, gemini_out.file_name)
    log.info("Caption (%d chars):\n%s", len(gemini_out.caption), gemini_out.caption)

    gdrive_url = step_gdrive(video, gemini_out, skip=args.skip_gdrive)
    log.info("GDrive URL: %s", gdrive_url)

    if args.dry_run:
        log.info("--dry-run set; skipping watermark render + distribution.")
        return 0

    post_video = step_watermark(video, skip=args.skip_watermark)
    log.info("Posting copy ready: %s", post_video)

    step_distribute(video, post_video, gemini_out.caption, gdrive_url, platforms, args.force)

    final = state.get(config.state_file, video)
    log.info("Final state: %s", final.get("platforms"))
    failed = [
        k for k, v in final.get("platforms", {}).items() if v.get("status") != "ok"
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
