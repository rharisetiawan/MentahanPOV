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
from pathlib import Path

import requests

from config import config
from core import google_auth

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
        return CheckResult(label, "ok", "Refreshes fine right now.")
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
    except Exception as exc:  # noqa: BLE001
        return CheckResult(label, "error", f"Couldn't reach Graph API: {exc}")

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
        token=config.threads_access_token,
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
    """incoming/ and state/posting_copies/ only ever grow — nothing in this
    pipeline deletes old videos automatically. On a board with a small SD
    card/eMMC, that's the quiet way a run eventually fails with ENOSPC.
    """
    import shutil

    try:
        usage = shutil.disk_usage(config.state_file.parent or Path("."))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Disk space", "error", f"Couldn't check: {exc}")

    free_pct = usage.free / usage.total * 100
    free_gb = usage.free / (1024**3)
    if free_pct < 10:
        return CheckResult(
            "Disk space", "error", f"Only {free_gb:.1f}GB free ({free_pct:.0f}%)."
        )
    if free_pct < 20:
        return CheckResult(
            "Disk space", "warning", f"{free_gb:.1f}GB free ({free_pct:.0f}%) — getting low."
        )
    return CheckResult("Disk space", "ok", f"{free_gb:.1f}GB free ({free_pct:.0f}%).")


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
