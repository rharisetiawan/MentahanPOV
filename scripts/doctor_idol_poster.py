"""Post the Doctor Idol reels to Instagram and Facebook from the box, on schedule.

Runs inside the bot container from cron and lives in the mounted state folder, so
the bot's code and image stay untouched. Reads queue.json and keeps results in
done.json next to this file. Each run posts at most one due item.

The reel lives in a private Drive folder. Instagram has to fetch it by URL, so
it is shared by link only while posting and un-shared right after.

Never retries a post on its own: a failure may have published anyway, and the
repeat would be a duplicate on a public account. Failures go to Telegram instead.

    python doctor_idol_poster.py --check    # credentials and queue, posts nothing
    python doctor_idol_poster.py            # post what is due
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

import requests  # noqa: E402

from config import config  # noqa: E402
from core import gdrive  # noqa: E402
from core import threads_token  # noqa: E402
from distributors import facebook, instagram, threads, youtube  # noqa: E402
from distributors.meta_graph import graph_url  # noqa: E402

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "queue.json"
DONE = HERE / "done.json"
LOCK = HERE / "poster.lock"
ALERTS = HERE / "alerts.json"
YT_PLAN = HERE / "yt_plan.json"  # both copied from the laptop after each upload run
YT_QUEUE = HERE / "youtube_queue.json"
YT_ITEMS = HERE / "yt_items.json"
DAILY_UPLOADS = 8  # per 24 hours: the channel's upload limit is shared with the bot's own posts
PLATFORMS = ("instagram", "facebook", "threads")
# Late posts (box down, token dead) still go out, but at least this far apart.
CATCH_UP_GAP = dt.timedelta(minutes=45)


def telegram_base() -> str:
    # Same server as the bot. Once a bot lives on a Local Bot API Server, calling
    # api.telegram.org with its token can log it back in there and break the bot.
    base = config.telegram_local_api_url.rstrip("/") if config.telegram_api_id else "https://api.telegram.org"
    return f"{base}/bot{config.telegram_bot_token}"


def notify(text: str) -> None:
    print(text, flush=True)
    for user in config.telegram_allowed_user_ids or ():
        try:
            resp = requests.post(f"{telegram_base()}/sendMessage",
                                 data={"chat_id": user, "text": f"Doctor Idol: {text}"[:4000]}, timeout=30)
            if not resp.ok:
                print(f"telegram gagal: HTTP {resp.status_code}", flush=True)
        except requests.RequestException as exc:
            print(f"telegram gagal: {type(exc).__name__}", flush=True)


def alert_once(alerts: dict, key: str, text: str) -> None:
    if key not in alerts:
        alerts[key] = dt.datetime.now(dt.timezone.utc).isoformat()
        notify(text)


def load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def watch() -> int:
    """Hourly: anything that went wrong without a post attempt to report it. One alert per problem."""
    alerts = load(ALERTS, {})
    plan = load(YT_PLAN, [])
    uploaded = load(YT_QUEUE, {})
    now = dt.datetime.now(dt.timezone.utc)
    for item in plan:
        key, at = item["key"], dt.datetime.fromisoformat(item["at"])
        entry = uploaded.get(key)
        if not entry:
            if now < at <= now + dt.timedelta(hours=24):
                alert_once(alerts, f"missing:{key}", f"YouTube {key} tayang {item['at']} tapi belum terunggah. "
                           "Nyalakan laptop dan buka app Claude supaya antrean jalan.")
            continue
        if entry.get("thumbnail") not in ("ok", "-", None) and now + dt.timedelta(hours=24) >= at:
            alert_once(alerts, "thumbnail", "Thumbnail kustom YouTube masih ditolak. Cek fitur lanjutan kanal di Studio.")
        if at + dt.timedelta(minutes=30) <= now and not entry.get("verified") and f"public:{key}" not in alerts:
            resp = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=30, params={
                "part": "status,contentDetails", "id": entry["id"], "key": config.youtube_api_key})
            if not resp.ok:
                print(f"cek youtube gagal: HTTP {resp.status_code}", flush=True)
                continue
            found = resp.json().get("items", [])
            if not found:
                alert_once(alerts, f"public:{key}", f"YouTube {key} ({entry['id']}) belum publik setelah jadwalnya. "
                           "Kemungkinan kena klaim atau gagal diproses, cek Studio.")
            else:
                entry["verified"] = True
                YT_QUEUE.write_text(json.dumps(uploaded, indent=1, sort_keys=True), encoding="utf-8")
                blocked = found[0].get("contentDetails", {}).get("regionRestriction", {}).get("blocked")
                if blocked:
                    alert_once(alerts, f"blocked:{key}", f"YouTube {key} diblokir di {len(blocked)} negara (klaim hak cipta?).")
    queue = load(QUEUE, [])
    done = load(DONE, {})
    finished = plan and queue and all(uploaded.get(i["key"], {}).get("verified") or f"public:{i['key']}" in alerts for i in plan) \
        and all({*PLATFORMS, "tiktok"} <= set(done.get(i["key"], {})) for i in queue)
    if finished:
        alert_once(alerts, "all-done", "Seri selesai tayang. File /etc/cron.d/mentahanpov-doctor-idol boleh dihapus.")
    ALERTS.write_text(json.dumps(alerts, indent=1), encoding="utf-8")
    return 0


def save(done: dict) -> None:
    tmp = DONE.with_suffix(".tmp")
    tmp.write_text(json.dumps(done, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DONE)


def download(svc, file_id: str, out: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    with out.open("wb") as fh:
        loader = MediaIoBaseDownload(fh, svc.files().get_media(fileId=file_id), chunksize=16 * 1024 * 1024)
        finished = False
        while not finished:
            _, finished = loader.next_chunk(num_retries=3)


def post_item(item: dict, done: dict, platforms: list[str], tiktok: bool) -> None:
    key = item["key"]
    state = done.setdefault(key, {})
    svc = gdrive._service()
    permission = None
    with tempfile.TemporaryDirectory(prefix="doctor-idol-") as tmp:
        video = Path(tmp) / f"{key}.mp4"
        try:
            download(svc, item["drive_id"], video)
            if platforms:  # only Meta fetches by URL; the TikTok kit uses the local copy
                permission = svc.permissions().create(
                    fileId=item["drive_id"], body={"role": "reader", "type": "anyone"}, fields="id").execute()["id"]
            modules = {"instagram": instagram, "facebook": facebook, "threads": threads}
            for platform, module in ((p, modules[p]) for p in platforms):
                # Written first: if this process dies mid-post the next run must not repeat it.
                state[platform] = {"status": "started"}
                save(done)
                try:
                    url = gdrive.direct_download_url(item["drive_id"])
                    # Threads cuts at 500 characters, which could lose the fancam note.
                    caption = item["short_caption"] if platform == "threads" else item["caption"]
                    result = module.post(video, caption, post_url=url)
                except Exception as exc:  # noqa: BLE001 - any failure is reported, never retried
                    state[platform] = {"status": "failed", "error": str(exc)[:300]}
                    notify(f"{key} GAGAL di {platform}, tidak dicoba ulang: {str(exc)[:300]}")
                else:
                    state[platform] = {"status": "posted", "url": result.get("url", "")}
                    print(f"{key} tayang di {platform}: {result.get('url', '')}", flush=True)
                save(done)
            if tiktok:
                send_tiktok_kit(item, video, state)
                save(done)
        finally:
            if permission:
                svc.permissions().delete(fileId=item["drive_id"], permissionId=permission).execute()


def send_tiktok_kit(item: dict, video: Path, state: dict) -> None:
    """TikTok has no uploader here: the video and a caption to copy, sent to Telegram."""
    state["tiktok"] = {"status": "started"}
    try:
        for user in config.telegram_allowed_user_ids or ():
            with video.open("rb") as fh:
                sent = requests.post(f"{telegram_base()}/sendVideo", timeout=600, files={"video": fh},
                                     data={"chat_id": user, "supports_streaming": "true",
                                           "caption": f"TikTok {item['key']}: video ini, teks di pesan berikutnya."})
            sent.raise_for_status()
            text = requests.post(f"{telegram_base()}/sendMessage", timeout=30,
                                 data={"chat_id": user, "text": item["short_caption"]})
            text.raise_for_status()
    except (requests.RequestException, OSError) as exc:
        state["tiktok"] = {"status": "failed", "error": type(exc).__name__}
        notify(f"Kiriman TikTok {item['key']} gagal ({type(exc).__name__}). Video ada di Drive.")
    else:
        state["tiktok"] = {"status": "sent"}


def youtube_reason(exc: Exception) -> str:
    return str(getattr(exc, "content", b"") or exc)


def upload_youtube() -> int:
    """Upload due-soonest videos private with publishAt. YouTube flips them public by itself."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        alerts = load(ALERTS, {})
        uploaded = load(YT_QUEUE, {})
        now = dt.datetime.now(dt.timezone.utc)
        recent = sum(1 for e in uploaded.values() if isinstance(e, dict) and e.get("uploaded_at")
                     and now - dt.datetime.fromisoformat(e["uploaded_at"]) < dt.timedelta(hours=24))
        svc = gdrive._service()
        yt = youtube._service()

        def set_thumbnail(entry: dict, thumb_id: str, folder: Path) -> None:
            jpg = folder / "thumb.jpg"
            download(svc, thumb_id, jpg)
            try:
                yt.thumbnails().set(videoId=entry["id"], media_body=MediaFileUpload(str(jpg), mimetype="image/jpeg")).execute()
                entry["thumbnail"] = "ok"
            except HttpError as exc:
                entry["thumbnail"] = f"HTTP {exc.resp.status}"

        with tempfile.TemporaryDirectory(prefix="doctor-idol-yt-") as tmp:
            folder = Path(tmp)
            for item in load(YT_ITEMS, []):
                entry = uploaded.get(item["key"])
                if entry:
                    if item.get("thumb_id") and entry.get("thumbnail") != "ok":
                        set_thumbnail(entry, item["thumb_id"], folder)
                        YT_QUEUE.write_text(json.dumps(uploaded, indent=1, sort_keys=True), encoding="utf-8")
                    continue
                if recent >= DAILY_UPLOADS:
                    continue  # leave the channel's allowance for the bot's own uploads
                at = dt.datetime.fromisoformat(item["at"])
                if at <= now + dt.timedelta(hours=2):
                    alert_once(alerts, f"late:{item['key']}", f"YouTube {item['key']} terlewat jadwal, tidak diunggah.")
                    continue
                video = folder / "video.mp4"
                try:
                    download(svc, item["drive_id"], video)
                    request = yt.videos().insert(part="snippet,status", body={
                        "snippet": {"title": item["title"], "description": item["description"], "categoryId": "24"},
                        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                                   "publishAt": at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")},
                    }, media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=16 * 1024 * 1024, resumable=True))
                    response = None
                    while response is None:
                        _, response = request.next_chunk(num_retries=3)
                except HttpError as exc:
                    reason = youtube_reason(exc)
                    if "uploadLimitExceeded" in reason or "quotaExceeded" in reason:
                        print(f"batas unggah tercapai di {item['key']}, lanjut jadwal berikutnya", flush=True)
                        break
                    alert_once(alerts, f"upload:{item['key']}", f"Unggah YouTube {item['key']} gagal: HTTP {exc.resp.status}")
                    break
                except Exception as exc:  # noqa: BLE001 - network or Drive; the next run tries again
                    print(f"{item['key']}: {type(exc).__name__}", flush=True)
                    break
                finally:
                    video.unlink(missing_ok=True)
                entry = {"id": response["id"], "publishAt": response.get("status", {}).get("publishAt"),
                         "thumbnail": "-", "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                uploaded[item["key"]] = entry
                if not entry["publishAt"]:
                    alert_once(alerts, f"nopublish:{item['key']}", f"YouTube {item['key']} terunggah tanpa jadwal tayang, cek Studio.")
                if item.get("thumb_id"):
                    set_thumbnail(entry, item["thumb_id"], folder)
                YT_QUEUE.write_text(json.dumps(uploaded, indent=1, sort_keys=True), encoding="utf-8")
                recent += 1
                print(f"ok {item['key']} {entry['id']} {entry['publishAt']} thumb={entry['thumbnail']}", flush=True)
        ALERTS.write_text(json.dumps(alerts, indent=1), encoding="utf-8")
    return 0


def test_link(queue: list[dict]) -> int:
    """Share the first reel for a moment, confirm a stranger gets video bytes, un-share. Posts nothing."""
    svc = gdrive._service()
    file_id = queue[0]["drive_id"]
    permission = svc.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}, fields="id").execute()["id"]
    try:
        resp = requests.get(gdrive.direct_download_url(file_id), stream=True, timeout=60, headers={"Range": "bytes=0-1023"})
        print("link", resp.status_code, resp.headers.get("content-type"))
        resp.close()
    finally:
        svc.permissions().delete(fileId=file_id, permissionId=permission).execute()
    after = requests.get(gdrive.direct_download_url(file_id), stream=True, timeout=60)
    print("setelah ditutup", after.status_code, after.headers.get("content-type"))
    after.close()
    return 0


