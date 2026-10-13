"""Guards for the 2026-10-11 merge of b81febb (deployed) with the later work.

The merge combined two independent implementations of the same ideas (disk
cleanup, /cleanup, daily job, disk alert). The failure mode that tests rarely
catch there is *wiring*: a handler or job registered twice, or one of them
silently dropped. These pin the final wiring, and the deployed TikTok switch.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telegram.ext import Application, CommandHandler

import main as pipeline_main
import telegram_bot as bot
from config import _drop_disabled


class MainWiringTests(unittest.TestCase):
    def _run_main(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = SimpleNamespace(
            telegram_bot_token="123456:TEST-TOKEN-NOT-REAL",
            telegram_api_id="",
            telegram_api_hash="",
            telegram_local_api_url="http://localhost:8081",
            telegram_allowed_user_ids=(1,),
            daily_status_hour=8,
            auto_cleanup=True,
            cleanup_hour=4,
            heartbeat_interval_min=5,
            insights_enabled=True,
            state_file=Path(tmp.name) / "posts.json",
        )
        captured: dict = {}

        def fake_run_polling(self, *args, **kwargs):
            captured["app"] = self

        with (
            mock.patch.object(bot, "config", cfg),
            mock.patch.object(Application, "run_polling", fake_run_polling),
        ):
            bot.main()
        return captured["app"]

    def test_every_expected_job_is_registered_exactly_once(self) -> None:
        app = self._run_main()
        names = [job.name for job in app.job_queue.jobs()]
        expected = {
            "housekeeping",  # heartbeat + disk-pressure relief
            "daily_cleanup_job",  # 04:00 WIB (CLEANUP_HOUR)
            "disk_watch_job",  # 6-hourly disk alert (deployed in b81febb)
            "daily_status_job",  # 08:00 WIB report
            "insights_job",
        }
        self.assertEqual(set(names), expected)
        self.assertEqual(len(names), len(set(names)), f"duplicate job in {names}")

    def test_every_command_is_handled_once(self) -> None:
        app = self._run_main()
        commands: list[str] = []
        for group in app.handlers.values():
            for handler in group:
                if isinstance(handler, CommandHandler):
                    commands.extend(handler.commands)
        for wanted in ("status", "dashboard", "cleanup", "jadwal", "insights"):
            self.assertEqual(commands.count(wanted), 1, f"/{wanted}: {commands}")

    def test_daily_cleanup_can_be_switched_off(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = SimpleNamespace(
            telegram_bot_token="123456:TEST-TOKEN-NOT-REAL",
            telegram_api_id="",
            telegram_api_hash="",
            telegram_local_api_url="http://localhost:8081",
            telegram_allowed_user_ids=(),
            daily_status_hour=8,
            auto_cleanup=False,
            cleanup_hour=4,
            heartbeat_interval_min=5,
            insights_enabled=False,
            state_file=Path(tmp.name) / "posts.json",
        )
        captured: dict = {}
        with (
            mock.patch.object(bot, "config", cfg),
            mock.patch.object(
                Application,
                "run_polling",
                lambda self, *a, **k: captured.setdefault("app", self),
            ),
        ):
            bot.main()
        names = {job.name for job in captured["app"].job_queue.jobs()}
        self.assertNotIn("daily_cleanup_job", names)
        self.assertNotIn("insights_job", names)
        self.assertIn("housekeeping", names, "the heartbeat must always run")


class TikTokSwitchStillWorksTests(unittest.TestCase):
    """b81febb's TIKTOK_ENABLED, which the merged code must not regress."""

    def test_disabled_drops_tiktok_from_configured_lists(self) -> None:
        with mock.patch.dict("os.environ", {"TIKTOK_ENABLED": "false"}):
            self.assertEqual(
                _drop_disabled(("youtube", "tiktok", "facebook")), ("youtube", "facebook")
            )

    def test_enabled_keeps_tiktok(self) -> None:
        with mock.patch.dict("os.environ", {"TIKTOK_ENABLED": "true"}):
            self.assertIn("tiktok", _drop_disabled(("youtube", "tiktok")))

    def test_registry_and_keyboard_follow_the_switch(self) -> None:
        if pipeline_main.config.tiktok_enabled:
            self.skipTest("TIKTOK_ENABLED=true in this environment")
        self.assertNotIn("tiktok", pipeline_main.PLATFORM_REGISTRY)
        keyboard = bot._confirmation_keyboard("abc123def456", {"youtube"})
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertFalse(any("tiktok" in label for label in labels), labels)

    def test_no_config_check_noise_for_a_deliberately_disabled_tiktok(self) -> None:
        cfg = SimpleNamespace(
            threads_user_id="",
            threads_access_token="",
            tiktok_enabled=False,
            tiktok_worker_host="100.71.198.45",  # credentials present, switched off
            tiktok_storage_state=Path("nonexistent.json"),
        )
        with mock.patch.object(pipeline_main, "config", cfg):
            with self.assertNoLogs("mentahanpov", level="WARNING"):
                pipeline_main._warn_unselected_but_configured(["youtube"])


if __name__ == "__main__":
    unittest.main()
