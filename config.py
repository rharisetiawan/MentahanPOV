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


# Platforms whose code now lives in archive/ (see archive/tiktok/README.md).
# Still dropped from PLATFORMS/TELEGRAM_PLATFORMS so an older .env that
# lists them doesn't make every run report an "unknown platform".
ARCHIVED_PLATFORMS = frozenset({"tiktok"})


def _drop_disabled(platforms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p for p in platforms if p not in ARCHIVED_PLATFORMS)


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
    # Tried only after GEMINI_MODEL exhausts its own retries on a 503
    # "high demand" error — a different model's capacity pool is often
    # less contested, especially right after a new model's release draws
    # a demand spike. Set empty to disable and just fail after GEMINI_MODEL
    # gives up.
    #
    # Google retires model names with little warning (gemini-2.0-flash and
    # even gemini-2.5-flash-lite both started 404ing "no longer available"
    # sometime after this project's own knowledge cutoff) — if this starts
    # 404ing, don't guess a replacement name from memory; list what's
    # actually live first:
    #   python -c "from google import genai; from config import config; \
    #     [print(m.name) for m in genai.Client(api_key=config.gemini_api_key).models.list() \
    #      if 'generateContent' in (m.supported_actions or [])]"
    # ...and confirm the replacement actually accepts video input before
    # trusting it, since retired models can still show up in that list:
    #   client.models.generate_content(model=NAME, contents=[uploaded_file, "test"])
    gemini_fallback_model: str = field(
        default_factory=lambda: _env("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
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
        default_factory=lambda: float(_env("WATERMARK_WIDTH_PCT", "0.18"))
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

    # Threads (separate product from the Facebook/Instagram Graph API above —
    # different host, different OAuth flow, different token. See README →
    # "Threads" for how to get threads_user_id / threads_access_token.)
    threads_user_id: str = field(default_factory=lambda: _env("THREADS_USER_ID"))
    threads_access_token: str = field(
        default_factory=lambda: _env("THREADS_ACCESS_TOKEN")
    )
    threads_api_version: str = field(
        default_factory=lambda: _env("THREADS_API_VERSION", "v1.0")
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
        default_factory=lambda: _drop_disabled(
            tuple(
                p.strip()
                for p in _env("TELEGRAM_PLATFORMS", "").split(",")
                if p.strip()
            )
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
        default_factory=lambda: _drop_disabled(
            tuple(
                p.strip()
                for p in _env(
                    "PLATFORMS",
                    # Threads is opt-in: it needs its own separately-issued
                    # access token — listing it before that setup is done
                    # makes every run report a failure. Add "threads" here (or
                    # to TELEGRAM_PLATFORMS) once THREADS_USER_ID/ACCESS_TOKEN
                    # are set.
                    "youtube,facebook,instagram,facebook_story,instagram_story",
                ).split(",")
                if p.strip()
            )
        )
    )
    state_file: Path = field(
        default_factory=lambda: Path(_env("STATE_FILE", "./state/posts.json"))
    )
    # (CLEANUP_RETENTION_DAYS, CLEANUP_IGNORE_PLATFORMS and the DISK_*_FREE_PCT
    # alert thresholds live in the "Automatic disk cleanup" block further down.)
    #
    # The bot (telegram_bot.py) runs the same cleanup itself — after every
    # run, once a day at CLEANUP_HOUR (WIB), and immediately when free disk
    # drops under DISK_PRESSURE_FREE_GB — so it doesn't depend on a host cron
    # entry that, on a box that goes offline for weeks, may simply not exist.
    # Under pressure the retention window is skipped; every other safety
    # condition in core/cleanup.py still applies.
    auto_cleanup: bool = field(
        default_factory=lambda: _env("AUTO_CLEANUP", "true").lower() != "false"
    )
    cleanup_hour: int = field(default_factory=lambda: int(_env("CLEANUP_HOUR", "4")))
    # A video that was dry-run (master already on Drive) but never posted,
    # scheduled, or cancelled can be deleted locally after this many days —
    # what ❌ Batal would have done. OFF by default (0): replaying the box's
    # real posts.json showed it would delete a file the 2026-10-04 cleanup had
    # deliberately held back for the owner to decide on, and a new feature must
    # not silently delete things someone already chose to keep. Opt in with e.g. 7.
    abandoned_retention_days: int = field(
        default_factory=lambda: int(_env("ABANDONED_RETENTION_DAYS", "0"))
    )
    disk_pressure_free_gb: float = field(
        default_factory=lambda: float(_env("DISK_PRESSURE_FREE_GB", "2"))
    )
    # A new video is refused (with an explanation) instead of half-downloaded
    # when free space, after a pressure cleanup, is below its size plus this.
    upload_min_free_mb: int = field(
        default_factory=lambda: int(_env("UPLOAD_MIN_FREE_MB", "1024"))
    )

    # Liveness. The bot rewrites state/heartbeat.json every
    # HEARTBEAT_INTERVAL_MIN minutes (read back on startup to report how long
    # it was down, and by dashboard.py's /heartbeat for scripts/watchdog.py),
    # and — if set — GETs HEARTBEAT_PING_URL on the same beat (e.g. a
    # healthchecks.io check, which alerts you when the pings *stop*; that's
    # the only kind of signal that still works when the whole box is dead).
    heartbeat_interval_min: int = field(
        default_factory=lambda: int(_env("HEARTBEAT_INTERVAL_MIN", "5"))
    )
    heartbeat_ping_url: str = field(default_factory=lambda: _env("HEARTBEAT_PING_URL"))

    # Google OAuth apps whose consent screen is still in "Testing" get
    # refresh tokens that die a fixed 7 days after the original grant (see
    # DEPLOYMENT.md -> "Google OAuth"). core/health.py uses this to warn a
    # day or two *before* that happens, when it knows when the token was
    # granted. 0 disables the countdown (use once the app is in Production).
    google_token_max_age_days: int = field(
        default_factory=lambda: int(_env("GOOGLE_TOKEN_MAX_AGE_DAYS", "7"))
    )

    # Post performance (core/insights.py). YouTube view counts need a plain
    # API key — the youtube.upload OAuth scope this project holds can't read
    # statistics. Everything else reuses the Meta/Threads tokens above.
    youtube_api_key: str = field(default_factory=lambda: _env("YOUTUBE_API_KEY"))
    insights_enabled: bool = field(
        default_factory=lambda: _env("INSIGHTS_ENABLED", "true").lower() != "false"
    )
    # How many of the best-performing past hooks get shown to Gemini as style
    # reference, and how many posts must have performance data before any
    # are shown at all (a handful of posts is noise, not a signal). 0 = off.
    caption_top_hooks: int = field(
        default_factory=lambda: int(_env("CAPTION_TOP_HOOKS", "3"))
    )
    insights_min_samples: int = field(
        default_factory=lambda: int(_env("INSIGHTS_MIN_SAMPLES", "5"))
    )

    # Scheduled posting (the 📅 Jadwalkan button). Hours (0-23, WIB) offered
    # as "next slot" buttons — whichever upcoming ones are soonest.
    schedule_slot_hours: tuple[int, ...] = field(
        default_factory=lambda: tuple(
            int(h)
            for h in _env("SCHEDULE_SLOT_HOURS", "7,12,19").split(",")
            if h.strip().isdigit() and 0 <= int(h) <= 23
        )
    )

    # Status dashboard (dashboard.py) + Telegram /status and the daily
    # health-check job (see telegram_bot.py). Read-only — never displays
    # actual credential values, only ok/expiring/broken per integration.
    dashboard_user: str = field(
        default_factory=lambda: _env("DASHBOARD_USER", "admin")
    )
    dashboard_password: str = field(
        default_factory=lambda: _env("DASHBOARD_PASSWORD")
    )
    dashboard_port: int = field(
        default_factory=lambda: int(_env("DASHBOARD_PORT", "8090"))
    )
    # Meta/Threads tokens are flagged in /status and the dashboard once
    # they're within this many days of their real expiry (queried live via
    # Graph API's debug_token — Meta actually tells us, unlike Google).
    token_warn_days: int = field(
        default_factory=lambda: int(_env("TOKEN_WARN_DAYS", "7"))
    )
    # Hour (0-23, server local time) the daily Telegram status report goes
    # out. See telegram_bot.py's job_queue.run_daily.
    daily_status_hour: int = field(
        default_factory=lambda: int(_env("DAILY_STATUS_HOUR", "8"))
    )

    # Automatic disk cleanup (core/cleanup.py): finished jobs older than this
    # many days lose their master in incoming/ and their posting copies.
    cleanup_retention_days: int = field(
        default_factory=lambda: int(_env("CLEANUP_RETENTION_DAYS", "3"))
    )
    # Platforms whose "error" does NOT block cleanup. TikTok is a manual
    # hand-off, so its status never means "still needs a retry".
    cleanup_ignore_platforms: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            p.strip()
            for p in _env("CLEANUP_IGNORE_PLATFORMS", "tiktok").split(",")
            if p.strip()
        )
    )
    # Disk-space alert thresholds (% free) used by core/health.py and the
    # daily Telegram report. The partition is small, so warn early.
    disk_warn_free_pct: float = field(
        default_factory=lambda: float(_env("DISK_WARN_FREE_PCT", "25"))
    )
    disk_error_free_pct: float = field(
        default_factory=lambda: float(_env("DISK_ERROR_FREE_PCT", "12"))
    )

    # Admin dashboard (admin.py) — the read-write sibling of dashboard.py
    # above: edits .env/credentials and drives docker compose. Runs as its
    # own host process (not in the mentahanpov-app image — it needs to
    # write files the read-only container's mounts deliberately can't,
    # and to shell out to `docker compose`), on its own port so it never
    # collides with the read-only dashboard. Reuses DASHBOARD_USER/
    # DASHBOARD_PASSWORD above rather than a second credential.
    admin_port: int = field(default_factory=lambda: int(_env("ADMIN_PORT", "8091")))
    # Full URL telegram_bot.py's /dashboard command replies with (e.g.
    # "http://100.73.125.7:8091", the box's Tailscale IP) — a Tailscale IP
    # rather than the LAN one so the link still works from outside the
    # house. Left empty, /dashboard just points at DEPLOYMENT.md instead
    # of guessing a URL that might not resolve for whoever's reading it.
    admin_dashboard_url: str = field(
        default_factory=lambda: _env("ADMIN_DASHBOARD_URL")
    )


config = Config()