def main() -> int:
    queue = json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else []
    if "--watch" in sys.argv:
        return watch()
    if "--upload-youtube" in sys.argv:
        return upload_youtube()
    if "--test-link" in sys.argv:
        return test_link(queue)
    if "--test-tiktok" in sys.argv:
        # Sends the first kit as a rehearsal. Records nothing, so the real one still goes out on its day.
        with tempfile.TemporaryDirectory(prefix="doctor-idol-") as tmp:
            video = Path(tmp) / "uji.mp4"
            download(gdrive._service(), queue[0]["drive_id"], video)
            state: dict = {}
            send_tiktok_kit({**queue[0], "key": f"UJI {queue[0]['key']}"}, video, state)
            print(state)
        return 0
    if "--test-telegram" in sys.argv:
        notify("alert jadwal seri ini aktif. Pesan hanya dikirim kalau ada masalah.")
        return 0
    if "--check" in sys.argv:
        missing = [n for n, v in (("ig_user_id", config.ig_user_id), ("fb_page_id", config.fb_page_id),
                                  ("fb_token", config.fb_page_access_token),
                                  ("telegram", config.telegram_bot_token)) if not v]
        about = gdrive._service().about().get(fields="user(emailAddress)").execute()
        for item in queue:
            gdrive._service().files().get(fileId=item["drive_id"], fields="id").execute()
        print(f"config kurang: {missing or '-'}; drive: {about['user']['emailAddress']}; "
              f"telegram users: {len(config.telegram_allowed_user_ids or ())}; antrean: {len(queue)}")
        return 1 if missing else 0

    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # the previous run is still posting
        done = load(DONE, {})
        alerts = load(ALERTS, {})
        now = dt.datetime.now(dt.timezone.utc)
        ready: dict[str, bool] = {}
        try:
            return post_next(queue, done, alerts, ready, now)
        finally:
            ALERTS.write_text(json.dumps(alerts, indent=1), encoding="utf-8")


