"""Google Drive uploader (Service Account auth).

Uploads a single video file to a target folder, sets the file permission to
"anyone with the link can view", and returns the shareable URL.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config

log = logging.getLogger(__name__)

# Full `drive` scope (not just `drive.file`) so the service account can list
# the category subfolders under GDRIVE_FOLDER_ID, not just write new files.
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _service() -> Any:
    if not config.gdrive_sa_json.exists():
        raise FileNotFoundError(
            f"GDrive service-account JSON not found at {config.gdrive_sa_json}. "
            "See README → 'Google Drive setup'."
        )
    creds = Credentials.from_service_account_file(
        str(config.gdrive_sa_json), scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def resolve_category_folder_id(category_name: str) -> str:
    """Find the id of the subfolder named `category_name` under GDRIVE_FOLDER_ID."""
    if not config.gdrive_folder_id:
        raise RuntimeError(
            "GDRIVE_FOLDER_ID is empty. Share the MentahanPOV project folder "
            "with the service account and set its ID."
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
            "Check the folder exists and the service account has access to it."
        )
    return files[0]["id"]


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
        raise RuntimeError(
            "GDRIVE_FOLDER_ID is empty. Share a folder with the service account and set its ID."
        )
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
