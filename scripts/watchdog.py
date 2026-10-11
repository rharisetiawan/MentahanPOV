"""Tell you when the HG680 (or just the bot on it) stops answering.

Everything that watches the bot from *inside* the box (core/health.py, the
daily Telegram report) dies with it — which is how the box sat dark for ~19
days unnoticed. Run this from a DIFFERENT machine (the campus worker, a
laptop that's always on, a Raspberry Pi...): it polls the dashboard's
/heartbeat endpoint over Tailscale and messages you on Telegram when the
box is unreachable or the bot's heartbeat has gone stale, and again when it
recovers.

Standard library only, so it runs anywhere with Python 3.8+ and no
`pip install` — it deliberately does not import this repo's config.py.

Config (flags or environment):
    WATCHDOG_URL             e.g. http://100.73.125.7:8090/heartbeat
    WATCHDOG_TELEGRAM_TOKEN  bot token to send alerts with (the main bot's
                             token works — sending doesn't conflict with
                             its polling)
    WATCHDOG_CHAT_ID         your numeric Telegram user id
    WATCHDOG_STATE_FILE      where to remember "already alerted" (default:
                             ~/.mentahanpov-watchdog.json)

Cron, every 5 minutes (alerts after 3 consecutive bad checks ~ 15 min):
    */5 * * * * WATCHDOG_URL=... WATCHDOG_TELEGRAM_TOKEN=... WATCHDOG_CHAT_ID=... \\
        /usr/bin/python3 /path/to/watchdog.py >> /tmp/watchdog.log 2>&1

Exit code: 0 healthy, 1 unhealthy (so it also works under other monitors).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _fetch_age(url: str, timeout: float) -> tuple[bool, str, float | None]:
    """(reachable, problem-if-any, heartbeat age in seconds)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} dari {url}", None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"gak bisa dihubungi ({type(reason).__name__})", None
    except ValueError:
        return False, "balasan bukan JSON", None
    age = body.get("heartbeat_age_s")
    return True, "", float(age) if isinstance(age, (int, float)) else None


def _humanize(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} hari {hours} jam"
    if hours:
        return f"{hours} jam {minutes} menit"
    return f"{minutes} menit"


def _send(token: str, chat_id: str, text: str) -> bool:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(  # noqa: S310
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=20):  # noqa: S310
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Never print the URL/exception text: it contains the bot token.
        print(f"[watchdog] Telegram send failed ({type(exc).__name__})", file=sys.stderr)
        return False


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=env("WATCHDOG_URL"))
    parser.add_argument("--token", default=env("WATCHDOG_TELEGRAM_TOKEN"))
    parser.add_argument("--chat-id", default=env("WATCHDOG_CHAT_ID"))
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(
            env("WATCHDOG_STATE_FILE", str(Path.home() / ".mentahanpov-watchdog.json"))
        ),
    )
    parser.add_argument(
        "--max-age-min",
        type=float,
        default=15,
        help="Heartbeat older than this counts as the bot being down (default 15; "
        "the bot beats every HEARTBEAT_INTERVAL_MIN=5).",
    )
    parser.add_argument(
        "--fail-threshold",
        type=int,
        default=3,
        help="Consecutive bad checks before alerting (default 3).",
    )
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    if not (args.url and args.token and args.chat_id):
        print(
            "Need WATCHDOG_URL, WATCHDOG_TELEGRAM_TOKEN and WATCHDOG_CHAT_ID "
            "(env or --url/--token/--chat-id).",
            file=sys.stderr,
        )
        return 2

    reachable, problem, age = _fetch_age(args.url, args.timeout)
    if reachable and age is None:
        reachable, problem = False, "dashboard hidup tapi bot belum pernah mencatat heartbeat"
    elif reachable and age > args.max_age_min * 60:
        reachable = False
        problem = f"dashboard hidup tapi bot diam {_humanize(age)} (heartbeat basi)"

    state = _load(args.state_file)
    now = time.time()

    if reachable:
        was_down_since = state.get("down_since")
        if state.get("alerted") and was_down_since:
            _send(
                args.token,
                args.chat_id,
                f"✅ MentahanPOV pulih. Sempat bermasalah ± "
                f"{_humanize(now - was_down_since)}.",
            )
        args.state_file.write_text(json.dumps({"failures": 0}), encoding="utf-8")
        return 0

    failures = int(state.get("failures", 0)) + 1
    down_since = state.get("down_since") or now
    alerted = bool(state.get("alerted"))
    if failures >= args.fail_threshold and not alerted:
        alerted = _send(
            args.token,
            args.chat_id,
            f"🚨 MentahanPOV bermasalah: {problem}.\n"
            f"Sudah {failures} kali cek berturut-turut. Cek listrik/kabel LAN, "
            "`tailscaled`, dan container bot di HG680.",
        )
    args.state_file.write_text(
        json.dumps({"failures": failures, "down_since": down_since, "alerted": alerted}),
        encoding="utf-8",
    )
    print(f"[watchdog] unhealthy ({failures}x): {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