def token_ok(platform: str, ready: dict, alerts: dict) -> bool:
    """Is the platform's token alive? Asked once per run. A dead one holds posts instead of failing them."""
    group = "threads" if platform == "threads" else "meta"
    if group not in ready:
        if group == "meta":
            url, token = graph_url(config.ig_user_id), config.fb_page_access_token
        else:
            url, token = f"https://graph.threads.net/{config.threads_api_version}/me", threads_token.current()
        try:
            resp = requests.get(url, params={"fields": "id", "access_token": token}, timeout=30)
            ready[group] = resp.ok
            dead = resp.status_code in (400, 401) and resp.json().get("error", {}).get("code") == 190
        except (requests.RequestException, ValueError):
            ready[group], dead = False, False  # network trouble: just try next run
        if ready[group]:
            alerts.pop(f"token:{group}", None)  # so the next outage alerts again
        elif dead:
            names = "Threads" if group == "threads" else "Instagram dan Facebook"
            alert_once(alerts, f"token:{group}", f"Token {names} ditolak Meta (sesi dibatalkan). Posting ditahan, "
                       "bukan dibatalkan, dan otomatis menyusul setelah token baru dipasang.")
    return ready[group]


def post_next(queue: list[dict], done: dict, alerts: dict, ready: dict, now: dt.datetime) -> int:
    """Act on the first item that still owes something, in order, so no platform posts out of sequence."""
    last = done.get("_last_post")
    blocked = False  # an earlier item waits on a token: later ones may only send their TikTok kit
    for item in queue:
        state = done.get(item["key"], {})
        # A "started" left behind means a run died mid-post: never repeat, ask a human.
        stuck = [p for p, s in state.items() if s.get("status") == "started"]
        if stuck:
            for platform in stuck:
                state[platform] = {"status": "unknown"}
            save(done)
            notify(f"{item['key']}: proses terhenti saat posting ke {stuck}. Cek akunnya, tidak dicoba ulang.")
        pending = [p for p in PLATFORMS if p not in state]
        tiktok = "tiktok" not in state
        if not pending and not tiktok:
            continue
        at = dt.datetime.fromisoformat(item["at"])
        if at > now:
            return 0
        go = [] if blocked else [p for p in pending if token_ok(p, ready, alerts)]
        # Catching up after an outage: space posts out instead of a burst.
        if go and now - at > dt.timedelta(minutes=20) and last and \
                now - dt.datetime.fromisoformat(last) < CATCH_UP_GAP:
            go = []
        if go or tiktok:
            post_item(item, done, go, tiktok)
            if go:
                done["_last_post"] = now.isoformat()
                save(done)
            return 0
        if pending:
            blocked = True  # waiting on a token: later items must not jump the queue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
