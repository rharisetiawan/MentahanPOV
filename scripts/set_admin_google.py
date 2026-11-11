"""Save the OAuth client behind the admin dashboard's Sign in with Google.

Run it yourself on the box, where `.env` lives:

    cd /www/apps/mentahanpov && python3 scripts/set_admin_google.py

Asks for the client ID and secret (hidden input), and asks Google whether the
pair is real before saving: exchanging a made-up code answers `invalid_grant`
for a good client and `invalid_client` for a bad one. Then it backs up .env and
restarts the dashboard. Nothing secret is printed; a rejected pair changes nothing.
"""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from refresh_threads_token import EnvFile  # noqa: E402
from set_insights_creds import write_env  # noqa: E402

TOKEN_URL = "https://oauth2.googleapis.com/token"
SERVICE = "mentahanpov-admin"


def token_error(client_id: str, secret: str) -> str:
    """Google's OAuth error code for a deliberately invalid code."""
    body = urllib.parse.urlencode(
        {
            "code": "probe-invalid-code",
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": "https://localhost/",
            "grant_type": "authorization_code",
        }
    ).encode("ascii")
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed https URL
            urllib.request.Request(TOKEN_URL, data=body, method="POST"), timeout=30
        ):
            return ""
    except urllib.error.HTTPError as exc:
        try:
            return str(json.loads(exc.read().decode("utf-8")).get("error", ""))
        except ValueError:
            return f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"network: {type(exc).__name__}"


def check_client(client_id: str, secret: str, probe: Callable[[str, str], str]) -> str | None:
    """None when usable, otherwise why not."""
    if not client_id.endswith(".apps.googleusercontent.com"):
        return "that doesn't look like a client ID (…apps.googleusercontent.com)"
    if not secret:
        return "client secret is empty"
    error = probe(client_id, secret)
    if error in ("invalid_client", "unauthorized_client"):
        return "Google doesn't recognise this client ID + secret pair"
    if error != "invalid_grant":
        return f"couldn't confirm with Google ({error or 'unexpected answer'})"
    return None


def run(env_path: Path, *, restart: bool, probe=token_error, ask=getpass.getpass) -> int:
    client_id = ask("ADMIN_GOOGLE_CLIENT_ID (hidden): ").strip()
    secret = ask("ADMIN_GOOGLE_CLIENT_SECRET (hidden): ").strip()
    problem = check_client(client_id, secret, probe)
    if problem:
        print(f"Not saved: {problem}")
        return 1
    print("Client: OK")

    env = EnvFile(env_path.read_bytes().decode("utf-8"))
    missing = [k for k in ("ADMIN_PUBLIC_URL", "ADMIN_GOOGLE_EMAILS") if not env.get(k)]
    backup = write_env(
        env_path, {"ADMIN_GOOGLE_CLIENT_ID": client_id, "ADMIN_GOOGLE_CLIENT_SECRET": secret}
    )
    print(f"Saved (backup: {backup.name})")
    if missing:
        print(f"Google login stays off until these are set too: {', '.join(missing)}")
    if restart:
        done = subprocess.run(["systemctl", "restart", SERVICE])
        print("Dashboard restarted." if done.returncode == 0 else f"Run: systemctl restart {SERVICE}")
        return done.returncode
    return 0


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
