"""Posting-copy pre-render, encoder flags and killing a run's whole process group."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import main as pipeline_main
import telegram_bot as bot
from core import watermark


class KillRunTests(unittest.TestCase):
    def test_finished_process_is_left_alone(self) -> None:
        proc = mock.Mock(returncode=0, pid=123)
        with mock.patch.object(bot.os, "killpg", create=True) as killpg:
            bot._kill_run(proc)
        killpg.assert_not_called()
        proc.kill.assert_not_called()

    def test_posix_kills_the_whole_group(self) -> None:
        proc = mock.Mock(returncode=None, pid=123)
        with mock.patch.object(bot.os, "killpg", create=True) as killpg:
            bot._kill_run(proc)
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 123)
        proc.kill.assert_not_called()

    def test_falls_back_to_plain_kill(self) -> None:
        proc = mock.Mock(returncode=None, pid=123)
        with mock.patch.object(bot.os, "killpg", create=True, side_effect=PermissionError):
            bot._kill_run(proc)
        proc.kill.assert_called_once()


class PrerenderTests(unittest.TestCase):
    def _run(self, *flags: str, render_error: Exception | None = None) -> mock.Mock:
        gem = SimpleNamespace(caption="c", file_name="f", folder="d")
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            video.write_bytes(b"x")
            with (
                mock.patch.object(sys, "argv", ["main.py", str(video), "--dry-run", *flags]),
                mock.patch.object(pipeline_main, "setup_logging"),
                mock.patch.object(pipeline_main.video_facts, "extract_facts"),
                mock.patch.object(pipeline_main, "step_geocode", return_value=(None, None)),
                mock.patch.object(pipeline_main, "step_gemini", return_value=gem),
                mock.patch.object(pipeline_main, "step_gdrive", return_value="url"),
                mock.patch.object(pipeline_main.log, "warning"),
                mock.patch.object(
                    pipeline_main, "step_watermark", side_effect=render_error
                ) as render,
            ):
                self.assertEqual(pipeline_main.main(), 0)
        return render

    def test_dry_run_alone_does_not_render(self) -> None:
        self._run().assert_not_called()

    def test_prerender_renders_fresh(self) -> None:
        render = self._run("--prerender")
        render.assert_called_once()
        self.assertIs(render.call_args.kwargs["skip"], False)

    def test_failed_prerender_does_not_fail_the_dry_run(self) -> None:
        self._run("--prerender", render_error=RuntimeError("ffmpeg")).assert_called_once()


class PostingCopyFlagsTests(unittest.TestCase):
    def test_8bit_and_faststart(self) -> None:
        done = SimpleNamespace(returncode=0, stderr="")
        with (
            mock.patch.object(watermark, "_ensure_logo", return_value=Path("logo.png")),
            mock.patch.object(watermark, "_posting_size", return_value=(1080, 1920)),
            mock.patch.object(watermark.subprocess, "run", return_value=done) as run,
            tempfile.TemporaryDirectory() as tmp,
        ):
            watermark.make_posting_copy(Path("in.mp4"), output_path=Path(tmp) / "o.mp4")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "+faststart")


if __name__ == "__main__":
    unittest.main()
