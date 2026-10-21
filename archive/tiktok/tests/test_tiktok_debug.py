"""A TikTok failure screenshot's trip: worker -> HG680 -> parked -> chat."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import main as pipeline_main
import telegram_bot as bot
import worker_service
from core import debug_artifacts
from distributors import tiktok_remote

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 8


class RemoteErrorCarriesScreenshot(unittest.TestCase):
    def _post(self, payload: dict):
        cfg = SimpleNamespace(
            tiktok_worker_host="100.0.0.1",
            tiktok_worker_port=8790,
            tiktok_worker_shared_secret="s",
            tiktok_remote_timeout_s=5,
        )
        response = MagicMock()
        response.json.return_value = payload
        with (
            mock.patch.object(tiktok_remote, "config", cfg),
            mock.patch.object(tiktok_remote.requests, "post", return_value=response),
        ):
            return tiktok_remote.post(Path("x_post.mp4"), "caption", post_url="https://u")

    def test_error_with_screenshot(self) -> None:
        with self.assertRaises(tiktok_remote.TikTokRemoteError) as ctx:
            self._post(
                {
                    "status": "error",
                    "error": "RuntimeError: Caption box has 5 chars",
                    "screenshot_b64": base64.b64encode(PNG).decode(),
                }
            )
        self.assertEqual(ctx.exception.screenshot_png, PNG)
        self.assertIn("Caption box has 5 chars", str(ctx.exception))

    def test_garbage_screenshot_never_hides_the_real_error(self) -> None:
        with self.assertRaises(tiktok_remote.TikTokRemoteError) as ctx:
            self._post({"status": "error", "error": "boom", "screenshot_b64": "!!!not-b64"})
        self.assertIsNone(ctx.exception.screenshot_png)
        self.assertIn("boom", str(ctx.exception))

    def test_old_worker_without_screenshot_still_works(self) -> None:
        with self.assertRaises(tiktok_remote.TikTokRemoteError) as ctx:
            self._post({"status": "error", "error": "old worker"})
        self.assertIsNone(ctx.exception.screenshot_png)


class WorkerSideTests(unittest.TestCase):
    def test_screenshot_b64_round_trip_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "a.png"
            small.write_bytes(PNG)
            self.assertEqual(
                base64.b64decode(worker_service._screenshot_b64(small)), PNG
            )
            self.assertIsNone(worker_service._screenshot_b64(Path(tmp) / "missing.png"))
            self.assertIsNone(worker_service._screenshot_b64(None))
            big = Path(tmp) / "big.png"
            big.write_bytes(b"x" * (7 * 1024 * 1024))
            self.assertIsNone(worker_service._screenshot_b64(big))


class ParkAndSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        cfg = SimpleNamespace(state_file=Path(self._tmp.name) / "state" / "posts.json")
        for target in (debug_artifacts, pipeline_main):
            p = mock.patch.object(target, "config", cfg)
            p.start()
            self.addCleanup(p.stop)
        self.video = Path(self._tmp.name) / "incoming" / "v.mp4"
        self.video.parent.mkdir()
        self.video.write_bytes(b"x")
        self.key = pipeline_main.state.video_key(self.video)

    def test_remote_failure_is_parked_under_the_video_key(self) -> None:
        exc = tiktok_remote.TikTokRemoteError("boom", screenshot_png=PNG)
        pipeline_main._park_failure_screenshot(self.video, "tiktok", exc)
        parked = debug_artifacts.take(self.key, "tiktok")
        self.assertIsNotNone(parked)
        self.assertEqual(parked.read_bytes(), PNG)

    def test_local_failure_screenshot_path_is_copied(self) -> None:
        src = Path(self._tmp.name) / "shot.png"
        src.write_bytes(PNG)
        exc = RuntimeError("boom")
        exc.screenshot_path = src  # what distributors.tiktok.TikTokPostError carries
        pipeline_main._park_failure_screenshot(self.video, "tiktok", exc)
        self.assertEqual(debug_artifacts.take(self.key, "tiktok").read_bytes(), PNG)

    def test_exception_without_screenshot_parks_nothing(self) -> None:
        pipeline_main._park_failure_screenshot(self.video, "tiktok", RuntimeError("x"))
        self.assertIsNone(debug_artifacts.take(self.key, "tiktok"))

    def test_oversized_screenshot_is_dropped(self) -> None:
        exc = tiktok_remote.TikTokRemoteError(
            "boom", screenshot_png=b"x" * (debug_artifacts.MAX_BYTES + 1)
        )
        pipeline_main._park_failure_screenshot(self.video, "tiktok", exc)
        self.assertIsNone(debug_artifacts.take(self.key, "tiktok"))

    async def test_bot_sends_the_photo_then_deletes_it(self) -> None:
        debug_artifacts.save_bytes(self.key, "tiktok", PNG)
        message = SimpleNamespace(reply_photo=AsyncMock(), reply_document=AsyncMock())
        await bot._send_tiktok_debug(message, self.key)
        message.reply_photo.assert_awaited_once()
        self.assertIsNone(debug_artifacts.take(self.key, "tiktok"))

    async def test_bot_falls_back_to_document_when_photo_is_rejected(self) -> None:
        debug_artifacts.save_bytes(self.key, "tiktok", PNG)
        message = SimpleNamespace(
            reply_photo=AsyncMock(side_effect=bot.telegram_error.BadRequest("too tall")),
            reply_document=AsyncMock(),
        )
        await bot._send_tiktok_debug(message, self.key)
        message.reply_document.assert_awaited_once()
        self.assertIsNone(debug_artifacts.take(self.key, "tiktok"))

    async def test_bot_does_nothing_without_a_screenshot(self) -> None:
        message = SimpleNamespace(reply_photo=AsyncMock(), reply_document=AsyncMock())
        await bot._send_tiktok_debug(message, self.key)
        message.reply_photo.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
