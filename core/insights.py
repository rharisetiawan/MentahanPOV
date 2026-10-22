"""How each post actually performed, per platform — and a feedback loop from it.

Distribution used to be fire-and-forget: six platforms get the same video
and nothing ever looks at which one (or which caption) worked. This closes
that loop in three steps:

  1. main.py records each platform's own post id in state/posts.json
     (the permalink alone can't be used to look an Instagram/Threads post
     back up — see core/state.py's mark_platform).
  2. `collect()` — run hourly by the bot — fetches view/like/comment counts
     once ~24h and ~72h after a post went out, and stores them under the
     entry's "insights" key. Stories are skipped (they expire after a day and
     their numbers aren't comparable), as is TikTok (no stable post id comes
     back from the Studio automation).
  3. `top_hooks()` hands the opening lines of the best-performing posts to
     core/gemini.py as style reference for the next caption, and
     `ranked()` powers the /insights command and the dashboard table.

Honest limitations, so they don't surface as mysteries later:
  - YouTube needs YOUTUBE_API_KEY (the upload-only OAuth scope can't read
    statistics). Meta/Threads need tokens whose app has the insights
    permissions (read_insights / instagram_manage_insights / threads_manage_insights).
    Without them a snapshot is stored as an error, not silently dropped.
  - Meta renames its insights metrics regularly, so the Facebook/Instagram
    fetchers ask for several candidate metrics one by one and keep whatever
    answers. Treat the numbers as "good enough to rank posts", not an audit.
  - None of this has been exercised against the live APIs from a dev
    machine — the first real run on the box is the real test.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from config import config
from core import state as pipeline_state
from core import threads_token
from distributors.meta_graph import graph_url

log = logging.getLogger(__name__)

INSIGHT_PLATFORMS = ("youtube", "facebook", "instagram", "threads")
WINDOWS_H = (24, 72)
# How long after a window opens a snapshot can still be taken. Past this the
# number would be a "5 days" figure filed under "24h" — worse than missing.
GRACE_H = 24
MAX_ATTEMPTS = 3

_YT_ID = re.compile(r"(?:shorts/|[?&]v=|youtu\.be/)([\w-]{11})")
_FB_ID = re.compile(r"/videos/(\d+)")


class Unavailable(Exception):
    """This platform can't give numbers for this post right now (and why)."""


def _scrub(text: str) -> str:
    for secret in (
        config.fb_page_access_token,
        threads_token.current(config),
        config.youtube_api_key,
    ):
        if secret:
            text = text.replace(secret, "***")
    return text


