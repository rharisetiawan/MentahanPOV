"""MentahanPOV — local GUI entry point.

Usage:
    python dashboard.py

Opens a browser dashboard (default http://127.0.0.1:8765) for editing
`.env` values, uploading/replacing credential files, browsing posting
history, and starting/stopping the bot or a pipeline run — all without
touching a terminal or a text editor. See README -> "Dashboard" for the
full walkthrough and the DASHBOARD_* env vars.

This process is separate from telegram_bot.py: it only edits config and
launches/stops subprocesses, it never itself uploads a video anywhere.
"""

from __future__ import annotations

import logging
import webbrowser
from threading import Timer

from config import config
from dashboard.app import create_app

log = logging.getLogger("mentahanpov.dashboard")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = create_app()
    url = f"http://{config.dashboard_host}:{config.dashboard_port}"

    if config.dashboard_host in ("127.0.0.1", "localhost"):
        Timer(1.0, lambda: webbrowser.open(url)).start()
    else:
        log.warning(
            "[dashboard] DASHBOARD_HOST=%s (not localhost) — make sure "
            "DASHBOARD_PASSWORD is set and this is behind your own "
            "firewall/VPN before exposing it.",
            config.dashboard_host,
        )

    log.info("[dashboard] MentahanPOV dashboard -> %s", url)
    app.run(host=config.dashboard_host, port=config.dashboard_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
