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
    # GDrive
    gdrive_sa_json: Path = field(
        default_factory=lambda: Path(
            _env("GDRIVE_SERVICE_ACCOUNT_JSON", "./credentials/gdrive-sa.json")
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

    # Reverse geocoding (GPS -> human-readable address)
    google_maps_api_key: str = field(
        default_factory=lambda: _env("GOOGLE_MAPS_API_KEY")
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

    # Orchestration
    platforms: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            p.strip()
            for p in _env("PLATFORMS", "youtube,facebook,instagram,tiktok").split(",")
            if p.strip()
        )
    )
    state_file: Path = field(
        default_factory=lambda: Path(_env("STATE_FILE", "./state/posts.json"))
    )


config = Config()
