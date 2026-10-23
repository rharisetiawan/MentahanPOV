"""core/insights.py — id resolution, due windows, ranking, prompt hand-off."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import insights

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


class ResolvePostIdTests(unittest.TestCase):
    def test_recorded_id_wins(self) -> None:
        self.assertEqual(
            insights.resolve_post_id("instagram", {"post_id": "1789", "url": "x"}),
            "1789",
        )

    def test_youtube_id_parsed_from_old_urls(self) -> None:
        self.assertEqual(
            insights.resolve_post_id(
                "youtube", {"url": "https://youtube.com/shorts/dQw4w9WgXcQ"}
            ),
            "dQw4w9WgXcQ",
        )

    def test_facebook_id_parsed_from_old_urls(self) -> None:
        self.assertEqual(
            insights.resolve_post_id(
                "facebook", {"url": "https://www.facebook.com/123/videos/456789"}
            ),
            "456789",
        )

    def test_instagram_permalink_cannot_be_resolved(self) -> None:
        self.assertIsNone(
            insights.resolve_post_id(
                "instagram", {"url": "https://www.instagram.com/reel/AbC123/"}
            )
        )


class MetricParsingTests(unittest.TestCase):
    def test_values_shape(self) -> None:
        payload = {"data": [{"name": "views", "values": [{"value": 12}]}]}
        self.assertEqual(insights._metric_values(payload), {"views": 12})

    def test_threads_total_value_shape(self) -> None:
        payload = {"data": [{"name": "likes", "total_value": {"value": 7}}]}
        self.assertEqual(insights._metric_values(payload), {"likes": 7})

    def test_ignores_non_numeric_and_junk(self) -> None:
        payload = {"data": [{"name": "x", "values": [{"value": {"a": 1}}]}, "junk", {}]}
        self.assertEqual(insights._metric_values(payload), {})


class StateBackedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = Path(self._tmp.name) / "posts.json"
        self.cfg = SimpleNamespace(
            state_file=self.state_file,
            insights_enabled=True,
            caption_top_hooks=2,
            insights_min_samples=3,
            youtube_api_key="yt-key",
            fb_page_access_token="fb-token",
            threads_access_token="th-token",
        )
        p = mock.patch.object(insights, "config", self.cfg)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, entries: dict) -> None:
        self.state_file.write_text(json.dumps(entries), encoding="utf-8")

    def _entry(self, caption: str, *, posted_h_ago: float, **platforms) -> dict:
        return {
            "caption": caption,
            "platforms": {
                name: {
                    "status": "ok",
                    "at": _ago(posted_h_ago),
                    "post_id": f"{name}-id",
                    "url": f"https://x/{name}",
                    **extra,
                }
                for name, extra in platforms.items()
            },
        }

    def test_due_only_inside_a_window_and_not_twice(self) -> None:
        self._write(
            {
                "a": self._entry("A", posted_h_ago=25, youtube={}),  # 24h due
                "b": self._entry("B", posted_h_ago=10, youtube={}),  # too early
                "c": self._entry("C", posted_h_ago=80, youtube={}),  # 72h due
                "d": self._entry("D", posted_h_ago=60, youtube={}),  # between windows
            }
        )
        due = {(d.key, d.window) for d in insights.due(NOW)}
        self.assertEqual(due, {("a", "24h"), ("c", "72h")})

    def test_unconfigured_platform_is_skipped_silently(self) -> None:
        self.cfg.youtube_api_key = ""
        self._write({"a": self._entry("A", posted_h_ago=25, youtube={})})
        self.assertEqual(insights.due(NOW), [])

    def test_stories_and_failed_posts_are_never_due(self) -> None:
        entry = self._entry("A", posted_h_ago=25, instagram_story={}, tiktok={})
        entry["platforms"]["youtube"] = {"status": "error", "at": _ago(25), "post_id": "x"}
        self._write({"a": entry})
        self.assertEqual(insights.due(NOW), [])

    def test_collect_records_metrics_then_stops_asking(self) -> None:
        self._write({"a": self._entry("A", posted_h_ago=25, youtube={})})
        with mock.patch.object(insights, "fetch", return_value={"views": 120, "likes": 9}):
            summary = insights.collect(NOW)
        self.assertEqual((summary.ok, summary.failed), (1, 0))
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["a"]["insights"]["youtube"]["24h"]["metrics"]["views"], 120)
        self.assertEqual(insights.due(NOW), [])

    def test_failed_fetch_is_retried_a_limited_number_of_times(self) -> None:
        self._write({"a": self._entry("A", posted_h_ago=25, youtube={})})
        with mock.patch.object(
            insights, "fetch", side_effect=insights.Unavailable("youtube: HTTP 403")
        ):
            for _ in range(insights.MAX_ATTEMPTS):
                self.assertEqual(len(insights.due(NOW)), 1)
                insights.collect(NOW)
        self.assertEqual(insights.due(NOW), [])
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIn("error", saved["a"]["insights"]["youtube"]["24h"])

    def test_one_failure_does_not_stop_the_batch(self) -> None:
        self._write(
            {
                "a": self._entry("A", posted_h_ago=25, youtube={}),
                "b": self._entry("B", posted_h_ago=25, youtube={}),
            }
        )
        calls = iter([RuntimeError("boom"), {"views": 5}])

        def fake_fetch(platform, post_id):
            result = next(calls)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(insights, "fetch", side_effect=fake_fetch):
            summary = insights.collect(NOW)
        self.assertEqual((summary.ok, summary.failed), (1, 1))

    def _with_metrics(self, key: str, caption: str, views: dict[str, float]) -> dict:
        entry = self._entry(caption, posted_h_ago=100, **{p: {} for p in views})
        entry["insights"] = {
            p: {"72h": {"at": _ago(1), "metrics": {"views": v}}} for p, v in views.items()
        }
        return {key: entry}

    def test_ranking_and_top_hooks(self) -> None:
        rows = {}
        rows.update(self._with_metrics("a", "Hook A 🔊\n\nbody", {"youtube": 100}))
        rows.update(self._with_metrics("b", "Hook B\n\nbody", {"youtube": 50, "facebook": 400}))
        rows.update(self._with_metrics("c", "Hook C\n\nbody", {"threads": 10}))
        self._write(rows)

        ranked = insights.ranked(days=30, now=NOW)
        self.assertEqual([r.key for r in ranked], ["b", "a", "c"])
        self.assertEqual(ranked[0].total_views, 450)
        self.assertEqual(insights.top_hooks(), ["Hook B", "Hook A 🔊"])

    def test_top_hooks_wait_for_enough_samples(self) -> None:
        rows = {}
        rows.update(self._with_metrics("a", "Hook A\n\nbody", {"youtube": 100}))
        rows.update(self._with_metrics("b", "Hook B\n\nbody", {"youtube": 50}))
        self._write(rows)  # only 2 posts, minimum is 3
        self.assertEqual(insights.top_hooks(), [])

    def test_top_hooks_off_switch(self) -> None:
        self.cfg.caption_top_hooks = 0
        rows = {}
        for k in "abc":
            rows.update(self._with_metrics(k, f"Hook {k}\n\nbody", {"youtube": 100}))
        self._write(rows)
        self.assertEqual(insights.top_hooks(), [])


class ErrorScrubbingTests(unittest.TestCase):
    def test_tokens_never_survive_into_an_error_message(self) -> None:
        cfg = SimpleNamespace(
            fb_page_access_token="SECRET-FB",
            threads_access_token="",
            youtube_api_key="SECRET-YT",
        )
        with mock.patch.object(insights, "config", cfg):
            cleaned = insights._scrub("HTTP 400 token SECRET-FB and key SECRET-YT")
        self.assertNotIn("SECRET", cleaned)


if __name__ == "__main__":
    unittest.main()
