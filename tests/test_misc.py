"""Heartbeat, Google token age bookkeeping, the Gemini prompt hand-off."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import gemini, google_auth, health, heartbeat

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


class HeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        cfg = SimpleNamespace(
            state_file=Path(self._tmp.name) / "state" / "posts.json",
            heartbeat_ping_url="",
        )
        p = mock.patch.object(heartbeat, "config", cfg)
        p.start()
        self.addCleanup(p.stop)

    def test_no_file_means_unknown_age(self) -> None:
        self.assertIsNone(heartbeat.age_seconds(NOW))

    def test_beat_then_age(self) -> None:
        heartbeat.beat(NOW - timedelta(minutes=7))
        self.assertAlmostEqual(heartbeat.age_seconds(NOW), 420, delta=1)

    def test_corrupt_file_is_treated_as_unknown(self) -> None:
        path = heartbeat.heartbeat_file()
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(heartbeat.last_beat())

    def test_humanize(self) -> None:
        self.assertEqual(heartbeat.humanize(30), "kurang dari semenit")
        self.assertEqual(heartbeat.humanize(45 * 60), "45 menit")
        self.assertEqual(heartbeat.humanize(2 * 3600 + 10 * 60), "2 jam 10 menit")
        self.assertEqual(heartbeat.humanize(19 * 86400 + 3 * 3600), "19 hari 3 jam")

    def test_ping_without_url_is_a_noop(self) -> None:
        self.assertFalse(heartbeat.ping())


class GoogleTokenAgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token = Path(self._tmp.name) / "token.json"

    def _granted(self, days_ago: float | None) -> None:
        data: dict = {"token": "x", "refresh_token": "y"}
        if days_ago is not None:
            data["granted_at"] = (
                datetime.now(timezone.utc) - timedelta(days=days_ago)
            ).isoformat()
        self.token.write_text(json.dumps(data), encoding="utf-8")

    def test_missing_granted_at_is_none(self) -> None:
        self._granted(None)
        self.assertIsNone(google_auth.read_granted_at(self.token))

    def test_unreadable_file_is_none(self) -> None:
        self.assertIsNone(google_auth.read_granted_at(self.token))

    def _result(self, days_ago: float | None, max_age: int = 7) -> health.CheckResult:
        self._granted(days_ago)
        cfg = SimpleNamespace(google_token_max_age_days=max_age)
        with mock.patch.object(health, "config", cfg):
            return health._google_age_result("Google Drive", self.token)

    def test_fresh_token_is_ok_with_countdown(self) -> None:
        r = self._result(2)
        self.assertEqual(r.status, "ok")
        self.assertIn("days left", r.detail)

    def test_token_near_expiry_warns_before_it_dies(self) -> None:
        r = self._result(6)
        self.assertEqual(r.status, "warning")
        self.assertIn("reauth_google", r.detail)

    def test_old_but_working_token_points_at_production(self) -> None:
        r = self._result(30)
        self.assertEqual(r.status, "ok")
        self.assertIn("Production", r.detail)

    def test_unknown_age_stays_plain(self) -> None:
        r = self._result(None)
        self.assertEqual((r.status, r.detail), ("ok", "Refreshes fine right now."))

    def test_countdown_can_be_disabled(self) -> None:
        r = self._result(6, max_age=0)
        self.assertEqual(r.status, "ok")


class PromptTests(unittest.TestCase):
    ARGS = dict(
        date="20261011",
        address="Jl. Contoh, Malang",
        coordinates="-7.9, 112.6",
        duration_s=12,
        resolution="4K",
    )

    def test_no_hooks_leaves_prompt_untouched(self) -> None:
        plain = gemini._load_prompt(**self.ARGS)
        self.assertNotIn("TOP_HOOKS_BLOCK", plain)
        self.assertNotIn("PROVEN HOOKS", plain)
        self.assertEqual(plain, gemini._load_prompt(**self.ARGS, top_hooks=[]))

    def test_hooks_are_inserted_before_the_task(self) -> None:
        prompt = gemini._load_prompt(**self.ARGS, top_hooks=["Rain, tin roof 🌧️"])
        self.assertIn("PROVEN HOOKS", prompt)
        self.assertIn("- Rain, tin roof 🌧️", prompt)
        self.assertLess(prompt.index("PROVEN HOOKS"), prompt.index("TASK"))
        self.assertNotIn("{{", prompt)  # every placeholder was filled


if __name__ == "__main__":
    unittest.main()
