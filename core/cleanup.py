"""Free disk space by deleting local files for videos that are fully posted.

`incoming/<master>.mp4` and `state/posting_copies/<key>_post.mp4` (+ the
`_story.mp4` cut) used to grow forever: on the HG680's small /www partition
that ended at 0 bytes free on 2026-10-02, which also took Docker — and with
it the bot — down. The master is already archived to Drive by step_gdrive
before distribution even starts, so once every platform a video was sent to
has succeeded, its local copies serve no further purpose.

Safe by construction — a state entry's files are only touched when:
  - it has a gdrive_url (the master's Drive backup exists), AND EITHER
  - it is *finished*: it has at least one result for a platform that isn't in
    `ignore_platforms`, every such result is "ok" (a single lingering "error"
    keeps the files, since a retry still needs them), every platform that was
    *selected* for it (selected_platforms, set by the bot) has a result too —
    a parallel run records platforms one by one, so "all results so far are
    ok" alone would also match a run still in flight — and its last update is
    older than the retention window; OR
  - it is *abandoned*: it was dry-run (master uploaded to Drive, caption
    made) but never posted anywhere and never scheduled, and nobody tapped
    ✅ Post or ❌ Batal for ABANDONED_RETENTION_DAYS. That is exactly what
    ❌ Batal would have done to the local file, just done on their behalf.
  - and the file lives under incoming/ or the posting-copy dir. A video
    someone ran by hand straight from some other folder is *their* original,
    not a staging copy, and is never deleted — whatever path the state
    recorded.

Anything with a pending scheduled post is always left alone.

`ignore_platforms` (CLEANUP_IGNORE_PLATFORMS, default "tiktok"): a platform
whose "error" must not block cleanup — TikTok is a manual hand-off, so its
status never means "needs a retry".

Files in those two folders that no state entry points at ("orphans" — an
interrupted download, a copy from before state tracked it) are only
*reported*, never deleted: with no entry there's no proof the master is on
Drive.

Two APIs, deliberately:
  - `assess()` / `execute()` / `run()` — the full engine, returns a `Report`.
    Used by the bot (after every run, daily, on disk pressure, /cleanup),
    scripts/cleanup_storage.py and core/health.py.
  - `plan()` / `apply()` — the original, narrower contract that was deployed
    on the box in b81febb and is called from scripts/mentahan.sh
    (`mentahan cleanup`). Kept signature-for-signature so that helper keeps
    working untouched; they are thin wrappers over the engine above.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from config import config
from core import state as pipeline_state

log = logging.getLogger("mentahanpov.cleanup")

REPO_ROOT = Path(__file__).resolve().parent.parent
# Same location telegram_bot.py / admin/app.py download into (hardcoded
# there as REPO_ROOT / "incoming" too — see DEPLOYMENT.md for symlinking it
# onto external storage, which is why paths below are compared lexically
# and never resolve()d).
INCOMING_DIR = REPO_ROOT / "incoming"

# Even under disk pressure (which skips the retention window), never touch
# anything that changed this recently — a floor against racing a run that is
# still writing its state entry.
PRESSURE_MIN_AGE = timedelta(minutes=30)
# Under pressure, an unconfirmed video is only given this long before it's
# treated as abandoned (the normal window is ABANDONED_RETENTION_DAYS).
PRESSURE_ABANDONED_AGE = timedelta(days=1)
# Don't report a file as an orphan until it's been sitting this long — an
# in-progress download has no state entry yet (it's only written afterwards).
ORPHAN_MIN_AGE = timedelta(days=1)


@dataclass
class Item:
    key: str
    path: Path
    size: int
    reason: str = "finished"  # or "abandoned"


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    # Videos with local files that this pass is deliberately leaving alone
    # because they aren't finished (a platform failed / is still pending).
    held_back_videos: int = 0
    held_back_bytes: int = 0
    # Finished videos still inside the retention window — reclaimable later.
    soon_videos: int = 0
    soon_bytes: int = 0
    # Files nothing in state.json points at. Reported, never deleted.
    orphan_files: int = 0
    orphan_bytes: int = 0
    pressure: bool = False
    applied: bool = False
    deleted: int = 0
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.items)

    @property
    def abandoned_videos(self) -> int:
        return len({i.key for i in self.items if i.reason == "abandoned"})

    @property
    def abandoned_bytes(self) -> int:
        return sum(i.size for i in self.items if i.reason == "abandoned")


@dataclass
class Candidate:
    """Legacy result shape of plan() — see the module docstring."""

    path: Path
    size: int
    key: str


def _norm(path: Path | str) -> str:
    return os.path.normpath(str(path))


def _under(path: Path, root: Path) -> bool:
    """Lexical containment — deliberately not resolve(), so a symlinked
    incoming/ (external USB storage) still counts as incoming/."""
    return _norm(path).startswith(_norm(root) + os.sep)


def _anchored(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def posting_copy_root() -> Path:
    return _anchored(config.posting_copy_dir)


def allowed_roots() -> list[Path]:
    return [INCOMING_DIR, posting_copy_root()]


def localize(path_str: str) -> Path:
    """Map a path recorded in state.json to where it lives for *this* process.

    State is written by the bot inside its container, so video_path is
    `/app/incoming/x.mp4` there — which doesn't exist when this runs from a
    plain checkout on the host (cron). Without this, a host-side cleanup
    would find nothing and silently free nothing.
    """
    p = Path(path_str)
    if p.is_absolute() and p.exists():
        return p
    posix = PurePosixPath(path_str.replace("\\", "/"))
    if len(posix.parts) > 2 and posix.parts[0] == "/" and posix.parts[1] == "app":
        return REPO_ROOT.joinpath(*posix.parts[2:])
    if not posix.is_absolute() and not p.is_absolute():
        # Always anchored to the repo root, even when the relative path
        # happens to exist from the current directory: every caller below
        # compares paths lexically against absolute roots, and inside the
        # container (cwd == /app) a relative path that "exists" would
        # otherwise be returned as-is, fail that comparison, and the
        # posting copies would silently never be cleaned.
        return REPO_ROOT / p
    return p


def _updated_at(entry: dict) -> datetime | None:
    raw = entry.get("updated_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def is_fully_done(entry: dict, ignore_platforms: tuple[str, ...] = ()) -> bool:
    if not entry.get("gdrive_url"):
        return False
    platforms = entry.get("platforms") or {}
    relevant = {n: r for n, r in platforms.items() if n not in ignore_platforms}
    if not relevant:
        return False
    if any(r.get("status") != "ok" for r in relevant.values()):
        return False
    selected = entry.get("selected_platforms")
    if selected is not None:
        waiting_on = set(selected) - set(ignore_platforms)
        if not waiting_on <= set(platforms):
            return False  # chosen, but no result recorded yet — still running
    return True


def is_abandoned_candidate(entry: dict) -> bool:
    """Dry-run finished (master is on Drive) but nothing was ever posted or
    scheduled. Age is checked separately by the caller."""
    return (
        bool(entry.get("gdrive_url"))
        and not entry.get("platforms")
        and not entry.get("scheduled_at")
    )


def _entry_files(
    entry: dict, key: str | None, roots: list[Path], copy_dir: Path
) -> list[Path]:
    """Every local file this entry points at that this module may delete.

    The trimmed story copy (watermark.make_story_copy) isn't recorded in
    state, but is always `<key>_story.mp4` next to the posting copy — so it's
    derived from the entry's key rather than leaving it to grow forever.
    """
    files: list[Path] = []
    for field_name in ("video_path", "posting_copy_path"):
        value = entry.get(field_name)
        if not value:
            continue
        path = localize(str(value))
        if not any(_under(path, root) for root in roots):
            log.debug("[cleanup] ignoring %s outside the storage dirs", path)
            continue
        files.append(path)
    if key:
        files.append(copy_dir / f"{key}_story.mp4")
    return files


def disk_usage() -> shutil._ntuple_diskusage:
    probe = INCOMING_DIR if INCOMING_DIR.exists() else config.state_file.parent
    try:
        return shutil.disk_usage(probe)
    except OSError:
        return shutil.disk_usage(REPO_ROOT)


def disk_free_bytes() -> int:
    return disk_usage().free


# On a small disk a fixed "2 GB free" threshold is a permanent alarm: the
# HG680's internal storage is 6.47 GB in total (seen on its CasaOS dashboard,
# 2026-10-11, ~1.7 GB free), which would sit under 2 GB forever and make
# "pressure" mode the normal state. So the threshold never exceeds this
# fraction of the disk's total size.
PRESSURE_MAX_FRACTION = 0.25


def pressure_threshold_bytes(total: int) -> float:
    return min(config.disk_pressure_free_gb * 1024**3, total * PRESSURE_MAX_FRACTION)


def under_pressure() -> bool:
    usage = disk_usage()
    return usage.free < pressure_threshold_bytes(usage.total)


def _orphans(referenced: set[str], now: datetime, roots: list[Path]) -> tuple[int, int]:
    count = total = 0
    for root in roots:
        try:
            scan = os.scandir(root)
        except OSError:
            continue
        with scan:
            for entry in scan:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if _norm(entry.path) in referenced:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if now.timestamp() - stat.st_mtime < ORPHAN_MIN_AGE.total_seconds():
                    continue
                count += 1
                total += stat.st_size
    return count, total


def assess(
    *,
    retention_days: int | None = None,
    pressure: bool | None = None,
    exclude_keys: frozenset[str] = frozenset(),
    now: datetime | None = None,
    state_file: Path | None = None,
    ignore_platforms: tuple[str, ...] | None = None,
    allowed_roots_: list[Path] | None = None,
    posting_copy_dir: Path | None = None,
) -> Report:
    """Work out what would be deleted, without touching the disk.

    Every override defaults to the configured value; they exist so the legacy
    plan() wrapper (and tests) can pin them explicitly.
    """
    now = now or datetime.now(timezone.utc)
    if pressure is None:
        pressure = under_pressure()
    days = config.cleanup_retention_days if retention_days is None else retention_days
    finished_cutoff = now - (PRESSURE_MIN_AGE if pressure else timedelta(days=days))

    ignore = (
        tuple(getattr(config, "cleanup_ignore_platforms", ()))
        if ignore_platforms is None
        else tuple(ignore_platforms)
    )
    roots = [
        _anchored(r)
        for r in (allowed_roots_ if allowed_roots_ is not None else allowed_roots())
    ]
    copy_dir = _anchored(
        posting_copy_dir if posting_copy_dir is not None else config.posting_copy_dir
    )
    state_path = state_file if state_file is not None else config.state_file

    abandoned_cutoff: datetime | None = None
    abandoned_days = getattr(config, "abandoned_retention_days", 0)
    if abandoned_days > 0:
        window = timedelta(days=abandoned_days)
        if pressure:
            window = min(window, PRESSURE_ABANDONED_AGE)
        abandoned_cutoff = now - window

    report = Report(pressure=pressure)
    seen: set[str] = set()
    referenced: set[str] = set()
    for key, entry in pipeline_state.load_all(state_path).items():
        all_files = _entry_files(entry, key, roots, copy_dir)
        referenced.update(_norm(f) for f in all_files)
        if key in exclude_keys or entry.get("scheduled_at"):
            continue  # a scheduled post still needs its master + copy

        files = [f for f in all_files if f.exists() and _norm(f) not in seen]
        if not files:
            continue
        sizes = [(f, f.stat().st_size) for f in files]
        updated = _updated_at(entry)

        if is_fully_done(entry, ignore):
            if updated is None or updated >= finished_cutoff:
                report.soon_videos += 1
                report.soon_bytes += sum(s for _, s in sizes)
                continue
            reason = "finished"
        elif (
            abandoned_cutoff is not None
            and is_abandoned_candidate(entry)
            and updated is not None
            and updated < abandoned_cutoff
        ):
            reason = "abandoned"
        else:
            report.held_back_videos += 1
            report.held_back_bytes += sum(s for _, s in sizes)
            continue

        for path, size in sizes:
            seen.add(_norm(path))
            report.items.append(Item(key=key, path=path, size=size, reason=reason))

    report.orphan_files, report.orphan_bytes = _orphans(referenced, now, roots + [copy_dir])
    return report


def execute(report: Report) -> Report:
    for item in report.items:
        try:
            item.path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            report.errors.append(f"{item.path.name}: {exc}")
            log.warning("[cleanup] couldn't delete %s: %s", item.path, exc)
            continue
        report.deleted += 1
        report.freed_bytes += item.size
        log.info(
            "[cleanup] deleted %s (%.1f MB, %s)",
            item.path,
            item.size / 1048576,
            item.reason,
        )
    report.applied = True
    return report


def run(
    *,
    apply_changes: bool,
    retention_days: int | None = None,
    pressure: bool | None = None,
    exclude_keys: frozenset[str] = frozenset(),
) -> Report:
    report = assess(
        retention_days=retention_days, pressure=pressure, exclude_keys=exclude_keys
    )
    return execute(report) if apply_changes else report


# --- legacy contract (b81febb / scripts/mentahan.sh) -------------------------


def plan(
    state_file: Path,
    *,
    retention_days: int,
    ignore_platforms: tuple[str, ...],
    allowed_roots: list[Path],
    posting_copy_dir: Path,
    now: datetime | None = None,
) -> list[Candidate]:
    """Return the files that are safe to delete. Never touches the disk.

    Pressure mode is never applied here (callers pass an explicit retention
    window and expect it honoured) — only the retention/abandoned rules.
    """
    report = assess(
        retention_days=retention_days,
        pressure=False,
        now=now,
        state_file=state_file,
        ignore_platforms=ignore_platforms,
        allowed_roots_=allowed_roots,
        posting_copy_dir=posting_copy_dir,
    )
    return [Candidate(path=i.path, size=i.size, key=i.key) for i in report.items]


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


# --- helpers ----------------------------------------------------------------


def dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def fmt_bytes(n: float) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    return f"{n / 1024**2:.0f} MB"
