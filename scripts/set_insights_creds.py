"""Save the credentials that make view counts work, after checking them.

Run it yourself on the box, where `.env` lives:

    cd /www/apps/mentahanpov && python3 scripts/set_insights_creds.py

It asks for YOUTUBE_API_KEY and FB_PAGE_ACCESS_TOKEN (hidden input; press
Enter to skip either), and only writes a value that passes every check:

- YouTube key: can read the MentahanPOV channel.
- Meta token: a Page token for FB_PAGE_ID, with the posting permissions AND
  the insights ones, able to read an Instagram Reel's views. A User token
  or a token missing a posting scope would otherwise break the next post.

.env is backed up first (.env.bak-<timestamp>, mode 600), then the containers
are recreated to read it. Standard library only: the host has no `requests`.
Nothing secret is ever printed, and a rejected value leaves .env untouched.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from refresh_threads_token import EnvFile  # noqa: E402

YOUTUBE_HANDLE = "@MentahanPov"
POSTING_SCOPES = {
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
}
INSIGHT_SCOPES = {"read_insights", "instagram_manage_insights"}

# url + params -> (http status, decoded JSON body)
Fetch = Callable[[str, dict], tuple[int, dict]]


def http_get(url: str, params: dict) -> tuple[int, dict]:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full, timeout=30) as resp:  # noqa: S310 - fixed https hosts
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # Type only: the exception text can carry the URL, and the URL the secret.
        return 0, {"error": {"message": f"network: {type(exc).__name__}"}}


def _reason(body: dict, secret: str) -> str:
    err = body.get("error")
    message = err.get("message", "") if isinstance(err, dict) else ""
    return str(message).replace(secret, "***")[:160] or "no detail"


def check_youtube_key(key: str, fetch: Fetch) -> str | None:
    """None when usable, otherwise why not."""
    status, body = fetch(
        "https://www.googleapis.com/youtube/v3/channels",
        {"part": "id", "forHandle": YOUTUBE_HANDLE, "key": key},
    )
    if status != 200:
        return f"YouTube refused the key (HTTP {status}): {_reason(body, key)}"
    if not body.get("items"):
        return f"key works, but channel {YOUTUBE_HANDLE} was not found"
    return None


def check_meta_token(
    token: str, *, page_id: str, ig_user_id: str, version: str, fetch: Fetch
) -> str | None:
    """None when usable, otherwise why not."""
    base = f"https://graph.facebook.com/{version}"
    status, body = fetch(
        f"{base}/debug_token", {"input_token": token, "access_token": token}
    )
    if status != 200:
        return f"Meta refused the token (HTTP {status}): {_reason(body, token)}"
    data = body.get("data") or {}
    if not data.get("is_valid"):
        return "token is not valid (expired or revoked)"
    if data.get("type") != "PAGE":
        return (
            f"this is a {data.get('type', 'unknown')} token, not a Page token: "
            "take the access_token of the Page from me/accounts"
        )
    scopes = set(data.get("scopes") or [])
    missing = (POSTING_SCOPES | INSIGHT_SCOPES) - scopes
    if missing:
        return f"token is missing permissions: {', '.join(sorted(missing))}"
    expires_at = data.get("expires_at") or 0
    if expires_at and expires_at - time.time() < 7 * 86400:
        return "token expires within 7 days: it is short-lived, extend it first"

    status, body = fetch(f"{base}/me", {"fields": "id", "access_token": token})
    if status != 200 or str(body.get("id")) != str(page_id):
        return "token belongs to a different Page than FB_PAGE_ID"

    status, body = fetch(
        f"{base}/{ig_user_id}/media", {"fields": "id", "limit": 1, "access_token": token}
    )
    if status != 200:
        return f"can't read Instagram media (HTTP {status}): {_reason(body, token)}"
    media = body.get("data") or []
    if media:
        status, body = fetch(
            f"{base}/{media[0]['id']}/insights", {"metric": "views", "access_token": token}
        )
        if status != 200:
            return f"can't read Instagram views (HTTP {status}): {_reason(body, token)}"
    return None


def write_env(env_path: Path, values: dict[str, str]) -> Path:
    """Back up .env, then swap in the new values atomically."""
    backup = env_path.with_name(f"{env_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(env_path, backup)
    os.chmod(backup, 0o600)
    # Bytes, not read_text(): universal-newline mode would turn CRLF into LF.
    env = EnvFile(env_path.read_bytes().decode("utf-8"))
    for key, value in values.items():
        env.set(key, value)
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_bytes(env.text().encode("utf-8"))
    shutil.copymode(env_path, tmp)
    os.replace(tmp, env_path)
    return backup


def pipeline_running() -> bool:
    return subprocess.run(["pgrep", "-f", "main[.]py"], capture_output=True).returncode == 0


def run(env_path: Path, *, restart: bool, fetch: Fetch = http_get, ask=getpass.getpass) -> int:
    env = EnvFile(env_path.read_bytes().decode("utf-8"))
    page_id, ig_user_id = env.get("FB_PAGE_ID"), env.get("IG_USER_ID")
    version = env.get("GRAPH_API_VERSION") or "v19.0"

    values: dict[str, str] = {}
    failed = False

    key = ask("YOUTUBE_API_KEY (hidden, Enter to skip): ").strip()
    if key:
        problem = check_youtube_key(key, fetch)
        print(f"YouTube key: {problem or 'OK'}")
        failed |= bool(problem)
        if not problem:
            values["YOUTUBE_API_KEY"] = key

    token = ask("FB_PAGE_ACCESS_TOKEN (hidden, Enter to skip): ").strip()
    if token:
        problem = check_meta_token(
            token, page_id=page_id, ig_user_id=ig_user_id, version=version, fetch=fetch
        )
        print(f"Meta token: {problem or 'OK'}")
        failed |= bool(problem)
        if not problem:
            values["FB_PAGE_ACCESS_TOKEN"] = token

    if not values:
        print(".env not changed.")
        return 1 if failed else 0

    backup = write_env(env_path, values)
    print(f"Saved: {', '.join(values)} (backup: {backup.name})")

    if not restart:
        print("Now run: docker compose up -d --no-build")
    elif pipeline_running():
        print("A video is being processed, so the bot was NOT restarted.")
        print("When it finishes, run: docker compose up -d --no-build")
    else:
        print("Restarting the bot...")
        done = subprocess.run(
            ["docker", "compose", "up", "-d", "--no-build"], cwd=env_path.parent
        )
        if done.returncode != 0:
            print("Restart failed; run: docker compose up -d --no-build")
            return 1
        print("Bot restarted.")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()
    if not args.env.is_file():
        print(f"{args.env} not found: run this from the project folder")
        return 2
    return run(args.env.resolve(), restart=not args.no_restart)


if __name__ == "__main__":
    sys.exit(main())
