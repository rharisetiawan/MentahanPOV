"""Refresh the Threads long-lived token and switch Threads on in .env.

Run it yourself where `.env` lives (it edits that file in place):

    python scripts/refresh_threads_token.py --env /path/to/.env [--dry-run]

What it does, in order:
1. Backs up .env next to itself (.env.bak-<timestamp>, mode 600).
2. Calls Threads' refresh_access_token with the current token, then checks
   that the NEW token is valid, belongs to the same THREADS_USER_ID and has
   more than 30 days left. The token is only written if all three hold.
3. Adds `threads` to PLATFORMS and TELEGRAM_PLATFORMS (right after
   `instagram`) when those keys exist and don't have it yet.

Nothing secret is ever printed. Restart the containers afterwards so they
re-read .env:  docker compose up -d --no-build
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

REFRESH_URL = "https://graph.threads.net/refresh_access_token"
PLATFORM_KEYS = ("PLATFORMS", "TELEGRAM_PLATFORMS")


class EnvFile:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines(keepends=True)

    def _index(self, key: str) -> int | None:
        for i, line in enumerate(self.lines):
            if re.match(r"\s*" + re.escape(key) + r"\s*=", line):
                return i
        return None

    def has(self, key: str) -> bool:
        return self._index(key) is not None

    def get(self, key: str) -> str:
        i = self._index(key)
        if i is None:
            return ""
        value = self.lines[i].split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value

    def set(self, key: str, value: str) -> None:
        i = self._index(key)
        eol = "\r\n" if i is not None and self.lines[i].endswith("\r\n") else "\n"
        line = f"{key}={value}{eol}"
        if i is None:
            if self.lines and not self.lines[-1].endswith(("\n", "\r")):
                self.lines[-1] += eol
            self.lines.append(line)
        else:
            self.lines[i] = line

    def text(self) -> str:
        return "".join(self.lines)


def refresh_token(token: str, user_id: str, version: str, requests_mod) -> str | None:
    """Return a verified new token, or None (reason printed, no secrets)."""
    base = f"https://graph.threads.net/{version}"
    try:
        r = requests_mod.get(
            REFRESH_URL,
            params={"grant_type": "th_refresh_token", "access_token": token},
            timeout=30,
        )
        if not r.ok:
            print(f"refresh: HTTP {r.status_code} (token NOT changed)")
            return None
        new = r.json().get("access_token")
        me = requests_mod.get(
            f"{base}/me", params={"fields": "id", "access_token": new}, timeout=30
        ).json()
        data = requests_mod.get(
            f"{base}/debug_token",
            params={"input_token": new, "access_token": new},
            timeout=30,
        ).json().get("data", {})
    except Exception as exc:  # noqa: BLE001 - never print exc: URLs carry the token
        print(f"refresh: failed ({type(exc).__name__}), token NOT changed")
        return None

    expires_at = data.get("expires_at")
    days = (expires_at - time.time()) / 86400 if expires_at else None
    ok = (
        bool(new)
        and str(me.get("id")) == str(user_id)
        and bool(data.get("is_valid"))
        and (days is None or days > 30)
    )
    shown = "no expiry" if days is None else f"{days:.1f} days left"
    print(f"refresh: new token valid + same account: {ok} ({shown})")
    return new if ok else None


def enable_threads(env: EnvFile) -> bool:
    changed = False
    for key in PLATFORM_KEYS:
        if not env.has(key):
            print(f"{key}: not set, skipped")
            continue
        current = [p.strip() for p in env.get(key).split(",") if p.strip()]
        if "threads" in current:
            print(f"{key}: already has threads")
            continue
        pos = current.index("instagram") + 1 if "instagram" in current else len(current)
        current.insert(pos, "threads")
        env.set(key, ",".join(current))
        print(f"{key}: -> {','.join(current)}")
        changed = True
    return changed


def run(env_path: Path, *, dry_run: bool, requests_mod=None) -> int:
    if requests_mod is None:
        import requests as requests_mod  # type: ignore[no-redef]

    # Bytes, not read_text(): universal-newline mode would turn CRLF into LF.
    env = EnvFile(env_path.read_bytes().decode("utf-8"))
    token, user_id = env.get("THREADS_ACCESS_TOKEN"), env.get("THREADS_USER_ID")
    if not token or not user_id:
        print("THREADS_ACCESS_TOKEN / THREADS_USER_ID missing in .env - nothing to do")
        return 2

    changed = False
    if dry_run:
        # Refreshing is a real, state-changing call (the new token would be
        # thrown away), so a rehearsal must not make it.
        print("dry-run: token refresh skipped, only showing the platform change")
        new_token = None
    else:
        new_token = refresh_token(
            token, user_id, env.get("THREADS_API_VERSION") or "v1.0", requests_mod
        )
    if new_token:
        env.set("THREADS_ACCESS_TOKEN", new_token)
        changed = True
    changed = enable_threads(env) or changed

    if not changed:
        print("no change needed")
        return 0
    if dry_run:
        print("dry-run: .env NOT written")
        return 0

    backup = env_path.with_name(f"{env_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(env_path, backup)
    os.chmod(backup, 0o600)
    tmp = env_path.with_name(env_path.name + ".new")
    tmp.write_text(env.text(), encoding="utf-8", newline="")
    st = env_path.stat()
    os.chmod(tmp, st.st_mode & 0o777)
    if hasattr(os, "chown"):
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, env_path)
    print(f"written. backup: {backup.name}")
    print("next: docker compose up -d --no-build   (so the containers re-read .env)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return run(args.env, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
