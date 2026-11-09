"""Store a new Threads token after checking it. Run it yourself inside the bot container:

    docker exec -it mentahanpov-mentahanpov-bot-1 python /app/state/doctor_idol/set_threads_token.py

The token is read with hidden input and never printed. It is saved only if it
belongs to the same Threads account as THREADS_USER_ID, to the token file that
already wins over .env (core/threads_token.py), so nothing needs a restart.
"""

from __future__ import annotations

import getpass
import sys

sys.path.insert(0, "/app")

import requests  # noqa: E402

from config import config  # noqa: E402
from core import threads_token  # noqa: E402


def main() -> int:
    token = getpass.getpass("Token Threads baru (tidak tampil saat diketik): ").strip()
    if not token:
        print("Kosong, tidak ada yang diubah.")
        return 1
    base = f"https://graph.threads.net/{config.threads_api_version}"
    me = requests.get(f"{base}/me", params={"fields": "id,username", "access_token": token}, timeout=30)
    if not me.ok:
        print(f"Ditolak Threads (HTTP {me.status_code}). Tidak ada yang diubah.")
        return 1
    if str(me.json().get("id")) != str(config.threads_user_id):
        print(f"Token ini milik @{me.json().get('username')}, bukan akun THREADS_USER_ID. Tidak ada yang diubah.")
        return 1
    debug = requests.get("https://graph.threads.net/debug_token",
                         params={"input_token": token, "access_token": token}, timeout=30)
    expires = (debug.json().get("data") or {}).get("expires_at") if debug.ok else None
    threads_token._save(token, float(expires) if expires else None)
    print(f"Tersimpan untuk @{me.json().get('username')}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
