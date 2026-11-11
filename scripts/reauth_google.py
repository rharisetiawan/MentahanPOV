"""Re-authenticate Google Drive and/or YouTube, from a machine with a browser.

Replaces the hand-pasted snippet that used to live in DEPLOYMENT.md. Same
job — run the OAuth consent flow and write fresh token files — plus:

  - redirects to 127.0.0.1 instead of `localhost`, which on Windows often
    resolves to IPv6 and dies with ERR_CONNECTION_REFUSED *after* you've
    already clicked Allow (the flow only serves one request, so that meant
    starting over);
  - records when consent was granted, which is what lets /status and the
    dashboard count down to a Testing-mode token's 7-day expiry and warn
    before it dies instead of after (core/health.py);
  - keeps going to the next service if one account picker is cancelled,
    and says clearly which token files are fresh.

Usage (from the repo root, on the laptop — never on the headless box):
    python scripts/reauth_google.py                 # both Drive and YouTube
    python scripts/reauth_google.py --only drive
    python scripts/reauth_google.py --only youtube

Then copy the printed files to the box, e.g.:
    scp credentials/gdrive-token.json credentials/youtube-token.json \\
        root@<box>:/www/apps/mentahanpov/credentials/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config  # noqa: E402
from core import google_auth  # noqa: E402

SERVICES = {
    "drive": (
        "Google Drive (account that owns the archive folders)",
        ["https://www.googleapis.com/auth/drive"],
        lambda: config.gdrive_token_file,
    ),
    "youtube": (
        "YouTube (account that owns the channel)",
        ["https://www.googleapis.com/auth/youtube.upload"],
        lambda: config.youtube_token_file,
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=sorted(SERVICES), help="Just one service.")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the URL instead of opening a browser.",
    )
    args = parser.parse_args()

    secrets = config.youtube_client_secrets
    if not secrets.exists():
        print(
            f"OAuth client secrets not found at {secrets}.\n"
            "Copy credentials/youtube-oauth.json from the box first "
            "(see DEPLOYMENT.md -> 'Re-authenticating Google OAuth').",
            file=sys.stderr,
        )
        return 2

    names = [args.only] if args.only else list(SERVICES)
    done: list[Path] = []
    failed: list[str] = []
    for name in names:
        label, scopes, token_file = SERVICES[name]
        token_path = token_file()
        print(f"\n=== {label} ===")
        print("Pick the right account in the browser — the first try is the "
              "only one: cancelling or choosing wrong ends this attempt.")
        try:
            google_auth.authorize(
                token_path=token_path,
                secrets_path=secrets,
                scopes=scopes,
                open_browser=not args.no_browser,
            )
        except Exception as exc:  # noqa: BLE001 — report and continue with the next service
            print(f"[FAILED] {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed.append(name)
            continue
        print(f"[OK] wrote {token_path}")
        done.append(token_path)

    if done:
        print("\nFresh token file(s):")
        for path in done:
            print(f"  {path}")
        print("Copy them to the box's credentials/ folder — no restart needed.")
    if failed:
        print(f"\nStill needs another try: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
