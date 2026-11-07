"""Sign in with Google for the admin dashboard (OpenID Connect, code flow + PKCE).

This dashboard can rewrite every credential the pipeline uses, so the rules
are strict: it stays off unless fully configured, and only a verified address
listed in ADMIN_GOOGLE_EMAILS gets in. Having a Google account is not enough.

Standard library only for the HTTP call: the admin venv is deliberately small.
The ID token's signature isn't checked because it comes straight from Google's
token endpoint over TLS, which OpenID Connect Core 3.1.3.7 allows; its claims
still are.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/auth/google/callback"
_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class LoginError(Exception):
    """Why this login was refused. Safe to show: never holds a secret."""


def allowed_emails(raw: str) -> set[str]:
    return {e.strip().casefold() for e in (raw or "").split(",") if e.strip()}


def is_enabled(cfg) -> bool:
    return bool(
        cfg.admin_google_client_id
        and cfg.admin_google_client_secret
        and cfg.admin_public_url
        and allowed_emails(cfg.admin_google_emails)
    )


def redirect_uri(cfg) -> str:
    return cfg.admin_public_url.rstrip("/") + CALLBACK_PATH


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def begin(cfg) -> tuple[str, dict[str, str]]:
    """The URL to send the browser to, and what to remember in the session."""
    pending = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": secrets.token_urlsafe(48),
    }
    challenge = _b64url(hashlib.sha256(pending["verifier"].encode("ascii")).digest())
    query = urllib.parse.urlencode(
        {
            "client_id": cfg.admin_google_client_id,
            "redirect_uri": redirect_uri(cfg),
            "response_type": "code",
            "scope": "openid email",
            "state": pending["state"],
            "nonce": pending["nonce"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return f"{AUTH_URL}?{query}", pending


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError) as exc:
        raise LoginError("Google mengirim token yang tidak bisa dibaca.") from exc
    if not isinstance(claims, dict):
        raise LoginError("Google mengirim token yang tidak bisa dibaca.")
    return claims


def verified_email(claims: dict, cfg, nonce: str, *, now: float | None = None) -> str:
    """The allowed address these claims prove, or LoginError."""
    now = time.time() if now is None else now
    if claims.get("iss") not in _ISSUERS:
        raise LoginError("Token bukan dari Google.")
    if claims.get("aud") != cfg.admin_google_client_id:
        raise LoginError("Token bukan untuk aplikasi ini.")
    expires = claims.get("exp")
    if not isinstance(expires, (int, float)) or expires <= now:
        raise LoginError("Token sudah kedaluwarsa, coba masuk lagi.")
    if not nonce or claims.get("nonce") != nonce:
        raise LoginError("Sesi login tidak cocok, coba masuk lagi.")
    email = str(claims.get("email") or "").casefold()
    if not email or claims.get("email_verified") is not True:
        raise LoginError("Email Google belum terverifikasi.")
    if email not in allowed_emails(cfg.admin_google_emails):
        raise LoginError(f"{email} tidak punya akses ke dashboard ini.")
    return email


def exchange_code(code: str, verifier: str, cfg) -> dict:
    """Trade the callback's code for the ID token's claims."""
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": cfg.admin_google_client_id,
            "client_secret": cfg.admin_google_client_secret,
            "redirect_uri": redirect_uri(cfg),
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    ).encode("ascii")
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:  # noqa: S310 - fixed https URL
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LoginError(f"Google menolak login (HTTP {exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LoginError(f"Gagal menghubungi Google ({type(exc).__name__}).") from None
    id_token = data.get("id_token") if isinstance(data, dict) else None
    if not id_token:
        raise LoginError("Google tidak mengirim ID token.")
    return _claims(id_token)
