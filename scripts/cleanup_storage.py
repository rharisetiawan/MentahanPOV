"""Free disk space by deleting local files for videos that are fully posted.

Thin CLI over core/cleanup.py, which holds the actual rules (and the full
explanation of why they're safe) so the Telegram bot and this script can
never disagree about what's deletable. The bot already runs the same
cleanup by itself — after every run, daily at CLEANUP_HOUR, and when disk
runs low — so this script is only needed to preview/force it by hand or
for a box that isn't running the bot.

Defaults to a dry run. Pass --apply to actually delete.

Usage:
    python scripts/cleanup_storage.py                  # preview only
    python scripts/cleanup_storage.py --apply           # actually delete
    python scripts/cleanup_storage.py --retention-days 7 --apply
    python scripts/cleanup_storage.py --pressure --apply  # ignore retention window

Inside the running bot container (paths in state/posts.json are the
container's, so this is the most direct way):
    docker exec mentahanpov-mentahanpov-bot-1 python scripts/cleanup_storage.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Lives in scripts/, one level below the repo root that config.py and
# core/ are imported from — same fix-up main.py gets for free by sitting
# at the root itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config  # noqa: E402
from core import cleanup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retention-days",
        type=int,
        default=config.cleanup_retention_days,
        help="Only touch entries whose last update is older than this many days "
        f"(default: {config.cleanup_retention_days}, from CLEANUP_RETENTION_DAYS).",
    )
    parser.add_argument(
        "--pressure",
        action="store_true",
        help="Skip the retention window (keeps only a 30-minute safety floor). "
        "Every other safety condition still applies.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete files. Without this flag, only prints what would happen.",
    )
    args = parser.parse_args()

    report = cleanup.run(
        apply_changes=args.apply,
        retention_days=args.retention_days,
        pressure=True if args.pressure else False,
    )

    tag = "DELETE" if args.apply else "would delete"
    for item in report.items:
        why = "" if item.reason == "finished" else f", {item.reason} (never posted)"
        print(f"[{tag}] {item.path} ({item.size / 1024 / 1024:.1f} MB{why})")
    for err in report.errors:
        print(f"[ERROR] {err}")

    if args.apply:
        print(
            f"\n{report.deleted} file(s), "
            f"{cleanup.fmt_bytes(report.freed_bytes)} freed."
        )
    else:
        print(
            f"\n{len(report.items)} file(s), "
            f"{cleanup.fmt_bytes(report.total_bytes)} would be freed."
        )
        if report.items:
            print("Dry run only -- rerun with --apply to actually delete.")
    if report.soon_videos:
        print(
            f"{report.soon_videos} finished video(s), "
            f"{cleanup.fmt_bytes(report.soon_bytes)}, still inside the retention window."
        )
    if report.held_back_videos:
        print(
            f"{report.held_back_videos} unfinished video(s) "
            f"({cleanup.fmt_bytes(report.held_back_bytes)}) kept: a platform "
            "failed or is still pending, so a retry still needs the files."
        )
    if report.orphan_files:
        print(
            f"{report.orphan_files} file(s) ({cleanup.fmt_bytes(report.orphan_bytes)}) "
            "in incoming/ or the posting-copy dir that no state entry points at — "
            "never deleted automatically (nothing proves the master is on Drive); "
            "inspect them by hand."
        )
    print(f"Free disk: {cleanup.fmt_bytes(cleanup.disk_free_bytes())}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
