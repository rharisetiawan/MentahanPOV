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

import html
import json
import logging
from functools import wraps

from flask import Flask, Response, request

from config import config
from core import health, heartbeat, insights
from core.authutil import safe_equal

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
  h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
  .meta {{ color: #666; font-size: .8rem; margin-top: 1.5rem; text-align: center; }}
</style>
</head>
<body>
<h1>MentahanPOV — Status</h1>
{rows}
{performance}
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
        if (
            not auth
            or not safe_equal(auth.username, config.dashboard_user)
            or not safe_equal(auth.password, config.dashboard_password)
        ):
            return Response(
                "Login required.", 401,
                {"WWW-Authenticate": 'Basic realm="MentahanPOV Status"'},
            )
        return view(*args, **kwargs)

    return wrapped


def _fmt_count(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def _performance_html() -> str:
    """Best posts of the last 30 days (core/insights.py). Hooks are
    caption text that came from a model and a chat message — escaped, never
    interpolated raw into the page."""
    try:
        rows = insights.ranked(days=30)[:5]
    except Exception:  # noqa: BLE001 — a stats problem must never take the status page down
        log.exception("[dashboard] couldn't load performance data")
        return ""
    if not rows:
        return (
            "<h2>Performa post</h2>"
            '<div class="row"><span class="detail">Belum ada data — angka '
            "diambil ±24 &amp; ±72 jam setelah posting.</span></div>"
        )
    out = ["<h2>Performa post (30 hari, views)</h2>"]
    for row in rows:
        detail = " · ".join(
            f"{html.escape(p)} {_fmt_count(v)}"
            for p, v in sorted(row.per_platform.items(), key=lambda kv: -kv[1])
        )
        hook = html.escape(row.hook[:80]) or "(tanpa caption)"
        out.append(
            f'<div class="row"><span class="name">{_fmt_count(row.total_views)} — '
            f'{hook}</span><span class="detail">{detail}</span></div>'
        )
    return "\n".join(out)


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
    return _PAGE.format(rows=rows, performance=_performance_html())


@app.route("/healthz")
def healthz() -> Response:
    # Unauthenticated, minimal — for docker-compose healthcheck: only
    # confirms the web server itself is up, not what core/health.py finds.
    return Response("ok", 200)


@app.route("/heartbeat")
def heartbeat_status() -> Response:
    # Unauthenticated like /healthz, and for the same reason: it's polled by
    # scripts/watchdog.py from another machine, which has no credentials.
    # It exposes one number — seconds since the bot last wrote its
    # heartbeat — nothing about what the bot is doing or holds.
    age = heartbeat.age_seconds()
    body = {"heartbeat_age_s": None if age is None else round(age, 1)}
    return Response(json.dumps(body), 200, mimetype="application/json")


if __name__ == "__main__":
    if not config.dashboard_password:
        raise SystemExit(
            "DASHBOARD_PASSWORD is empty. Set it in .env before running dashboard.py."
        )
    app.run(host="0.0.0.0", port=config.dashboard_port)
