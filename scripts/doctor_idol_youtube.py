"""Upload Doctor Idol videos to YouTube, private until publishAt.

YouTube itself flips each video public at its time, so the laptop only has to be
on while uploading. The API allows about 6 uploads a day (quota resets 15:00 WIB),
so rerun this daily: finished items are recorded in doctor_idol/youtube_queue.json
and skipped, and the run stops cleanly when the quota is gone.

Order is the series order: announcement first, then episode 1, 2, ... One item a
day: the Short at 10:00 WIB and the full version at 21:00 WIB, away from the bot's
slots (7, 12, 19). The first 11 uploads went out at 12:00 and 19:00 and stay there:
this token can upload but not edit. After each run the plan and results are copied
to the box, whose hourly watch alerts on Telegram if the laptop falls behind.

    python scripts/doctor_idol_youtube.py --dry-run
    python scripts/doctor_idol_youtube.py --start 2026-11-09 [--limit 1]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from doctor_idol import catalog as cat  # noqa: E402

WIB = dt.timezone(dt.timedelta(hours=7))
# (reel and Short, full version), away from the bot's slots at 7, 12 and 19.
DAILY_SLOTS = ((9, 11), (15, 17), (20, 22))
QUEUE = cat.PACKAGE_DIR / "youtube_queue.json"
PLAN = cat.PACKAGE_DIR / "yt_plan.json"
BOX_DIR = "root@100.97.108.67:/www/apps/mentahanpov/state/doctor_idol/"
TOKEN_FILE = cat.main_checkout() / "credentials" / "youtube-token.json"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
THUMB = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CHUNK = 64 * 1024 * 1024
ENTERTAINMENT = "24"


class QuotaExceeded(Exception):
    pass


def section(text: str, header: str) -> str:
    """The body under `=== header ===`, up to the next header."""
    body = text.split(f"=== {header} ===\n", 1)[1]
    return body.split("\n=== ", 1)[0].strip()


def full_meta(text: str) -> tuple[str, str]:
    body = section(text, "YOUTUBE")
    title, description = body.split("\nDeskripsi:\n", 1)
    return title.removeprefix("Judul: ").strip(), description.strip()


def short_meta(text: str) -> tuple[str, str]:
    body = section(text, "YOUTUBE SHORTS")
    title, description = body.split("\nDeskripsi: ", 1)
    return title.removeprefix("Judul: ").strip(), description.strip()


def slot_times(start: dt.date, index: int) -> tuple[dt.datetime, dt.datetime]:
    """Three a day while the event is fresh: (reel + Short time, full version time)."""
    day, slot = divmod(index, len(DAILY_SLOTS))
    date = start + dt.timedelta(days=day)
    short_hour, full_hour = DAILY_SLOTS[slot]
    return dt.datetime.combine(date, dt.time(short_hour), WIB), dt.datetime.combine(date, dt.time(full_hour), WIB)


def plan(catalog: cat.Catalog, start: dt.date) -> list[dict]:
    """Every upload in posting order. Episodeless (announcement) goes first."""
    ordered = sorted(catalog.finalists, key=lambda f: f.episode or 0)
    root = cat.media_root()
    items = []
    for index, f in enumerate(ordered):
        short_at, full_at = slot_times(start, index)
        reel = cat.reels_dir(f.slug, root)
        long_dir = root / "youtube" / f.slug
        title, desc = short_meta((reel / "caption.txt").read_text(encoding="utf-8"))
        items.append({"key": f"{f.slug}:short", "video": reel / "reels.mp4", "thumb": None, "title": title,
                      "description": desc, "at": short_at})
        title, desc = full_meta((long_dir / "youtube.txt").read_text(encoding="utf-8"))
        items.append({"key": f"{f.slug}:full", "video": long_dir / "video.mp4", "thumb": long_dir / "thumbnail.jpg",
                      "title": title, "description": desc, "at": full_at})
    return items


class YouTube:
    def __init__(self) -> None:
        self.info = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        self.token = ""
        self.expires = 0.0

    def headers(self) -> dict[str, str]:
        if time.time() > self.expires:
            resp = requests.post(self.info.get("token_uri", "https://oauth2.googleapis.com/token"), data={
                "client_id": self.info["client_id"], "client_secret": self.info["client_secret"],
                "refresh_token": self.info["refresh_token"], "grant_type": "refresh_token",
            }, timeout=60)
            resp.raise_for_status()
            self.token = resp.json()["access_token"]
            self.expires = time.time() + 3000
        return {"Authorization": f"Bearer {self.token}"}

    def upload(self, item: dict) -> dict:
        size = item["video"].stat().st_size
        body = {
            "snippet": {"title": item["title"], "description": item["description"], "categoryId": ENTERTAINMENT},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                       "publishAt": item["at"].astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")},
        }
        start = requests.post(UPLOAD, headers={**self.headers(), "X-Upload-Content-Length": str(size),
                                               "X-Upload-Content-Type": "video/mp4"}, json=body, timeout=60)
        # 403 quotaExceeded is the API's daily quota; 400 uploadLimitExceeded is the channel's own limit.
        if start.status_code in (400, 403) and ("quota" in start.text.lower() or "uploadLimit" in start.text):
            raise QuotaExceeded(start.text[:300])
        if not start.ok:
            raise RuntimeError(f"HTTP {start.status_code} {start.text[:400]}")
        session = start.headers["Location"]
        sent = 0
        with item["video"].open("rb") as fh:
            while True:
                fh.seek(sent)
                chunk = fh.read(CHUNK)
                end = sent + len(chunk) - 1
                resp = None
                for attempt in range(8):
                    try:
                        resp = requests.put(session, data=chunk, timeout=900, headers={
                            **self.headers(), "Content-Range": f"bytes {sent}-{end}/{size}"})
                    except requests.RequestException as exc:
                        print(f"  jaringan: {type(exc).__name__}, coba lagi", flush=True)
                        time.sleep(2 ** attempt)
                        resp = None
                        continue
                    if resp.status_code < 500:
                        break
                    time.sleep(2 ** attempt)
                if resp is None:
                    raise RuntimeError("jaringan gagal terus")
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code != 308:
                    raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:300]}")
                received = resp.headers.get("Range")
                sent = int(received.rsplit("-", 1)[1]) + 1 if received else 0

    def thumbnail(self, video_id: str, path: Path) -> str:
        resp = requests.post(THUMB, params={"videoId": video_id}, data=path.read_bytes(), timeout=300,
                             headers={**self.headers(), "Content-Type": "image/jpeg"})
        return "ok" if resp.ok else f"HTTP {resp.status_code}: {resp.text[:160]}"


def sync_to_box(items: list[dict], queue: dict) -> None:
    """Best effort: if the box is unreachable, its watch simply sees older data."""
    plan = [{"key": i["key"], "at": (queue.get(i["key"], {}).get("publishAt") or i["at"].isoformat()).replace("Z", "+00:00")}
            for i in items]
    PLAN.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    result = subprocess.run(["scp", "-q", "-i", str(Path.home() / ".ssh" / "hg680_mentahanpov"), "-o", "BatchMode=yes",
                             "-o", "ConnectTimeout=15", str(PLAN), str(QUEUE), BOX_DIR], capture_output=True, text=True)
    print("sinkron ke box:", "ok" if result.returncode == 0 else result.stderr.strip()[:160], flush=True)


def main() -> int:
    queue = json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else {}
    start = dt.date.fromisoformat(queue.get("_start", "2026-11-09"))
    try:
        return run()
    finally:
        queue = json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else {}
        if "--dry-run" not in sys.argv:
            sync_to_box(plan(cat.load_catalog(), start), queue)


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2026-11-09", help="tanggal tayang pertama (WIB)")
    parser.add_argument("--limit", type=int, help="berhenti setelah n unggahan")
    parser.add_argument("--dry-run", action="store_true", help="tampilkan jadwal, tanpa mengunggah")
    parser.add_argument("--thumbnails", action="store_true", help="pasang ulang thumbnail yang belum berhasil")
    args = parser.parse_args()

    queue = json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else {}
    items = plan(cat.load_catalog(), dt.date.fromisoformat(queue.get("_start", args.start)))
    queue.setdefault("_start", args.start)
    if args.dry_run:
        for item in items:
            state = "sudah" if item["key"] in queue else "antre"
            print(f"{state} {item['at']:%d %b %H:%M} {item['key']:24} {item['title']}")
            assert len(item["title"]) <= 100, item["key"]
            assert "bukan akun resmi Doctor Idol" in item["description"], item["key"]
        return 0

    yt = YouTube()
    if args.thumbnails:
        # A channel can set custom thumbnails only once verified; catch up on the ones refused before that.
        for item in items:
            entry = queue.get(item["key"])
            if entry and item["thumb"] and entry.get("thumbnail") != "ok":
                entry["thumbnail"] = yt.thumbnail(entry["id"], item["thumb"])
                QUEUE.write_text(json.dumps(queue, indent=1, sort_keys=True), encoding="utf-8")
                print(f"{item['key']}: {entry['thumbnail'][:120]}", flush=True)
        return 0
    uploaded = 0
    for item in items:
        if item["key"] in queue:
            continue
        if args.limit is not None and uploaded >= args.limit:
            break
        if item["at"] <= dt.datetime.now(WIB) + dt.timedelta(hours=1):
            print(f"! {item['key']}: jadwal {item['at']:%d %b %H:%M} sudah lewat, berhenti")
            return 2
        try:
            result = yt.upload(item)
        except QuotaExceeded as exc:
            print(f"batas unggah tercapai, lanjut pada jadwal berikutnya: {exc}", flush=True)
            return 0
        except (RuntimeError, requests.RequestException) as exc:
            print(f"x {item['key']}: {exc}", flush=True)
            return 1
        status = result.get("status", {})
        thumb = yt.thumbnail(result["id"], item["thumb"]) if item["thumb"] else "-"
        queue[item["key"]] = {"id": result["id"], "publishAt": status.get("publishAt"), "thumbnail": thumb}
        QUEUE.write_text(json.dumps(queue, indent=1, sort_keys=True), encoding="utf-8")
        uploaded += 1
        print(f"ok {item['key']} id={result['id']} privacy={status.get('privacyStatus')} "
              f"publishAt={status.get('publishAt')} thumb={thumb}", flush=True)
        if not status.get("publishAt"):
            print("! YouTube tidak menyimpan jadwal, berhenti untuk diperiksa", flush=True)
            return 3
    print(f"unggahan sesi ini: {uploaded}; total selesai: {len(queue) - 1}/{len(items)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
