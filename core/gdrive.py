"""Google Drive uploader (OAuth2 installed-app flow).

Service accounts have zero personal storage quota on regular "My Drive"
folders (Google only lets them write into paid Shared Drives), so this
authenticates as the actual Drive owner instead — same pattern as
distributors/youtube.py, reusing the same OAuth client. First run opens a
browser for consent; the token is cached afterwards.

Uploads a single video file to a target folder, sets the file permission to
"anyone with the link can view", and returns the shareable URL.
"""

from __future__ import annotations

import logging
import mimetypes
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

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_creds() -> Credentials:
    creds: Credentials | None = None
    token_path = config.gdrive_token_file
    secrets_path = config.youtube_client_secrets  # same OAuth client, different scope

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not secrets_path.exists():
            raise FileNotFoundError(
                f"OAuth client secrets not found at {secrets_path}. "
                "See README → 'Google Drive setup'."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
        # prompt=select_account forces Google to show the account chooser
        # instead of silently reusing whichever session already has this
        # scope+client consented (which may not be the account that owns
        # the Drive folder).
        creds = flow.run_local_server(
            port=0, prompt="select_account", open_browser=False
        )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service() -> Any:
    return build("drive", "v3", credentials=_get_creds(), cache_discovery=False)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def resolve_category_folder_id(category_name: str) -> str:
    """Find the id of the subfolder named `category_name` under GDRIVE_FOLDER_ID."""
    if not config.gdrive_folder_id:
        raise RuntimeError(
            "GDRIVE_FOLDER_ID is empty. Set it to the MentahanPOV project folder ID."
        )
    svc = _service()
    safe_name = category_name.replace("'", "\\'")
    query = (
        f"'{config.gdrive_folder_id}' in parents "
        f"and name = '{safe_name}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    resp = svc.files().list(q=query, fields="files(id, name)").execute()
    files = resp.get("files", [])
    if not files:
        raise RuntimeError(
            f"Folder '{category_name}' not found under GDRIVE_FOLDER_ID. "
            "Check the folder exists and belongs to the authenticated account."
        )
    return files[0]["id"]


def direct_download_url(file_id: str) -> str:
    """A URL that serves the file bytes directly, for third-party fetchers.

    Instagram's Graph API downloads `video_url` itself, so it must receive
    actual video bytes. The classic `drive.google.com/uc?export=download`
    form only does that for small files: past ~100 MB Drive answers with an
    HTML "Google Drive can't scan this file for viruses" interstitial
    instead, and the platform fetch fails. The usercontent host with
    `confirm=t` skips that interstitial at any size.
    """
    return (
        "https://drive.usercontent.google.com/download"
        f"?id={file_id}&export=download&confirm=t"
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def resolve_or_create_folder(name: str, *, parent_id: str | None = None) -> str:
    """Return the id of folder `name`, creating it if absent.

    `parent_id` defaults to My Drive's root. Staging folders deliberately
    live there rather than inside GDRIVE_FOLDER_ID: that folder is the
    link-in-bio archive people browse, and a `_posting-temp` directory
    appearing in it would be visible to every visitor.
    """
    svc = _service()
    safe_name = name.replace("'", "\\'")
    parent = parent_id or "root"
    query = (
        f"'{parent}' in parents and name = '{safe_name}' "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    files = svc.files().list(q=query, fields="files(id)").execute().get("files", [])
    if files:
        return files[0]["id"]
    created = (
        svc.files()
        .create(
            body={
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent],
            },
            fields="id",
        )
        .execute()
    )
    log.info("[gdrive] created folder %s (%s)", name, created["id"])
    return created["id"]


def delete_file(file_id: str) -> None:
    """Best-effort delete; never raises (used for cleanup of temp uploads)."""
    try:
        _service().files().delete(fileId=file_id).execute()
        log.info("[gdrive] deleted temp file %s", file_id)
    except Exception as exc:  # noqa: BLE001 — cleanup must never break a run
        log.warning("[gdrive] could not delete temp file %s: %s", file_id, exc)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def upload_video(
    video_path: Path,
    *,
    dest_folder_id: str | None = None,
    dest_filename: str | None = None,
) -> dict[str, str]:
    """Upload `video_path` to Drive, share publicly, return file id + URL.

    Defaults to GDRIVE_FOLDER_ID and the original filename when the
    category folder / SOP filename haven't been resolved yet (e.g. dry runs).
    """
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    folder_id = dest_folder_id or config.gdrive_folder_id
    if not folder_id:
        raise RuntimeError("GDRIVE_FOLDER_ID is empty.")
    filename = dest_filename or video_path.name

    svc = _service()
    mime, _ = mimetypes.guess_type(str(video_path))
    mime = mime or "video/mp4"

    log.info("[gdrive] uploading %s as %s (%s)", video_path.name, filename, mime)
    media = MediaFileUpload(
        str(video_path), mimetype=mime, resumable=True, chunksize=8 * 1024 * 1024
    )
    metadata = {"name": filename, "parents": [folder_id]}

    request = svc.files().create(
        body=metadata, media_body=media, fields="id, name, webViewLink"
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("[gdrive] %.0f%%", status.progress() * 100)

    file_id = response["id"]
    log.info("[gdrive] uploaded id=%s", file_id)

    # "Anyone with the link can view"
    svc.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
        fields="id",
    ).execute()

    info = (
        svc.files()
        .get(fileId=file_id, fields="id, webViewLink, webContentLink")
        .execute()
    )
    return {
        "id": file_id,
        "view_url": info.get(
            "webViewLink", f"https://drive.google.com/file/d/{file_id}/view"
        ),
        "download_url": info.get(
            "webContentLink",
            f"https://drive.google.com/uc?id={file_id}&export=download",
        ),
    }
