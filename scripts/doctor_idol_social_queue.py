"""Build queue.json for the box poster (scripts/doctor_idol_poster.py).

Three reels a day (09:00, 15:00, 20:00 WIB), away from the bot's slots (7, 12, 19). Needs
doctor_idol/drive_manifest.json from scripts/doctor_idol_backup.py. The caption is
the Instagram/Facebook section without its "Collab:" reminder, which is a note to us.

    python scripts/doctor_idol_social_queue.py --start 2026-11-09 --out queue.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from doctor_idol import catalog as cat  # noqa: E402
from doctor_idol.captions import FANCAM_NOTE, TAKEDOWN_NOTE  # noqa: E402
from doctor_idol_youtube import slot_times  # noqa: E402

HEADER = "=== INSTAGRAM REELS / FACEBOOK ===\n"


def post_caption(caption_txt: str) -> str:
    body = caption_txt.split(HEADER, 1)[1].split("\n=== ", 1)[0]
    lines = [line for line in body.splitlines() if not line.startswith("Collab:")]
    caption = "\n".join(lines).strip()
    if FANCAM_NOTE not in caption or TAKEDOWN_NOTE not in caption or "[" in caption:
        raise SystemExit(f"caption bermasalah:\n{caption}")
    return caption


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-11-09")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((cat.PACKAGE_DIR / "drive_manifest.json").read_text(encoding="utf-8"))
    start = dt.date.fromisoformat(args.start)
    queue = []
    finalists = sorted(cat.load_catalog().finalists, key=lambda x: x.episode or 0)
    for index, f in enumerate(finalists):
        at = slot_times(start, index)[0]
        text = (cat.reels_dir(f.slug) / "caption.txt").read_text(encoding="utf-8")
        short = text.split("=== TIKTOK ===\n", 1)[1].strip()
        if len(short) > 500 or FANCAM_NOTE not in short or TAKEDOWN_NOTE not in short:
            raise SystemExit(f"{f.slug}: teks pendek bermasalah ({len(short)} karakter)")
        queue.append({
            "key": f.slug,
            "drive_id": manifest[f"reels/{f.slug}/reels.mp4"]["id"],
            "caption": post_caption(text),
            "short_caption": short,
            "at": at.isoformat(),
        })
    args.out.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(queue)} item, {queue[0]['at']} .. {queue[-1]['at']}")

    # What is still to upload to YouTube, for the box to take over from the laptop.
    from doctor_idol_youtube import plan  # same folder

    done = json.loads((cat.PACKAGE_DIR / "youtube_queue.json").read_text(encoding="utf-8"))
    items = []
    for item in plan(cat.load_catalog(), start):
        slug, kind = item["key"].split(":")
        video = f"reels/{slug}/reels.mp4" if kind == "short" else f"youtube/{slug}/video.mp4"
        items.append({"key": item["key"], "drive_id": manifest[video]["id"],
                      "thumb_id": manifest[f"youtube/{slug}/thumbnail.jpg"]["id"] if kind == "full" else None,
                      "title": item["title"], "description": item["description"], "at": item["at"].isoformat()})
    out = args.out.with_name("yt_items.json")
    out.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"youtube: {len(items)} item, {sum(i['key'] not in done for i in items)} belum diunggah")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
