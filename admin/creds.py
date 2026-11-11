"""Credential-file management for the dashboard's Credentials tab.

Cross-references path-typed env fields (YOUTUBE_CLIENT_SECRETS,
GDRIVE_TOKEN_FILE, TIKTOK_STORAGE_STATE, ...) with what's actually on
disk, and uploads always write to the exact path the pipeline reads from
— there's no filename the user has to get right, which rules out the
"uploaded it, but to the wrong name" failure mode entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class CredentialFile:
    key: str  # env var name, e.g. "YOUTUBE_CLIENT_SECRETS"
    label: str
    path: Path
    exists: bool
    size_kb: float | None
    modified: str | None


def resolve(raw_path: str, repo_root: Path) -> Path:
    p = Path(raw_path)
    return p if p.is_absolute() else repo_root / p


def list_credential_files(
    fields: list[tuple[str, str]], values: dict[str, str], repo_root: Path
) -> list[CredentialFile]:
    """`fields` is [(key, label), ...] for the path fields to show."""
    out = []
    for key, label in fields:
        raw = values.get(key)
        if not raw:
            continue
        p = resolve(raw, repo_root)
        exists = p.exists()
        size_kb = round(p.stat().st_size / 1024, 1) if exists else None
        modified = (
            datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if exists
            else None
        )
        out.append(
            CredentialFile(
                key=key,
                label=label,
                path=p,
                exists=exists,
                size_kb=size_kb,
                modified=modified,
            )
        )
    return out


def save_upload(dest_path: Path, file_storage) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    file_storage.save(str(dest_path))


def delete_file(path: Path) -> None:
    if path.exists():
        path.unlink()
