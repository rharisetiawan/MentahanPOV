"""Startup announcement, the housekeeping tick, /cleanup text, and the
"file is gone" guards — the bot-side behavior around outages and disk."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import telegram_bot as bot
from core import cleanup


def _report(**kw) -> cleanup.Report:
    return cleanup.Report(**kw)


class StartupAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def _announce(self, previous_age: float | None) -> str:
        cfg = SimpleNamespace(heartbeat_interval_min=5, state_file=Path("x.json"))
        app = SimpleNamespace(bot=object())
        sent: list[str] = []

        async def fake_notify(_bot, text):
            sent.append(text)

        with (
            mock.patch.object(bot, "config", cfg),
            mock.patch.object(bot.heartbeat, "age_seconds", return_value=previous_age),
            mock.patch.object(bot.heartbeat, "beat"),
            mock.patch.object(bot.heartbeat, "last_beat", return_value=None),
            mock.patch.object(bot.cleanup, "disk_free_bytes", return_value=5 * 1024**3),
            mock.patch.object(bot.pipeline_state, "scheduled_entries", return_value=[]),
            mock.patch.object(bot, "_notify", new=fake_notify),
        ):
            await bot._announce_startup(app)
        self.assertEqual(len(sent), 1)
        return sent[0]

    async def test_first_ever_start(self) -> None:
        self.assertIn("nyala", await self._announce(None))

    async def test_long_outage_says_how_long(self) -> None:
        text = await self._announce(19 * 86400 + 3 * 3600)
        self.assertIn("online lagi", text)
        self.assertIn("19 hari 3 jam", text)
        self.assertIn("5.0 GB", text)

    async def test_quick_restart_is_not_an_outage(self) -> None:
        text = await self._announce(90)
        self.assertIn("restart", text)
        self.assertNotIn("Sempat mati", text)


class HousekeepingTickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cfg = SimpleNamespace(
            auto_cleanup=True,
            heartbeat_ping_url="https://hc.example/ping/abc",
        )
        p = mock.patch.object(bot, "config", self.cfg)
        p.start()
        self.addCleanup(p.stop)
        self.notified: list[str] = []

        async def fake_notify(_bot, text):
            self.notified.append(text)

        n = mock.patch.object(bot, "_notify", new=fake_notify)
        n.start()
        self.addCleanup(n.stop)
        self.context = SimpleNamespace(bot=object())

    async def test_beats_and_pings_even_when_disk_is_fine(self) -> None:
        with (
            mock.patch.object(bot.heartbeat, "beat") as beat,
            mock.patch.object(bot.heartbeat, "ping") as ping,
            mock.patch.object(bot.cleanup, "under_pressure", return_value=False),
            mock.patch.object(bot.cleanup, "run") as run,
        ):
            await bot.housekeeping_job(self.context)
        beat.assert_called_once()
        ping.assert_called_once()
        run.assert_not_called()
        self.assertEqual(self.notified, [])

    async def test_a_failing_cleanup_never_stops_the_heartbeat(self) -> None:
        with (
            mock.patch.object(bot.heartbeat, "beat") as beat,
            mock.patch.object(bot.heartbeat, "ping"),
            mock.patch.object(bot.cleanup, "under_pressure", return_value=True),
            mock.patch.object(bot.cleanup, "run", side_effect=OSError("disk gone")),
        ):
            await bot.housekeeping_job(self.context)  # must not raise
        beat.assert_called_once()

    async def test_pressure_that_frees_space_notifies_once(self) -> None:
        freed = _report(deleted=4, freed_bytes=3 * 1024**3, applied=True)
        with (
            mock.patch.object(bot.heartbeat, "beat"),
            mock.patch.object(bot.heartbeat, "ping"),
            mock.patch.object(
                bot.cleanup, "under_pressure", side_effect=[True, False]
            ),
            mock.patch.object(bot.cleanup, "run", return_value=freed),
            mock.patch.object(bot.cleanup, "disk_free_bytes", return_value=4 * 1024**3),
        ):
            await bot.housekeeping_job(self.context)
        self.assertEqual(len(self.notified), 1)
        self.assertIn("4 file", self.notified[0])

    async def test_still_low_with_nothing_deletable_stays_quiet(self) -> None:
        """The 6-hourly disk_watch_job owns "disk is low" alerts; this tick
        must not add a second voice about the same disk."""
        nothing = _report(deleted=0, applied=True)
        for _ in range(3):
            with (
                mock.patch.object(bot.heartbeat, "beat"),
                mock.patch.object(bot.heartbeat, "ping"),
                mock.patch.object(bot.cleanup, "under_pressure", return_value=True),
                mock.patch.object(bot.cleanup, "run", return_value=nothing),
            ):
                await bot.housekeeping_job(self.context)
        self.assertEqual(self.notified, [])

    async def test_skips_cleanup_while_a_run_is_in_flight(self) -> None:
        async with bot._PIPELINE_LOCK:
            with (
                mock.patch.object(bot.heartbeat, "beat") as beat,
                mock.patch.object(bot.heartbeat, "ping"),
                mock.patch.object(bot.cleanup, "under_pressure", return_value=True),
                mock.patch.object(bot.cleanup, "run") as run,
            ):
                await bot.housekeeping_job(self.context)
        beat.assert_called_once()
        run.assert_not_called()


class CleanupCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_text_and_buttons(self) -> None:
        report = _report(
            items=[
                cleanup.Item("a", Path("a.mp4"), 1000, "finished"),
                cleanup.Item("b", Path("b.mp4"), 2000, "abandoned"),
            ],
            soon_videos=2,
            soon_bytes=4096,
            held_back_videos=1,
            held_back_bytes=2048,
            orphan_files=3,
            orphan_bytes=9000,
        )
        cfg = SimpleNamespace(
            telegram_allowed_user_ids=(1,), cleanup_retention_days=3
        )
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
        with (
            mock.patch.object(bot, "config", cfg),
            mock.patch.object(bot.cleanup, "assess", return_value=report),
            mock.patch.object(bot.cleanup, "disk_free_bytes", return_value=3 * 1024**3),
        ):
            await bot.handle_cleanup(update, SimpleNamespace(args=[]))
        text = message.reply_text.await_args.args[0]
        self.assertIn("3.0 GB", text)
        self.assertIn("2 file", text)
        self.assertIn("gak pernah dikonfirmasi", text)
        self.assertIn("belum selesai", text)
        self.assertIn("gak tercatat", text)
        data = [
            b.callback_data
            for row in message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertEqual(data, ["cleanup:apply", "cleanup:force"])

    async def test_apply_argument_deletes_right_away(self) -> None:
        """`/cleanup apply` — the contract that was already deployed."""
        done = _report(deleted=3, freed_bytes=2 * 1024**3, applied=True)
        cfg = SimpleNamespace(telegram_allowed_user_ids=(1,))
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
        with (
            mock.patch.object(bot, "config", cfg),
            mock.patch.object(bot.cleanup, "run", return_value=done) as run,
            mock.patch.object(bot.cleanup, "disk_free_bytes", return_value=5 * 1024**3),
        ):
            await bot.handle_cleanup(update, SimpleNamespace(args=["apply"]))
        run.assert_called_once_with(apply_changes=True)
        text = message.reply_text.await_args.args[0]
        self.assertIn("3 file dihapus", text)
        self.assertIn("2.0 GB", text)

    async def test_apply_argument_waits_while_a_run_is_in_flight(self) -> None:
        cfg = SimpleNamespace(telegram_allowed_user_ids=(1,))
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
        with mock.patch.object(bot, "config", cfg), mock.patch.object(
            bot.cleanup, "run"
        ) as run:
            async with bot._PIPELINE_LOCK:
                await bot.handle_cleanup(update, SimpleNamespace(args=["apply"]))
        run.assert_not_called()
        self.assertIn("proses posting", message.reply_text.await_args.args[0])

    async def test_apply_is_refused_while_a_run_is_in_flight(self) -> None:
        cfg = SimpleNamespace(telegram_allowed_user_ids=(1,))
        query = SimpleNamespace(
            data="cleanup:apply",
            message=SimpleNamespace(),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=1)
        )
        with mock.patch.object(bot, "config", cfg), mock.patch.object(
            bot.cleanup, "run"
        ) as run:
            async with bot._PIPELINE_LOCK:
                await bot.handle_cleanup_callback(update, SimpleNamespace())
        run.assert_not_called()
        self.assertIn("proses posting", query.edit_message_text.await_args.args[0])


class MissingFileGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.video = root / "incoming" / "gone.mp4"  # never created
        self.state_file = root / "posts.json"
        self.key = bot.pipeline_state.video_key(self.video)
        self.state_file.write_text(
            json.dumps(
                {
                    self.key: {
                        "video_path": str(self.video),
                        "caption": "c",
                        "gemini": {"file_name": "gone.mp4"},
                        "platforms": {"youtube": {"status": "error"}},
                        "selected_platforms": ["youtube"],
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = SimpleNamespace(
            state_file=self.state_file,
            telegram_allowed_user_ids=(1,),
            telegram_platforms=(),
            platforms=("youtube",),
            abandoned_retention_days=7,
        )
        p = mock.patch.object(bot, "config", cfg)
        p.start()
        self.addCleanup(p.stop)

    def _update(self, data: str):
        message = SimpleNamespace(
            chat_id=1, reply_text=AsyncMock(), reply_markup=None
        )
        query = SimpleNamespace(
            data=data,
            message=message,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        return (
            SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1)),
            query,
            message,
        )

    async def test_post_on_a_cleaned_up_video_explains_instead_of_crashing(self) -> None:
        update, query, _ = self._update(f"post:{self.key}")
        context = SimpleNamespace(application=SimpleNamespace(job_queue=None))
        with mock.patch.object(bot, "_execute_post", new=AsyncMock()) as execute:
            await bot.handle_confirm_callback(update, context)
        execute.assert_not_awaited()
        shown = query.edit_message_text.await_args.args[0]
        self.assertIn("sudah gak ada", shown)
        self.assertIn("7 hari", shown)

    async def test_retry_on_a_missing_file_explains_too(self) -> None:
        update, query, message = self._update(f"retry:{self.key}:youtube")
        context = SimpleNamespace(application=SimpleNamespace(job_queue=None))
        with mock.patch.object(bot, "_run_pipeline", new=AsyncMock()) as run:
            await bot.handle_confirm_callback(update, context)
        run.assert_not_awaited()
        self.assertIn("sudah gak ada", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
