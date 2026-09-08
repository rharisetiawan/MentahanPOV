"""Shared OAuth2 installed-app credential flow for Google APIs.

core/gdrive.py and distributors/youtube.py both authenticate as the same
human Google account (not a service account — service accounts have zero
storage quota on regular "My Drive", see gdrive.py's docstring) through the
same installed-app OAuth client, just with different scopes and cached-token
paths. This is the one place that flow is implemented, so both callers stay
in sync automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

log = logging.getLogger(__name__)


def get_credentials(
    *, token_path: Path, secrets_path: Path, scopes: list[str]
) -> Credentials:
    """Load cached credentials for `scopes`, refreshing or consenting as needed.

    `token_path` is where the resulting credentials are cached after first
    use (per-scope — Drive and YouTube use separate token files even though
    they share `secrets_path`, since a token is only valid for the scopes it
    was issued with).
    """
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not secrets_path.exists():
            raise FileNotFoundError(
                f"OAuth client secrets not found at {secrets_path}. "
                "See README → 'Google Drive setup' / 'YouTube setup'."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), scopes)
        # prompt=select_account forces the account chooser instead of
        # silently reusing whichever session already consented to this
        # client+scope, which isn't necessarily the account you want (e.g.
        # the one that owns the target Drive folder).
        creds = flow.run_local_server(
            port=0, prompt="select_account", open_browser=False
        )

    # A freshly-refreshed access token is only worth caching if this
    # process can actually write it back — dashboard.py and core/health.py
    # deliberately mount credentials/ read-only (they're read-only status
    # viewers, not credential managers), so failing to persist here is
    # expected there, not a real problem: the creds object in hand is
    # still valid for this call, and the container that owns write access
    # will cache its own refresh normally on its next real use.
    try:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    except OSError:
        log.warning(
            "[google_auth] couldn't cache refreshed token to %s (read-only mount?); "
            "using it for this call without persisting it",
            token_path,
        )
    return creds
