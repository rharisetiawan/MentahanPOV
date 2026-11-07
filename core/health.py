"""Health checks for every external integration this pipeline depends on.

One shared place for "is this about to break" logic, used by all three
surfaces that need it: the Telegram /status command, the daily proactive
job, and dashboard.py. Never returns actual credential values — only
ok/warning/broken plus a human-readable reason, since this data is also
what an internet-exposed dashboard shows.

Google (Drive/YouTube) doesn't expose a "days until this refresh token
expires" API for Testing-mode OAuth apps, so those checks can only report
"works right now" vs "broken right now" — not a countdown. Meta (Graph
API) and Threads DO expose real expiry via debug_token, so those checks
give an actual days-remaining number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import config
from core import cleanup, google_auth, threads_token

# Import lazily inside functions where the module has heavier deps
# (google-genai) so a health check on one integration never fails because
# an unrelated one's import is broken.


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warning" | "error"
    detail: str

    @property
    def emoji(self) -> str:
        return {"ok": "✅", "warning": "⚠️", "error": "❌"}[self.status]


def _google_token(*, label: str, token_file: Path, scopes: list[str]) -> CheckResult:
    if not token_file.exists():
        return CheckResult(label, "error", "Token file not found — never authenticated.")
    try:
        google_auth.get_credentials(
            token_path=token_file,
            secrets_path=config.youtube_client_secrets,
            scopes=scopes,
        )
        return _google_age_result(label, token_file)
    except Exception as exc:  # noqa: BLE001 — report every failure mode the same way
        msg = str(exc)
        if "invalid_grant" in msg or "expired" in msg.lower() or "revoked" in msg.lower():
            return CheckResult(
                label,
                "error",
                "Token expired/revoked — needs re-authentication "
                "(see DEPLOYMENT.md → 'Re-authenticating Google OAuth').",
            )
        return CheckResult(label, "error", f"{type(exc).__name__}: {msg[:200]}")


def _google_age_result(label: str, token_file: Path) -> CheckResult:
    """The "works right now" result, plus a countdown when we can compute one.

    Google can't be asked when a Testing-mode refresh token dies, but it
    always dies a fixed number of days after the original consent — and
    core/google_auth.py records when that was ("granted_at"). So a token we
    know the age of gets a real "N days left" and a warning *before* the
    outage instead of only after. Tokens that predate that bookkeeping just
    get the plain message until they're next re-authenticated.
    """
    ok = "Refreshes fine right now."
    max_age = config.google_token_max_age_days
    granted = google_auth.read_granted_at(token_file)
    if granted is None or max_age <= 0:
        return CheckResult(label, "ok", ok)

    age_days = (datetime.now(timezone.utc) - granted).total_seconds() / 86400
    left = max_age - age_days
    if left <= 0:
        return CheckResult(
            label,
            "ok",
            f"{ok} Token is {age_days:.0f} days old and still works — the app "
            f"is probably in Production now; set GOOGLE_TOKEN_MAX_AGE_DAYS=0.",
        )
    if left <= 1.5:
        return CheckResult(
            label,
            "warning",
            f"Works now, but the token is {age_days:.1f} days old and a "
            f"Testing-mode app loses it ~{max_age} days after consent "
            f"(~{left:.1f} days left). Re-authenticate soon: "
            "python scripts/reauth_google.py",
        )
    return CheckResult(
        label,
        "ok",
        f"{ok} Token is {age_days:.1f} days old (~{left:.1f} days left "
        "if the app is still in Testing).",
    )


def check_gdrive() -> CheckResult:
    return _google_token(
        label="Google Drive",
        token_file=config.gdrive_token_file,
        scopes=["https://www.googleapis.com/auth/drive"],
    )


def check_youtube() -> CheckResult:
    return _google_token(
        label="YouTube",
        token_file=config.youtube_token_file,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )


def _debug_token(*, label: str, host: str, version: str, token: str) -> CheckResult:
    if not token:
        return CheckResult(label, "error", "No access token configured.")
    try:
        resp = requests.get(
            f"https://{host}/{version}/debug_token",
            params={"input_token": token, "access_token": token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
    except requests.RequestException as exc:
        # Never interpolate `exc` directly here: requests embeds the full
        # request URL (including the `input_token`/`access_token` query
        # params, i.e. the live secret) in HTTPError/ConnectionError
        # messages, and this detail string is displayed on the dashboard,
        # in /status, and broadcast to Telegram.
        http_status = getattr(exc.response, "status_code", None)
        detail = f"HTTP {http_status}" if http_status is not None else type(exc).__name__
        return CheckResult(label, "error", f"Couldn't reach Graph API ({detail}).")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(label, "error", f"Couldn't reach Graph API ({type(exc).__name__}).")

    if not data.get("is_valid"):
        return CheckResult(label, "error", "Token reports invalid — needs a new one.")

    expires_at = data.get("expires_at", 0)
    if not expires_at:
        return CheckResult(label, "ok", "Valid, no expiry set.")

    days_left = (expires_at - time.time()) / 86400
    if days_left <= 0:
        return CheckResult(label, "error", "Token already expired.")
    if days_left <= config.token_warn_days:
        return CheckResult(
            label, "warning", f"Expires in {days_left:.1f} days — refresh it soon."
        )
    return CheckResult(label, "ok", f"Valid for {days_left:.0f} more days.")


def check_facebook_instagram() -> CheckResult:
    # One Page Access Token backs both distributors/facebook.py and
    # instagram.py, so one check covers both.
    return _debug_token(
        label="Facebook/Instagram",
        host="graph.facebook.com",
        version=config.graph_api_version,
        token=config.fb_page_access_token,
    )


def check_threads() -> CheckResult:
    return _debug_token(
        label="Threads",
        host="graph.threads.net",
        version=config.threads_api_version,
        token=threads_token.current(),
    )


def _check_one_gemini_model(model: str, label: str) -> CheckResult:
    if not model:
        return CheckResult(label, "ok", "Not configured (disabled).")
    if not config.gemini_api_key:
        return CheckResult(label, "error", "GEMINI_API_KEY is empty.")
    try:
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=config.gemini_api_key)
        client.models.generate_content(
            model=model,
            contents=["Reply with exactly one word: OK"],
            config=genai_types.GenerateContentConfig(temperature=0),
        )
        return CheckResult(label, "ok", f"{model} responds.")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "404" in msg or "NOT_FOUND" in msg:
            return CheckResult(
                label,
                "error",
                f"{model} is retired (404) — pick a live replacement, "
                "see the comment above gemini_fallback_model in config.py.",
            )
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            return CheckResult(label, "warning", f"{model} is rate-limited right now.")
        if "503" in msg or "UNAVAILABLE" in msg:
            return CheckResult(label, "warning", f"{model} is over capacity right now.")
        return CheckResult(label, "error", f"{model}: {type(exc).__name__}: {msg[:150]}")


def check_gemini_primary() -> CheckResult:
    return _check_one_gemini_model(config.gemini_model, "Gemini (primary)")


def check_gemini_fallback() -> CheckResult:
    return _check_one_gemini_model(config.gemini_fallback_model, "Gemini (fallback)")


def check_disk_space() -> CheckResult:
    """How full the disk is, what's using it, and how much cleanup can give back.

    The bot (telegram_bot.py) cleans up by itself — see core/cleanup.py — so
    a low reading here normally means "cleanup already ran and what's left is
    unfinished videos" (kept for retry) or things outside this project; the
    detail says so explicitly. Status thresholds come from
    DISK_WARN_FREE_PCT / DISK_ERROR_FREE_PCT (also what the 6-hourly Telegram
    disk alert uses), so /status and that alert can never disagree.
    """
    import shutil

    try:
        usage = shutil.disk_usage(config.state_file.parent or Path("."))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Disk space", "error", f"Couldn't check: {exc}")

    free_pct = usage.free / usage.total * 100
    free_gb = usage.free / (1024**3)
    head = f"{free_gb:.1f}GB free ({free_pct:.0f}%)"

    breakdown = ""
    try:
        # The read-only dashboard container doesn't mount incoming/ (see
        # docker-compose.yml) — reporting "incoming 0 MB, 0 reclaimable"
        # from there would be actively misleading, so say nothing instead.
        if not cleanup.INCOMING_DIR.exists():
            raise FileNotFoundError(cleanup.INCOMING_DIR)
        incoming = cleanup.dir_size(cleanup.INCOMING_DIR)
        copies = cleanup.dir_size(cleanup.posting_copy_root())
        report = cleanup.assess(pressure=False)
        breakdown = (
            f" incoming {cleanup.fmt_bytes(incoming)}, "
            f"posting copies {cleanup.fmt_bytes(copies)}; "
            f"{cleanup.fmt_bytes(report.total_bytes)} reclaimable now"
        )
        if report.held_back_videos:
            breakdown += (
                f", {report.held_back_videos} unfinished video(s) "
                f"({cleanup.fmt_bytes(report.held_back_bytes)}) kept for retry"
            )
        if report.orphan_files:
            breakdown += (
                f", {report.orphan_files} untracked file(s) "
                f"({cleanup.fmt_bytes(report.orphan_bytes)}) to inspect by hand"
            )
        breakdown += "."
    except Exception:  # noqa: BLE001 — a breakdown must never hide the free-space figure
        breakdown = ""

    if free_pct < config.disk_error_free_pct:
        return CheckResult(
            "Disk space",
            "error",
            f"Only {head} — run cleanup / free space now.{breakdown}",
        )
    if free_pct < config.disk_warn_free_pct:
        return CheckResult("Disk space", "warning", f"{head} — getting low.{breakdown}")
    return CheckResult("Disk space", "ok", f"{head}.{breakdown}")


ALL_CHECKS = [
    check_gdrive,
    check_youtube,
    check_facebook_instagram,
    check_threads,
    check_gemini_primary,
    check_gemini_fallback,
    check_disk_space,
]


def run_all() -> list[CheckResult]:
    return [check() for check in ALL_CHECKS]
