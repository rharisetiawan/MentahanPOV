"""Centralised configuration loaded from environment variables.

Importing this module triggers `dotenv.load_dotenv()` so any script that
imports `config` automatically gets the .env values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val or ""


@dataclass(frozen=True)
class Config:
    # GDrive (OAuth2 installed-app — reuses the YouTube client secrets below,
    # since service accounts can't write to regular "My Drive" folders)
    gdrive_token_file: Path = field(
        default_factory=lambda: Path(
            _env("GDRIVE_TOKEN_FILE", "./credentials/gdrive-token.json")
        )
    )
    # ID of the MentahanPOV project ROOT folder (the one containing
    # "01 - Suasana Jalan & Perjalanan", "02 - Cuaca & Hujan", etc).
    gdrive_folder_id: str = field(default_factory=lambda: _env("GDRIVE_FOLDER_ID"))
    # Pipe-separated (folder names may contain commas). Must match the
    # actual subfolder names under GDRIVE_FOLDER_ID exactly.
    drive_categories: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            c.strip()
            for c in _env(
                "DRIVE_CATEGORIES",
                "01 - Suasana Jalan & Perjalanan|"
                "02 - Cuaca & Hujan|"
                "03 - Alam, Hewan & ASMR|"
                "04 - Raw Photos & Textures|"
                "05 - Timelapse Assets",
            ).split("|")
            if c.strip()
        )
    )

    # Gemini
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(
        default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.5-flash")
    )

    # Watermark / posting-copy rendering
    watermark_logo_path: Path = field(
        default_factory=lambda: Path(
            _env("WATERMARK_LOGO_PATH", "./assets/watermark-logo.png")
        )
    )
    watermark_opacity: float = field(
        default_factory=lambda: float(_env("WATERMARK_OPACITY", "0.35"))
    )
    # Fraction of the posting copy's width, not a pixel count: a fixed px
    # size would read as huge on a 720p clip and invisible on 4K.
    watermark_width_pct: float = field(
        default_factory=lambda: float(_env("WATERMARK_WIDTH_PCT", "0.26"))
    )
    posting_copy_dir: Path = field(
        default_factory=lambda: Path(
            _env("POSTING_COPY_DIR", "./state/posting_copies")
        )
    )

    # YouTube
    youtube_client_secrets: Path = field(
        default_factory=lambda: Path(
            _env("YOUTUBE_CLIENT_SECRETS", "./credentials/youtube-oauth.json")
        )
    )
    youtube_token_file: Path = field(
        default_factory=lambda: Path(
            _env("YOUTUBE_TOKEN_FILE", "./credentials/youtube-token.json")
        )
    )
    youtube_privacy: str = field(
        default_factory=lambda: _env("YOUTUBE_PRIVACY", "public")
    )
    youtube_category_id: str = field(
        default_factory=lambda: _env("YOUTUBE_CATEGORY_ID", "22")
    )

    # Facebook + Instagram
    fb_page_id: str = field(default_factory=lambda: _env("FB_PAGE_ID"))
    fb_page_access_token: str = field(
        default_factory=lambda: _env("FB_PAGE_ACCESS_TOKEN")
    )
    ig_user_id: str = field(default_factory=lambda: _env("IG_USER_ID"))
    # Only used to build a human-clickable story link in the run summary —
    # the Graph API doesn't return permalinks for stories.
    ig_username: str = field(default_factory=lambda: _env("IG_USERNAME"))
    graph_api_version: str = field(
        default_factory=lambda: _env("GRAPH_API_VERSION", "v19.0")
    )

    # TikTok
    tiktok_storage_state: Path = field(
        default_factory=lambda: Path(
            _env("TIKTOK_STORAGE_STATE", "./credentials/tiktok-storage.json")
        )
    )
    tiktok_headless: bool = field(
        default_factory=lambda: _env("TIKTOK_HEADLESS", "false").lower() == "true"
    )

    # Telegram bot front-end (see telegram_bot.py)
    telegram_bot_token: str = field(
        default_factory=lambda: _env("TELEGRAM_BOT_TOKEN")
    )
    # Comma-separated numeric Telegram user ids. Empty = anyone with the
    # bot's link can trigger it — set this unless you want that.
    telegram_allowed_user_ids: tuple[int, ...] = field(
        default_factory=lambda: tuple(
            int(u) for u in _env("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if u.strip()
        )
    )
    # Optional override of --platforms for bot-triggered runs. Empty = use
    # main.py's own PLATFORMS default.
    telegram_platforms: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            p.strip() for p in _env("TELEGRAM_PLATFORMS", "").split(",") if p.strip()
        )
    )
    # From https://my.telegram.org -> API development tools. Only needed to
    # run a Local Bot API Server (see README) — the default api.telegram.org
    # caps file downloads at 20MB, too small for raw phone footage.
    telegram_api_id: str = field(default_factory=lambda: _env("TELEGRAM_API_ID"))
    telegram_api_hash: str = field(default_factory=lambda: _env("TELEGRAM_API_HASH"))
    # Base URL of a running Local Bot API Server. Used automatically once
    # TELEGRAM_API_ID/HASH are set; override if it's not on localhost:8081.
    telegram_local_api_url: str = field(
        default_factory=lambda: _env("TELEGRAM_LOCAL_API_URL", "http://localhost:8081")
    )

    # Orchestration
    platforms: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            p.strip()
            for p in _env(
                "PLATFORMS",
                # TikTok is opt-in: it needs a one-off Playwright login, and
                # listing it before that makes every run report a failure.
                "youtube,facebook,instagram,facebook_story,instagram_story",
            ).split(",")
            if p.strip()
        )
    )
    state_file: Path = field(
        default_factory=lambda: Path(_env("STATE_FILE", "./state/posts.json"))
    )


config = Config()
