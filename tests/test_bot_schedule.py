"""The 📅 Jadwalkan flow in telegram_bot.py, driven with fake Telegram objects.

Covers what's easy to get wrong and silent when it is: the schedule has to
land in state.json (so it survives a reboot), a job has to be registered,
cancelling has to undo both, and an overdue schedule found at startup has to
run once — not twice, not never.
"""

from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import telegram_bot as bot

KEY = "abc123def456"


class FakeJob:
    def __init__(self, name: str) -> None:
        self.name = name
        self.removed = False

    def schedule_removal(self) -> None:
        self.removed = True


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[FakeJob] = []
        self.calls: list[dict] = []

    def run_once(self, callback, when, name=None, data=None):
        job = FakeJob(name)
        self.jobs.append(job)
        self.calls.append({"callback": callback, "when": when, "name": name, "data": data})
        return job

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]


class ScheduleFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.video = root / "incoming" / "clip.mp4"
        self.video.parent.mkdir()
        self.video.write_bytes(b"x")
        self.state_file = root / "state" / "posts.json"
        self.state_file.parent.mkdir()
        # The key must be the real hash of the path: video_path_for_key looks
        # entries up by it.
        self.key = bot.pipeline_state.video_key(self.video)
        self._write_state(
            {
                self.key: {
                    "video_path": str(self.video),
                    "caption": "Hook line 🔊\n\nbody",
                    "gemini": {"file_name": "clip.mp4", "folder": "01"},
                    "platforms": {},
                    "selected_platforms": ["youtube", "facebook"],
                    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
            }
        )

        self.cfg = SimpleNamespace(
            state_file=self.state_file,
            telegram_allowed_user_ids=(1,),
            telegram_platforms=(),
            platforms=("youtube", "facebook"),
            schedule_slot_hours=(7, 12, 19),
            heartbeat_interval_min=5,
        )
        p = mock.patch.object(bot, "config", self.cfg)
        p.start()
        self.addCleanup(p.stop)
        bot._PENDING_SCHEDULE.clear()
        bot._PENDING_EDIT.clear()
        self.jq = FakeJobQueue()
        self.context = SimpleNamespace(
            application=SimpleNamespace(job_queue=self.jq),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

    # -- helpers --------------------------------------------------------

    def _write_state(self, data: dict) -> None:
        self.state_file.write_text(json.dumps(data), encoding="utf-8")

    def _entry(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))[self.key]

    def _callback(self, data: str):
        message = SimpleNamespace(chat_id=42, reply_text=AsyncMock())
        query = SimpleNamespace(
            data=data,
            message=message,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=1)
        )
        return update, query, message

    # -- tests ----------------------------------------------------------

    async def test_all_callback_data_fits_telegrams_64_byte_limit(self) -> None:
        now = bot._now_local()
        keyboard = bot._schedule_keyboard(self.key, now)
        for row in keyboard.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)

    async def test_picking_a_slot_persists_and_registers_a_job(self) -> None:
        when = (bot._now_local() + datetime.timedelta(hours=3)).replace(second=0, microsecond=0)
        update, query, _ = self._callback(f"sched:{self.key}:{int(when.timestamp())}")
        await bot.handle_schedule_callback(update, self.context)

        entry = self._entry()
        self.assertEqual(entry["schedule_chat_id"], 42)
        self.assertEqual(
            datetime.datetime.fromisoformat(entry["scheduled_at"]), when.astimezone(datetime.timezone.utc)
        )
        self.assertEqual(len(self.jq.calls), 1)
        self.assertEqual(self.jq.calls[0]["name"], f"sched:{self.key}")
        self.assertEqual(self.jq.calls[0]["data"], {"key": self.key})
        shown = query.edit_message_text.await_args.args[0]
        self.assertIn("Dijadwalkan", shown)

    async def test_a_time_in_the_past_is_refused(self) -> None:
        past = int((bot._now_local() - datetime.timedelta(minutes=5)).timestamp())
        update, query, _ = self._callback(f"sched:{self.key}:{past}")
        await bot.handle_schedule_callback(update, self.context)
        self.assertNotIn("scheduled_at", self._entry())
        self.assertEqual(self.jq.calls, [])

    async def test_cancel_clears_state_and_removes_the_job(self) -> None:
        when = bot._now_local() + datetime.timedelta(hours=2)
        update, _, _ = self._callback(f"sched:{self.key}:{int(when.timestamp())}")
        await bot.handle_schedule_callback(update, self.context)
        self.assertTrue(self._entry()["scheduled_at"])

        update, query, message = self._callback(f"schedx:{self.key}")
        with mock.patch.object(bot, "_send_confirmation", new=AsyncMock()) as confirm:
            await bot.handle_schedule_callback(update, self.context)
        self.assertIsNone(self._entry()["scheduled_at"])
        self.assertTrue(self.jq.jobs[0].removed)
        confirm.assert_awaited_once()  # the confirmation screen comes back

    async def test_typed_time_flow(self) -> None:
        update, _, _ = self._callback(f"sched:{self.key}:custom")
        await bot.handle_schedule_callback(update, self.context)
        self.assertEqual(bot._PENDING_SCHEDULE[42], self.key)

        reply = AsyncMock()
        text_update = SimpleNamespace(
            message=SimpleNamespace(text="nanti aja", reply_text=reply),
            effective_chat=SimpleNamespace(id=42),
            effective_user=SimpleNamespace(id=1),
        )
        await bot.handle_text_reply(text_update, self.context)
        self.assertIn(42, bot._PENDING_SCHEDULE, "a typo must keep waiting for a time")
        self.assertEqual(self.jq.calls, [])

        text_update.message.text = "+90m"
        await bot.handle_text_reply(text_update, self.context)
        self.assertNotIn(42, bot._PENDING_SCHEDULE)
        self.assertTrue(self._entry()["scheduled_at"])
        self.assertEqual(len(self.jq.calls), 1)

    async def test_scheduled_job_claims_the_entry_then_posts(self) -> None:
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        entry = self._entry()
        entry.update(scheduled_at=past.isoformat(), schedule_chat_id=42)
        self._write_state({self.key: entry})

        sent = SimpleNamespace(reply_text=AsyncMock())
        self.context.bot.send_message = AsyncMock(return_value=sent)
        self.context.job = SimpleNamespace(data={"key": self.key})
        with mock.patch.object(bot, "_execute_post", new=AsyncMock()) as execute:
            await bot._scheduled_post_job(self.context)

        execute.assert_awaited_once()
        args = execute.await_args.args
        self.assertEqual(args[1], self.key)
        self.assertEqual(args[3], {"youtube", "facebook"})
        # claimed *before* the slow part, so a restart can't re-fire it
        self.assertIsNone(self._entry()["scheduled_at"])

    async def test_cancelled_schedule_does_not_fire(self) -> None:
        self.context.job = SimpleNamespace(data={"key": self.key})
        with mock.patch.object(bot, "_execute_post", new=AsyncMock()) as execute:
            await bot._scheduled_post_job(self.context)  # no scheduled_at in state
        execute.assert_not_awaited()

    async def test_missing_video_reports_instead_of_posting(self) -> None:
        entry = self._entry()
        entry.update(
            scheduled_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            schedule_chat_id=42,
        )
        self._write_state({self.key: entry})
        self.video.unlink()
        self.context.job = SimpleNamespace(data={"key": self.key})
        with mock.patch.object(bot, "_execute_post", new=AsyncMock()) as execute:
            await bot._scheduled_post_job(self.context)
        execute.assert_not_awaited()
        self.context.bot.send_message.assert_awaited()
        self.assertIsNone(self._entry()["scheduled_at"])

    async def test_restore_registers_overdue_and_future_schedules(self) -> None:
        entry = self._entry()
        entry.update(
            scheduled_at=(
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)
            ).isoformat(),
            schedule_chat_id=42,
        )
        self._write_state({self.key: entry})
        app = SimpleNamespace(job_queue=self.jq)
        self.assertEqual(bot._restore_schedules(app), 1)
        run_at = self.jq.calls[0]["when"]
        # overdue -> runs shortly after startup, not in the past
        self.assertGreater(run_at, datetime.datetime.now(datetime.timezone.utc))


