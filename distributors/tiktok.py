"""TikTok uploader via Playwright browser automation.

Two CLI subcommands:
- `python -m distributors.tiktok login`   → opens a real Chromium window so
  you can log in to TikTok manually; saves cookies to TIKTOK_STORAGE_STATE.
- `python -m distributors.tiktok post <video> "caption"`  → uploads the video
  using the saved storage state.

Why Playwright instead of the official Content Posting API?
The Content Posting API requires app review approval (often weeks). Playwright
works immediately and is easy to debug because it runs in headed mode by
default. Once an app is approved, swap this module for an HTTP client.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

from config import config

log = logging.getLogger(__name__)
PLATFORM = "tiktok"

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=upload"
LOGIN_URL = "https://www.tiktok.com/login"


def _ensure_storage_dir() -> None:
    config.tiktok_storage_state.parent.mkdir(parents=True, exist_ok=True)


def login() -> None:
    """Headed login flow → saves storage state for future upload runs."""
    from playwright.sync_api import sync_playwright

    _ensure_storage_dir()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL)
        print(">>> Log in to TikTok in the opened window.")
        print(">>> When you can see your profile in the top-right, press Enter here.")
        input()
        context.storage_state(path=str(config.tiktok_storage_state))
        browser.close()
        print(f"Saved TikTok storage state to {config.tiktok_storage_state}")


def post(video_path: Path, caption: str, **_: Any) -> dict[str, str]:
    """Upload the video and publish with the given caption."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    if not config.tiktok_storage_state.exists():
        raise FileNotFoundError(
            f"TikTok storage state not found at {config.tiktok_storage_state}. "
            "Run: python -m distributors.tiktok login"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.tiktok_headless)
        context = browser.new_context(storage_state=str(config.tiktok_storage_state))
        page = context.new_page()
        log.info("[tiktok] opening upload page")
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)

        # File chooser — TikTok Studio exposes a hidden <input type="file">.
        try:
            file_input = page.wait_for_selector(
                'input[type="file"]', timeout=30_000, state="attached"
            )
        except PWTimeout as exc:
            raise RuntimeError(
                "Could not find TikTok file input — UI may have changed."
            ) from exc
        file_input.set_input_files(str(video_path))
        log.info("[tiktok] uploaded file, waiting for processing")

        # Caption box (contenteditable). Selector tends to drift; we try several.
        caption_selectors = [
            'div[contenteditable="true"][role="combobox"]',
            "div.public-DraftEditor-content",
            'div[data-contents="true"]',
            'div[contenteditable="true"]',
        ]
        caption_box = None
        for sel in caption_selectors:
            try:
                caption_box = page.wait_for_selector(sel, timeout=15_000)
                if caption_box:
                    break
            except PWTimeout:
                continue
        if not caption_box:
            raise RuntimeError("Could not find TikTok caption box.")

        caption_box.click()
        # TikTok prepopulates filename → clear it.
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.type(caption, delay=15)
        log.info("[tiktok] caption entered")

        # Wait until the video finished server-side processing (Post button enabled).
        post_button = None
        for label in ("Post", "Publish", "Posting", "Upload"):
            try:
                post_button = page.get_by_role("button", name=label, exact=False)
                if post_button.count() > 0:
                    break
            except PWTimeout:
                continue
        if not post_button or post_button.count() == 0:
            raise RuntimeError("Could not find TikTok Post button.")

        # Wait for it to become enabled (video processed).
        for _ in range(120):
            if post_button.first.is_enabled():
                break
            time.sleep(2)
        else:
            raise TimeoutError("TikTok Post button never became enabled.")

        post_button.first.click()
        log.info("[tiktok] clicked Post; waiting for confirmation")

        # Confirmation: navigation to /tiktokstudio/content or a toast.
        try:
            page.wait_for_url("**/tiktokstudio/content**", timeout=120_000)
        except PWTimeout:
            log.warning("[tiktok] no content-page redirect; trusting upload anyway")

        context.storage_state(path=str(config.tiktok_storage_state))
        browser.close()

    return {"id": "", "url": "https://www.tiktok.com/tiktokstudio/content"}


def _cli() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    parser = argparse.ArgumentParser(prog="distributors.tiktok")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Open browser and save TikTok cookies.")
    p_post = sub.add_parser("post", help="Upload a video.")
    p_post.add_argument("video", type=Path)
    p_post.add_argument("caption")

    args = parser.parse_args()
    if args.cmd == "login":
        login()
        return 0
    if args.cmd == "post":
        result = post(args.video, args.caption)
        print(result)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
