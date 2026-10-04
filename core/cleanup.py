"""Delete old videos that are safely done, so the box's small disk can't fill up.

`incoming/` (the Telegram masters) and `state/posting_copies/` (the
watermarked `*_post.mp4` / `*_story.mp4`) used to only ever grow. On the
HG680's small /www partition that ended with 0 bytes free, which also took
Docker down with it.

A job's files are only deleted when ALL of these hold:
  * the master is already on Google Drive (`gdrive_url` in state),
  * it has at least one platform result and every platform that isn't in
    `ignore_platforms` is "ok" (an "error" may still be retried, so those
    files stay),
  * the entry is older than the retention window.

TikTok is ignored by default: it's a manual hand-off, so its "error" status
never means "needs a retry".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import state

log = logging.getLogger("mentahanpov.cleanup")

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Candidate:
    path: Path
    size: int
    key: str


def _resolve(raw: str) -> Path:
    # State stores paths relative to the repo root when they were recorded
    # that way; resolve explicitly so the cwd doesn't matter.
    p = Path(raw)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _is_inside(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def plan(
    state_file: Path,
    *,
    retention_days: int,
    ignore_platforms: tuple[str, ...],
    allowed_roots: list[Path],
    posting_copy_dir: Path,
    now: datetime | None = None,
) -> list[Candidate]:
    """Return the files that are safe to delete. Never touches the disk."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    out: list[Candidate] = []
    seen: set[Path] = set()

    for key, entry in state.load_all(state_file).items():
        if not entry.get("gdrive_url"):
            continue
        statuses = [
            res.get("status")
            for name, res in (entry.get("platforms") or {}).items()
            if name not in ignore_platforms
        ]
        if not statuses or any(s != "ok" for s in statuses):
            continue
        updated = _parse_ts(entry.get("updated_at"))
        if updated is None or updated >= cutoff:
            continue

        raw_paths = [entry.get("video_path"), entry.get("posting_copy_path")]
        paths = [_resolve(p) for p in raw_paths if p]
        paths.append(posting_copy_dir / f"{key}_story.mp4")
        for path in paths:
            if path in seen or not path.is_file():
                continue
            # Belt and braces: a corrupt state entry must never be able to
            # point a delete outside the two storage folders.
            if not _is_inside(path, allowed_roots):
                log.warning("[cleanup] skipping %s: outside storage dirs", path)
                continue
            seen.add(path)
            out.append(Candidate(path=path, size=path.stat().st_size, key=key))
    return out


def apply(candidates: list[Candidate]) -> tuple[int, int]:
    """Delete the candidates. Returns (files_deleted, bytes_freed)."""
    files = freed = 0
    for c in candidates:
        try:
            c.path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            log.exception("[cleanup] could not delete %s", c.path)
            continue
        files += 1
        freed += c.size
    return files, freed