class ConfirmationKeyboardTests(unittest.TestCase):
    def test_schedule_button_is_offered(self) -> None:
        keyboard = bot._confirmation_keyboard(KEY, {"youtube"})
        data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"sched:{KEY}", data)
        self.assertIn(f"post:{KEY}", data)
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))


class CallbackPatternTests(unittest.TestCase):
    def test_new_prefixes_do_not_collide_with_existing_handler(self) -> None:
        import re

        old = re.compile(r"^(post|cancel|edit|retry|toggle|stop):")
        for data in (f"sched:{KEY}", f"schedx:{KEY}", "cleanup:apply"):
            self.assertIsNone(old.match(data), data)


class DiskGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_available_passes_without_touching_anything(self) -> None:
        cfg = SimpleNamespace(upload_min_free_mb=1024, auto_cleanup=True)
        message = SimpleNamespace(reply_text=AsyncMock())
        with (
            mock.patch.object(bot, "config", cfg),
            mock.patch.object(bot.cleanup, "disk_free_bytes", return_value=10 * 1024**3),
        ):
            self.assertTrue(await bot._ensure_disk_room(message, 200))
        message.reply_text.assert_not_awaited()

    async def test_no_room_is_refused_with_an_explanation(self) -> None:
        cfg = SimpleNamespace(upload_min_free_mb=1024, auto_cleanup=False)
        message = SimpleNamespace(reply_text=AsyncMock())
        with (
            mock.patch.object(bot, "config", cfg),
            mock.patch.object(bot.cleanup, "disk_free_bytes", return_value=100 * 1024**2),
        ):
            self.assertFalse(await bot._ensure_disk_room(message, 500))
        message.reply_text.assert_awaited_once()
        self.assertIn("Disk gak cukup", message.reply_text.await_args.args[0])

    async def test_cleanup_that_frees_enough_lets_the_upload_through(self) -> None:
        cfg = SimpleNamespace(upload_min_free_mb=100, auto_cleanup=True)
        message = SimpleNamespace(reply_text=AsyncMock())
        free = iter([50 * 1024**2, 5 * 1024**3, 5 * 1024**3])
        report = MagicMock(deleted=3, freed_bytes=2 * 1024**3)
        with (
            mock.patch.object(bot, "config", cfg),
            mock.patch.object(bot.cleanup, "disk_free_bytes", side_effect=lambda: next(free)),
            mock.patch.object(bot.cleanup, "run", return_value=report),
        ):
            self.assertTrue(await bot._ensure_disk_room(message, 500))


if __name__ == "__main__":
    unittest.main()
