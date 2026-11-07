"""Copy media/doctor-idol-s4 to a private Google Drive folder, verified by MD5.

The folder sits in My Drive root, deliberately outside GDRIVE_FOLDER_ID: the bot
watches that folder and would treat these videos as new footage. Nothing here
deletes or shares anything. Safe to rerun: files already there with the same MD5
are skipped. doctor_idol/drive_manifest.json records what was verified.

    python scripts/doctor_idol_backup.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from doctor_idol import catalog as cat  # noqa: E402

ROOT_FOLDER = "MentahanPOV-Arsip"
SUB_FOLDER = "doctor-idol-s4"
CHUNK = 64 * 1024 * 1024  # must be a multiple of 256 KiB
FOLDER_MIME = "application/vnd.google-apps.folder"
FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,md5Checksum,size"
MANIFEST = cat.PACKAGE_DIR / "drive_manifest.json"
TOKEN_FILE = cat.main_checkout() / "credentials" / "gdrive-token.json"


class Drive:
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

    def find(self, name: str, parent: str) -> dict | None:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        resp = requests.get(FILES, headers=self.headers(), params={
            "q": f"name='{safe}' and '{parent}' in parents and trashed=false",
            "fields": "files(id,md5Checksum,size,mimeType)",
        }, timeout=60)
        resp.raise_for_status()
        found = resp.json()["files"]
        return found[0] if found else None

    def folder(self, name: str, parent: str) -> str:
        existing = self.find(name, parent)
        if existing:
            return existing["id"]
        resp = requests.post(FILES, headers=self.headers(),
                             json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}, timeout=60)
        resp.raise_for_status()
        return resp.json()["id"]

    def upload(self, path: Path, parent: str) -> dict:
        size = path.stat().st_size
        start = requests.post(UPLOAD, headers={**self.headers(), "X-Upload-Content-Length": str(size)},
                              json={"name": path.name, "parents": [parent]}, timeout=60)
        start.raise_for_status()
        session = start.headers["Location"]
        sent = 0
        with path.open("rb") as fh:
            while True:
                fh.seek(sent)
                chunk = fh.read(CHUNK)
                end = sent + len(chunk) - 1
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
                    raise RuntimeError(f"{path.name}: jaringan gagal terus")
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code != 308:
                    raise RuntimeError(f"{path.name}: HTTP {resp.status_code} {resp.text[:200]}")
                # Trust the server's Range, not our count: a lost reply may have been stored.
                received = resp.headers.get("Range")
                sent = int(received.rsplit("-", 1)[1]) + 1 if received else 0


def md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - matches Drive's md5Checksum, not used for security
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    root = cat.media_root()
    drive = Drive()
    top = drive.folder(SUB_FOLDER, drive.folder(ROOT_FOLDER, "root"))
    folders = {Path("."): top}
    manifest: dict[str, dict] = {}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in paths)
    print(f"{len(paths)} file, {total / 1e9:.1f} GB", flush=True)
    done = failed = 0
    for path in paths:
        rel = path.relative_to(root)
        parent = top
        walked = Path(".")
        for part in rel.parent.parts:
            walked /= part
            if walked not in folders:
                folders[walked] = drive.folder(part, parent)
            parent = folders[walked]
        local = md5(path)
        remote = drive.find(path.name, parent)
        if not (remote and remote.get("md5Checksum") == local):
            try:
                remote = drive.upload(path, parent)
            except (RuntimeError, requests.RequestException) as exc:
                failed += 1
                print(f"x {rel}: {exc}", flush=True)
                continue
        if remote.get("md5Checksum") != local or int(remote.get("size", -1)) != path.stat().st_size:
            failed += 1
            print(f"x {rel}: checksum beda", flush=True)
            continue
        manifest[rel.as_posix()] = {"id": remote["id"], "md5": local, "size": path.stat().st_size}
        MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
        done += path.stat().st_size
        print(f"ok {rel} ({done / total:.0%})", flush=True)
    print(f"selesai: {len(paths) - failed} cocok, {failed} gagal", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
