"""YouTube Shorts uploader (YouTube Data API v3, OAuth2 installed-app flow).

For a video to be treated as a Short, YouTube currently requires:
- aspect ratio 9:16 (or square)
- duration <= 60 seconds
- the tag/hashtag `#Shorts` somewhere in the title or description

This module appends `#Shorts` automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
PLATFORM = "youtube"


def _get_creds() -> Credentials:
    creds: Credentials | None = None
    token_path = config.youtube_token_file
    secrets_path = config.youtube_client_secrets

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not secrets_path.exists():
            raise FileNotFoundError(
                f"YouTube client secrets not found at {secrets_path}. "
                "See README → 'YouTube setup'."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
        # Opens browser for first-time auth, then caches token.
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service() -> Any:
    return build("youtube", "v3", credentials=_get_creds(), cache_discovery=False)


def _shortsify(title: str, description: str) -> tuple[str, str]:
    if "#shorts" not in title.lower() and "#shorts" not in description.lower():
        description = (description.rstrip() + "\n\n#Shorts").strip()
    # YouTube hard-caps title at 100 chars.
    return title[:100], description[:5000]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def post(video_path: Path, caption: str, **_: Any) -> dict[str, str]:
    title_line, *body = caption.strip().splitlines()
    title = title_line.strip() or video_path.stem
    description = "\n".join(body).strip() or caption.strip()
    title, description = _shortsify(title, description)

    body_payload = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": config.youtube_category_id,
        },
        "status": {
            "privacyStatus": config.youtube_privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    log.info("[youtube] uploading %s as %s", video_path.name, config.youtube_privacy)
    media = MediaFileUpload(
        str(video_path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/*"
    )
    request = (
        _service()
        .videos()
        .insert(part="snippet,status", body=body_payload, media_body=media)
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("[youtube] %.0f%%", status.progress() * 100)

    video_id = response["id"]
    return {"id": video_id, "url": f"https://youtube.com/shorts/{video_id}"}
