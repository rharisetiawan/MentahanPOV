"""Move already-uploaded Doctor Idol videos to the current schedule.

The bot's YouTube token may only upload, so this asks once, in the browser, for a
separate token that may edit. It is kept only in memory: the bot's own token file
is not touched, and nothing is written to disk but the updated queue.

    python scripts/doctor_idol_reschedule.py --start 2026-11-08
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import json
import secrets
import sys
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from doctor_idol import catalog as cat  # noqa: E402
from doctor_idol_youtube import QUEUE, plan  # noqa: E402

CLIENT = cat.main_checkout() / "credentials" / "youtube-oauth.json"
SCOPE = "https://www.googleapis.com/auth/youtube"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"


def ask_for_token() -> str:
    client = json.loads(CLIENT.read_text(encoding="utf-8"))["installed"]
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if query.get("state") == state:
                received.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Selesai, jendela ini boleh ditutup.".encode())

        def log_message(self, *_) -> None:
            pass

    server = http.server.HTTPServer(("localhost", 0), Handler)
    redirect = f"http://localhost:{server.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "state": state, "access_type": "online", "prompt": "consent",
    })
    print("BUKA:", url, flush=True)
    while not received:
        server.handle_request()
    if "code" not in received:
        raise SystemExit(f"izin tidak diberikan: {received.get('error')}")
    resp = requests.post(client.get("token_uri", "https://oauth2.googleapis.com/token"), timeout=60, data={
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code": received["code"],
        "redirect_uri": redirect, "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    args = parser.parse_args()
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    token = ask_for_token()
    headers = {"Authorization": f"Bearer {token}"}
    failed = 0
    for item in plan(cat.load_catalog(), dt.date.fromisoformat(args.start)):
        entry = queue.get(item["key"])
        if not entry:
            continue
        at = item["at"].astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        resp = requests.put(VIDEOS, params={"part": "status"}, headers=headers, timeout=60, json={
            "id": entry["id"],
            "status": {"privacyStatus": "private", "publishAt": at, "selfDeclaredMadeForKids": False},
        })
        if resp.ok:
            entry["publishAt"] = resp.json()["status"].get("publishAt")
            print(f"ok {item['key']} {entry['publishAt']}", flush=True)
        else:
            failed += 1
            print(f"x {item['key']}: HTTP {resp.status_code} {resp.text[:200]}", flush=True)
        QUEUE.write_text(json.dumps(queue, indent=1, sort_keys=True), encoding="utf-8")
    print(f"selesai, gagal {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
