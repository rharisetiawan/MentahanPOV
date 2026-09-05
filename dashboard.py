"""Read-only status dashboard for the MentahanPOV pipeline.

Shows the same checks as Telegram's /status command (core/health.py),
as a page you can open from any device on the same WiFi as the box this
runs on. Never displays actual credential values — only ok/warning/error
per integration — so even if someone else on the network finds the URL,
there's nothing sensitive to see beyond "which service is down".

Run:
    python dashboard.py
or via docker-compose.yml's `dashboard` service (the normal way this runs
in production — see DEPLOYMENT.md).
"""

from __future__ import annotations

import logging
from functools import wraps

from flask import Flask, Response, request

from config import config
from core import health

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mentahanpov.dashboard")

app = Flask(__name__)

_STATUS_COLOR = {"ok": "#2e7d32", "warning": "#e6a700", "error": "#c62828"}

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>MentahanPOV — Status</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #111; color: #eee;
         max-width: 640px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; }}
  .row {{ display: flex; justify-content: space-between; align-items: center;
          padding: .75rem 1rem; margin-bottom: .5rem; border-radius: 8px; background: #1c1c1c; }}
  .name {{ font-weight: 600; }}
  .detail {{ color: #aaa; font-size: .9rem; text-align: right; max-width: 60%; }}
  .dot {{ width: .6rem; height: .6rem; border-radius: 50%; display: inline-block; margin-right: .5rem; }}
  .meta {{ color: #666; font-size: .8rem; margin-top: 1.5rem; text-align: center; }}
</style>
</head>
<body>
<h1>MentahanPOV — Status</h1>
{rows}
<p class="meta">Auto-refresh tiap 60 detik &middot; nggak pernah nampilin isi token/kredensial asli</p>
</body>
</html>
"""


def _require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not config.dashboard_password:
            log.warning("[dashboard] DASHBOARD_PASSWORD is empty — refusing to serve")
            return Response("Dashboard misconfigured: DASHBOARD_PASSWORD is empty.", 500)
        if not auth or auth.username != config.dashboard_user or auth.password != config.dashboard_password:
            return Response(
                "Login required.", 401,
                {"WWW-Authenticate": 'Basic realm="MentahanPOV Status"'},
            )
        return view(*args, **kwargs)

    return wrapped


@app.route("/")
@_require_auth
def index() -> str:
    results = health.run_all()
    rows = "\n".join(
        f'<div class="row">'
        f'<span class="name"><span class="dot" style="background:{_STATUS_COLOR[r.status]}"></span>{r.name}</span>'
        f'<span class="detail">{r.detail}</span>'
        f"</div>"
        for r in results
    )
    return _PAGE.format(rows=rows)


@app.route("/healthz")
def healthz() -> Response:
    # Unauthenticated, minimal — for docker-compose healthcheck: only
    # confirms the web server itself is up, not what core/health.py finds.
    return Response("ok", 200)


if __name__ == "__main__":
    if not config.dashboard_password:
        raise SystemExit(
            "DASHBOARD_PASSWORD is empty. Set it in .env before running dashboard.py."
        )
    app.run(host="0.0.0.0", port=config.dashboard_port)
