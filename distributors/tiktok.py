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
import platform
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from config import config

log = logging.getLogger(__name__)
PLATFORM = "tiktok"

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=upload"
LOGIN_URL = "https://www.tiktok.com/login"

# Buttons seen on TikTok Studio's own interstitials so far ("Turn on
# automatic content checks?", "New editing features added", plus common
# variants of the same idea). TikTok A/B tests and redesigns these without
# notice, so this list is expected to need new entries over time — if the
# caption-box-empty error below recurs with a fresh debug screenshot
# showing an unlisted popup, add its button label here rather than assume
# the read-back check itself regressed.
_DISMISS_LABEL_RE = re.compile(
    r"^(Cancel|Got it|Not now|Not Now|Skip|OK|Dismiss|Close|Maybe later|No thanks)$",
    re.IGNORECASE,
)


class TikTokPostError(RuntimeError):
    """A failure with a page screenshot attached.

    worker_service.py ships `screenshot_path`'s bytes back to the HG680 with
    the error, and main.py (when TikTok runs locally) parks it for the bot —
    so "what popup was in the way" reaches the Telegram chat instead of
    sitting in a temp dir on whichever machine happened to run Playwright.
    """

    def __init__(self, message: str, *, screenshot_path: Path | None = None) -> None:
        super().__init__(message)
        self.screenshot_path = screenshot_path


def _save_screenshot(page: Any, name: str) -> Path | None:
    # tempfile.gettempdir(), not a literal /tmp: the worker can be a Windows
    # machine, where Path("/tmp") silently fails to exist.
    path = Path(tempfile.gettempdir()) / name
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:  # noqa: BLE001 — never let debugging mask the real error
        log.exception("[tiktok] also failed to save debug screenshot")
        return None
    return path


def _ensure_storage_dir() -> None:
    config.tiktok_storage_state.parent.mkdir(parents=True, exist_ok=True)


def _dismiss_upload_popups(page: Any) -> None:
    """Best-effort dismissal of interstitials TikTok shows right after a
    video upload — first confirmed 2026-09-04 via a debug screenshot, then
    seen recurring 2026-09-10 with a symptom consistent with a different,
    unlisted popup variant. TikTok keeps rotating which one shows (A/B
    tests, redesigns), so this matches a broad set of common dismiss-button
    labels via one regex (see _DISMISS_LABEL_RE) rather than a couple of
    exact strings, plus an Escape-key fallback for modals with no
    recognizable button at all. Any of these being absent is the normal
    case, not an error.
    """
    try:
        page.get_by_role("button", name=_DISMISS_LABEL_RE).first.click(timeout=1_500)
        log.info("[tiktok] dismissed a popup")
    except Exception:  # noqa: BLE001 — no matching popup is the common case
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001 — best-effort only
        pass


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

        _dismiss_upload_popups(page)

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

        # force=True: right after upload, TikTok shows a "We'll
        # automatically check if your video has any unoriginal content"
        # tooltip that overlaps the caption box and fails Playwright's
        # pointer-interception check even though it doesn't actually
        # block a real click — confirmed 2026-09-04 via a debug
        # screenshot, the caption box itself was visible/enabled/stable
        # on every retry, only the interception check kept failing.
        try:
            caption_box.click(force=True)
        except Exception as exc:  # noqa: BLE001 — re-raised below, with a screenshot
            shot = _save_screenshot(page, "tiktok-caption-box-debug.png")
            log.error("[tiktok] caption box click failed, screenshot: %s", shot)
            first_line = (str(exc).splitlines() or [type(exc).__name__])[0]
            raise TikTokPostError(
                f"Caption box click failed: {first_line}", screenshot_path=shot
            ) from exc
        # TikTok prepopulates filename → clear it.
        _mod = "Meta" if platform.system() == "Darwin" else "Control"
        page.keyboard.press(f"{_mod}+A")
        page.keyboard.press("Delete")
        page.keyboard.type(caption, delay=15)

        # Confirmed 2026-09-05: this used to post successfully with an
        # EMPTY caption and no exception anywhere — a popup grabbing focus
        # right after the click (see force=True above) meant the keystrokes
        # above landed nowhere, and page.keyboard.type() has no way to
        # notice that on its own. Read the box back and fail loudly instead
        # of silently publishing a blank caption — a real error here is
        # infinitely easier to fix than a wrong post already live on TikTok.
        actual = (caption_box.inner_text() or "").strip()
        expected = caption.strip()
        if len(actual) < min(10, len(expected)):
            debug_path = _save_screenshot(page, "tiktok-caption-empty-debug.png")
            raise TikTokPostError(
                f"Caption box has {len(actual)} chars after typing, expected "
                f"~{len(expected)} — it almost certainly didn't land (a popup "
                f"probably stole focus). Screenshot: {debug_path}",
                screenshot_path=debug_path,
            )
        log.info("[tiktok] caption entered (%d chars)", len(actual))

        # The Post button doesn't exist in the DOM until TikTok's upload +
        # processing finishes (confirmed 2026-09-04: a one-shot check right
        # after typing the caption found nothing, because the video was
        # still mid-upload behind a progress-bar overlay) — so discovery
        # itself has to retry over time, same as the "wait for enabled"
        # step below it. Re-dismiss popups each iteration too: TikTok can
        # show the content-checks modal again after the caption is typed.
        post_button = None
        for _ in range(120):  # ~4 minutes
            _dismiss_upload_popups(page)
            for label in ("Post", "Publish", "Posting", "Upload"):
                candidate = page.get_by_role("button", name=label, exact=False)
                if candidate.count() > 0:
                    post_button = candidate
                    break
            if post_button:
                break
            time.sleep(2)
        if not post_button:
            debug_path = _save_screenshot(page, "tiktok-no-post-button-debug.png")
            try:
                all_buttons = page.get_by_role("button").all_text_contents()
                log.error(
                    "[tiktok] no Post button found; visible button texts: %r; "
                    "screenshot saved to %s",
                    all_buttons,
                    debug_path,
                )
            except Exception:  # noqa: BLE001 — never let debugging mask the real error
                log.exception("[tiktok] couldn't list the visible buttons")
            raise TikTokPostError(
                "Could not find TikTok Post button.", screenshot_path=debug_path
            )

        # Wait for it to become enabled (video processed).
        for _ in range(120):
            if post_button.first.is_enabled():
                break
            time.sleep(2)
        else:
            raise TimeoutError("TikTok Post button never became enabled.")

        # force=True here too — the same post-upload tooltip (see above)
        # can still be up by the time this button is clicked.
        post_button.first.click(force=True)
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