def _get_json(url: str, params: dict[str, Any], *, label: str) -> dict[str, Any]:
    """GET + decode, converting every failure into Unavailable without ever
    letting the request URL (which carries the token in its query string)
    escape through an exception message — same rule as meta_graph.py."""
    try:
        resp = requests.get(url, params=params, timeout=20)
    except requests.RequestException as exc:
        raise Unavailable(f"{label}: {type(exc).__name__}") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        err = body.get("error") if isinstance(body, dict) else None
        detail = ""
        if isinstance(err, dict):
            detail = " " + str(err.get("message", ""))[:140]
        raise Unavailable(_scrub(f"{label}: HTTP {resp.status_code}{detail}"))
    return body if isinstance(body, dict) else {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _metric_values(payload: dict[str, Any]) -> dict[str, float]:
    """Flatten Graph's `{"data": [{"name", "values": [{"value"}]}]}` (and the
    Threads `total_value` variant) into {name: number}."""
    out: dict[str, float] = {}
    for item in payload.get("data", []) or []:
        if not isinstance(item, dict) or "name" not in item:
            continue
        value = None
        values = item.get("values")
        if isinstance(values, list) and values and isinstance(values[0], dict):
            value = _num(values[0].get("value"))
        if value is None and isinstance(item.get("total_value"), dict):
            value = _num(item["total_value"].get("value"))
        if value is not None:
            out[str(item["name"])] = value
    return out


# --- per-platform fetchers -------------------------------------------------


def _youtube(post_id: str) -> dict[str, float]:
    if not config.youtube_api_key:
        raise Unavailable("YOUTUBE_API_KEY belum di-set")
    body = _get_json(
        "https://www.googleapis.com/youtube/v3/videos",
        {"part": "statistics", "id": post_id, "key": config.youtube_api_key},
        label="youtube",
    )
    items = body.get("items") or []
    if not items:
        raise Unavailable("youtube: video tidak ditemukan / private")
    stats = items[0].get("statistics", {})
    return {
        "views": _num(stats.get("viewCount")) or 0,
        "likes": _num(stats.get("likeCount")) or 0,
        "comments": _num(stats.get("commentCount")) or 0,
    }


def _facebook(post_id: str) -> dict[str, float]:
    token = config.fb_page_access_token
    if not token:
        raise Unavailable("FB_PAGE_ACCESS_TOKEN kosong")
    metrics: dict[str, float] = {}

    base = _get_json(
        graph_url(post_id),
        {
            "fields": "likes.summary(true).limit(0),comments.summary(true).limit(0)",
            "access_token": token,
        },
        label="facebook",
    )
    for field_name, out_name in (("likes", "likes"), ("comments", "comments")):
        total = _num(base.get(field_name, {}).get("summary", {}).get("total_count"))
        if total is not None:
            metrics[out_name] = total

    # No `metric=` on purpose: asking for a metric Meta has since renamed
    # fails the whole call, while the bare edge returns whatever exists.
    try:
        found = _metric_values(
            _get_json(
                graph_url(f"{post_id}/video_insights"),
                {"access_token": token},
                label="facebook insights",
            )
        )
    except Unavailable as exc:
        log.info("[insights] facebook video_insights unavailable: %s", exc)
        found = {}
    for name in ("total_video_views", "post_video_views", "video_views"):
        if name in found:
            metrics["views"] = found[name]
            break
    if not metrics:
        raise Unavailable("facebook: tidak ada angka yang bisa dibaca")
    return metrics


def _instagram(post_id: str) -> dict[str, float]:
    token = config.fb_page_access_token
    if not token:
        raise Unavailable("FB_PAGE_ACCESS_TOKEN kosong")
    metrics: dict[str, float] = {}

    base = _get_json(
        graph_url(post_id),
        {"fields": "like_count,comments_count", "access_token": token},
        label="instagram",
    )
    likes, comments = _num(base.get("like_count")), _num(base.get("comments_count"))
    if likes is not None:
        metrics["likes"] = likes
    if comments is not None:
        metrics["comments"] = comments

    # One request per metric: a single unsupported name in a comma list makes
    # Graph reject the whole call, and which names are valid for Reels has
    # changed more than once (plays -> views, ...).
    for name in ("views", "plays", "reach", "saved", "shares"):
        try:
            got = _metric_values(
                _get_json(
                    graph_url(f"{post_id}/insights"),
                    {"metric": name, "access_token": token},
                    label=f"instagram {name}",
                )
            )
        except Unavailable:
            continue
        if name in got:
            metrics[name] = got[name]
    if "views" not in metrics and "plays" in metrics:
        metrics["views"] = metrics["plays"]
    if not metrics:
        raise Unavailable("instagram: tidak ada angka yang bisa dibaca")
    return metrics


def _threads(post_id: str) -> dict[str, float]:
    token = threads_token.current(config)
    if not token:
        raise Unavailable("THREADS_ACCESS_TOKEN kosong")
    got = _metric_values(
        _get_json(
            f"https://graph.threads.net/{config.threads_api_version}/{post_id}/insights",
            {"metric": "views,likes,replies,reposts,quotes", "access_token": token},
            label="threads",
        )
    )
    if not got:
        raise Unavailable("threads: tidak ada angka yang bisa dibaca")
    if "replies" in got:
        got["comments"] = got["replies"]
    return got


_FETCHERS = {
    "youtube": _youtube,
    "facebook": _facebook,
    "instagram": _instagram,
    "threads": _threads,
}


def fetch(platform: str, post_id: str) -> dict[str, float]:
    fetcher = _FETCHERS.get(platform)
    if fetcher is None:
        raise Unavailable(f"{platform}: tidak didukung")
    return fetcher(post_id)


def _configured(platform: str) -> bool:
    return {
        "youtube": bool(config.youtube_api_key),
        "facebook": bool(config.fb_page_access_token),
        "instagram": bool(config.fb_page_access_token),
        "threads": bool(threads_token.current(config)),
    }.get(platform, False)


# --- collection ------------------------------------------------------------


def resolve_post_id(platform: str, info: dict[str, Any]) -> str | None:
    """The platform's own id for a post: recorded directly for anything
    posted since this module existed, parsed back out of the permalink for
    YouTube/Facebook posts that predate it (Instagram/Threads can't be)."""
    if info.get("post_id"):
        return str(info["post_id"])
    url = info.get("url") or ""
    pattern = {"youtube": _YT_ID, "facebook": _FB_ID}.get(platform)
    match = pattern.search(url) if pattern else None
    return match.group(1) if match else None


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Due:
    key: str
    platform: str
    post_id: str
    window: str
    attempts: int


def due(now: datetime | None = None) -> list[Due]:
    now = now or datetime.now(timezone.utc)
    out: list[Due] = []
    for key, entry in pipeline_state.load_all(config.state_file).items():
        for platform, info in entry.get("platforms", {}).items():
            if platform not in INSIGHT_PLATFORMS or info.get("status") != "ok":
                continue
            if not _configured(platform):
                continue
            posted = _parse_ts(info.get("at"))
            post_id = resolve_post_id(platform, info)
            if posted is None or post_id is None:
                continue
            age_h = (now - posted).total_seconds() / 3600
            recorded = entry.get("insights", {}).get(platform, {})
            for hours in WINDOWS_H:
                label = f"{hours}h"
                snap = recorded.get(label, {})
                if "metrics" in snap or snap.get("attempts", 0) >= MAX_ATTEMPTS:
                    continue
                if hours <= age_h < hours + GRACE_H:
                    out.append(
                        Due(key, platform, post_id, label, snap.get("attempts", 0))
                    )
    return out


@dataclass
class CollectSummary:
    ok: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def collect(now: datetime | None = None, *, max_items: int = 20) -> CollectSummary:
    """Take every snapshot that's due. Blocking network calls — run it off
    the event loop (the bot uses asyncio.to_thread)."""
    summary = CollectSummary()
    if not config.insights_enabled:
        return summary
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for item in due(now)[:max_items]:
        try:
            snapshot: dict[str, Any] = {
                "at": stamp,
                "metrics": fetch(item.platform, item.post_id),
            }
            summary.ok += 1
        except Unavailable as exc:
            snapshot = {"at": stamp, "error": str(exc)[:200], "attempts": item.attempts + 1}
            summary.failed += 1
            summary.errors.append(f"{item.platform}/{item.window}: {exc}")
        except Exception as exc:  # noqa: BLE001 — one bad post must not stop the rest
            log.exception("[insights] unexpected failure for %s", item)
            snapshot = {
                "at": stamp,
                "error": type(exc).__name__,
                "attempts": item.attempts + 1,
            }
            summary.failed += 1
            summary.errors.append(f"{item.platform}/{item.window}: {type(exc).__name__}")
        pipeline_state.record_insight(
            config.state_file, item.key, item.platform, item.window, snapshot
        )
    if summary.ok or summary.failed:
        log.info("[insights] collected: %d ok, %d failed", summary.ok, summary.failed)
    return summary


# --- reading it back -------------------------------------------------------


def best_metrics(entry: dict[str, Any], platform: str) -> dict[str, float] | None:
    """The latest snapshot that actually has numbers (72h beats 24h)."""
    recorded = entry.get("insights", {}).get(platform, {})
    for label in ("72h", "24h"):
        metrics = recorded.get(label, {}).get("metrics")
        if metrics:
            return metrics
    return None


def hook_of(caption: str) -> str:
    for line in (caption or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


@dataclass
class Row:
    key: str
    hook: str
    total_views: float
    per_platform: dict[str, float]
    posted_at: datetime | None


def ranked(*, days: int = 30, now: datetime | None = None) -> list[Row]:
    """Posts that have performance data, best first (by total views across
    the platforms that reported any)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows: list[Row] = []
    for key, entry in pipeline_state.load_all(config.state_file).items():
        per_platform: dict[str, float] = {}
        for platform in INSIGHT_PLATFORMS:
            metrics = best_metrics(entry, platform)
            if metrics is None:
                continue
            views = metrics.get("views")
            if views is None:
                views = metrics.get("likes", 0)
            per_platform[platform] = views
        if not per_platform:
            continue
        times = [
            _parse_ts(i.get("at")) for i in entry.get("platforms", {}).values()
        ]
        posted = min((t for t in times if t), default=None)
        if posted is not None and posted < cutoff:
            continue
        rows.append(
            Row(
                key=key,
                hook=hook_of(entry.get("caption", "")),
                total_views=sum(per_platform.values()),
                per_platform=per_platform,
                posted_at=posted,
            )
        )
    rows.sort(key=lambda r: r.total_views, reverse=True)
    return rows


def platform_averages(*, days: int = 30) -> dict[str, tuple[int, float]]:
    """platform -> (posts measured, average views)."""
    sums: dict[str, list[float]] = {}
    for row in ranked(days=days):
        for platform, views in row.per_platform.items():
            sums.setdefault(platform, []).append(views)
    return {p: (len(v), sum(v) / len(v)) for p, v in sums.items()}


def top_hooks(count: int | None = None) -> list[str]:
    """Opening lines of the best past posts, for core/gemini.py's prompt.

    Empty until at least INSIGHTS_MIN_SAMPLES posts have numbers: ranking
    three videos and calling the winner "what works" would just teach the
    model to imitate noise. Only the hook line is returned — never the whole
    caption — so no place name, hashtag or file name can leak into a
    different video's caption.
    """
    count = config.caption_top_hooks if count is None else count
    if count <= 0 or not config.insights_enabled:
        return []
    rows = ranked(days=90)
    if len(rows) < config.insights_min_samples:
        return []
    hooks: list[str] = []
    for row in rows:
        if row.total_views <= 0 or not row.hook:
            continue
        hook = row.hook[:140]
        if hook not in hooks:
            hooks.append(hook)
        if len(hooks) >= count:
            break
    return hooks
