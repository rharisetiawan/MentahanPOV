"""MentahanPOV — admin dashboard entry point (edit .env/credentials,
control the bot, trigger a run).

Usage (production — see DEPLOYMENT.md -> "Admin dashboard"):
    python admin.py

This is the read-write sibling of `dashboard.py` (which stays read-only
and containerized on purpose). This one runs directly on the host so it
can write `.env`/`credentials/` and issue `docker compose` commands —
see `admin/__init__.py` for the full reasoning.

Binds to 0.0.0.0 like dashboard.py (LAN/Tailscale reachable), protected
by the same DASHBOARD_USER/DASHBOARD_PASSWORD Basic Auth. Unlike the
read-only dashboard, this one can rewrite every credential the pipeline
uses — treat its URL like a root password, not a bookmark.
"""

from __future__ import annotations

import logging

from admin.app import create_app
from config import config

log = logging.getLogger("mentahanpov.admin")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not config.dashboard_password:
        raise SystemExit(
            "DASHBOARD_PASSWORD is empty. Set it in .env before running admin.py."
        )
    app = create_app()
    log.info("[admin] MentahanPOV admin dashboard -> http://0.0.0.0:%s", config.admin_port)
    app.run(host="0.0.0.0", port=config.admin_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
