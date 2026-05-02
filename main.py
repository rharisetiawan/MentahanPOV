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
from core import gdrive, gemini, state
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
        "--force",
        action="store_true",
        help="Re-post even if a platform previously succeeded.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def step_gdrive(video: Path, *, skip: bool) -> str:
    entry = state.get(config.state_file, video)
    if skip and entry.get("gdrive_url"):
        log.info("[gdrive] reusing cached URL: %s", entry["gdrive_url"])
        return entry["gdrive_url"]
    info = gdrive.upload_video(video)
    state.update(
        config.state_file,
        video,
        {"gdrive_id": info["id"], "gdrive_url": info["view_url"]},
    )
    return info["view_url"]


def step_gemini(
    video: Path, gdrive_url: str, *, skip: bool
) -> tuple[str, dict[str, str]]:
    entry = state.get(config.state_file, video)
    if skip and entry.get("caption"):
        log.info("[gemini] reusing cached caption from state")
        return entry["caption"], entry.get("gemini", {})
    out = gemini.generate_metadata(video, gdrive_url)
    caption = out.to_caption()
    payload = {
        "visual_analysis": out.visual_analysis,
        "draft_caption": out.draft_caption,
        "cloud_storage": out.cloud_storage,
    }
    state.update(config.state_file, video, {"caption": caption, "gemini": payload})
    return caption, payload


def step_distribute(
    video: Path, caption: str, gdrive_url: str, platforms: list[str], force: bool
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
            result = PLATFORM_REGISTRY[name](video, caption, gdrive_url=gdrive_url)
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

    gdrive_url = step_gdrive(video, skip=args.skip_gdrive)
    log.info("GDrive URL: %s", gdrive_url)

    caption, _ = step_gemini(video, gdrive_url, skip=args.skip_gemini)
    log.info("Caption (%d chars):\n%s", len(caption), caption)

    if args.dry_run:
        log.info("--dry-run set; skipping distribution.")
        return 0

    step_distribute(video, caption, gdrive_url, platforms, args.force)

    final = state.get(config.state_file, video)
    log.info("Final state: %s", final.get("platforms"))
    failed = [
        k for k, v in final.get("platforms", {}).items() if v.get("status") != "ok"
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
