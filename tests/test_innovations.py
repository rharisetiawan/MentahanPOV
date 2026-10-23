"""Per-platform captions and the on-video location label."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import main as pipeline_main
import telegram_bot as bot
from core import gemini, geocode, watermark
from core.video_facts import StreamInfo

STREAM = StreamInfo(width=1080, height=1920, duration=20.0, fps=30.0, has_audio=True)


class ShortLabelTests(unittest.TestCase):
    def test_street_and_city(self) -> None:
        label = geocode.short_label(
            {"road": "Jalan Basuki Rahmat", "suburb": "Klojen", "city": "Kota Malang"}
        )
        self.assertEqual(label, "Jl. Basuki Rahmat · Malang")

    def test_long_street_falls_back_to_the_area(self) -> None:
        label = geocode.short_label(
            {
                "road": "Jalan Raya Mayjen Sungkono Kedungkandang Lama",
                "suburb": "Buring",
                "city": "Kota Malang",
            }
        )
        self.assertEqual(label, "Buring · Malang")

    def test_city_only_and_nothing(self) -> None:
        self.assertEqual(geocode.short_label({"county": "Kabupaten Malang"}), "Malang")
        self.assertIsNone(geocode.short_label({}))


class LocationLabelStepTests(unittest.TestCase):
    def _facts(self, gps):
        return SimpleNamespace(gps=gps)

    def test_no_gps_or_switched_off_means_no_label(self) -> None:
        cfg = SimpleNamespace(location_label=True)
        with mock.patch.object(pipeline_main, "config", cfg):
            self.assertIsNone(pipeline_main.location_label(self._facts(None)))
        cfg = SimpleNamespace(location_label=False)
        with mock.patch.object(pipeline_main, "config", cfg):
            self.assertIsNone(pipeline_main.location_label(self._facts((1.0, 2.0))))

    def test_lookup_failure_is_not_fatal(self) -> None:
        cfg = SimpleNamespace(location_label=True)
        with (
            mock.patch.object(pipeline_main, "config", cfg),
            mock.patch.object(
                pipeline_main.geocode,
                "reverse_geocode_details",
                side_effect=RuntimeError("nominatim down"),
            ),
            mock.patch.object(pipeline_main.log, "warning"),
        ):
            self.assertIsNone(pipeline_main.location_label(self._facts((1.0, 2.0))))


class LabelRenderTests(unittest.TestCase):
    def test_label_png_is_sized_for_the_video(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = watermark.render_location_label(
                "Klojen · Malang", video_size=(1080, 1920), path=Path(tmp) / "l.png"
            )
            with Image.open(path) as img:
                self.assertEqual(img.mode, "RGBA")
                self.assertLess(img.width, 1080 * 0.6)
                self.assertGreater(img.height, 30)

    def test_label_adds_a_timed_overlay_and_cleans_up(self) -> None:
        done = SimpleNamespace(returncode=0, stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(watermark, "_ensure_logo", return_value=Path("logo.png")),
            mock.patch("core.video_facts.probe_stream_info", return_value=STREAM),
            mock.patch.object(watermark.subprocess, "run", return_value=done) as run,
        ):
            out = Path(tmp) / "o.mp4"
            watermark.make_posting_copy(Path("in.mp4"), output_path=out, label="Malang")
            self.assertFalse(out.with_suffix(".label.png").exists())
        cmd = run.call_args.args[0]
        self.assertIn("-loop", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("fade=t=out", graph)
        self.assertIn("eof_action=pass", graph)

    def test_silent_clip_without_label_gets_no_card(self) -> None:
        # "Sound on" makes no sense without sound, and there's no place to show.
        silent = StreamInfo(width=1080, height=1920, duration=20.0, fps=30.0, has_audio=False)
        done = SimpleNamespace(returncode=0, stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(watermark, "_ensure_logo", return_value=Path("logo.png")),
            mock.patch("core.video_facts.probe_stream_info", return_value=silent),
            mock.patch.object(watermark.subprocess, "run", return_value=done) as run,
        ):
            watermark.make_posting_copy(Path("in.mp4"), output_path=Path(tmp) / "o.mp4")
        cmd = run.call_args.args[0]
        self.assertNotIn("-loop", cmd)
        self.assertNotIn("[aout]", cmd)  # no audio to map


class FirstTwoSecondsTests(unittest.TestCase):
    """Hook trim, seamless loop, loudness — the swipe-or-stay package."""

    def _cmd(self, stream: StreamInfo, **kwargs) -> list[str]:
        done = SimpleNamespace(returncode=0, stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(watermark, "_ensure_logo", return_value=Path("logo.png")),
            mock.patch("core.video_facts.probe_stream_info", return_value=stream),
            mock.patch.object(watermark.subprocess, "run", return_value=done) as run,
        ):
            watermark.make_posting_copy(
                Path("in.mp4"), output_path=Path(tmp) / "o.mp4", **kwargs
            )
        return run.call_args.args[0]

    def test_hook_start_is_only_used_early_in_the_clip(self) -> None:
        self.assertEqual(watermark.hook_start(3.0, 20.0), 3.0)
        self.assertEqual(watermark.hook_start(0.5, 20.0), 0.0)  # not worth it
        self.assertEqual(watermark.hook_start(12.0, 20.0), 0.0)  # would cut too much
        self.assertEqual(watermark.hook_start(None, 20.0), 0.0)
        self.assertEqual(watermark.hook_start(1.2, 4.0), 0.0)  # clip too short

    def test_hook_seeks_the_input(self) -> None:
        cmd = self._cmd(STREAM, start_s=3.0)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "3")
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertNotIn("-ss", self._cmd(STREAM))

    def test_loop_and_loudnorm_in_the_graph(self) -> None:
        cmd = self._cmd(STREAM)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("xfade=transition=fade", graph)
        self.assertIn("acrossfade", graph)
        self.assertIn("loudnorm=I=-14", graph)
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")

    def test_very_short_clip_is_not_looped(self) -> None:
        short = StreamInfo(width=1080, height=1920, duration=4.0, fps=30.0, has_audio=True)
        graph = self._cmd(short)[self._cmd(short).index("-filter_complex") + 1]
        self.assertNotIn("xfade", graph)

    def test_card_has_place_and_sound_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            watermark, "render_overlay_card", wraps=watermark.render_overlay_card
        ) as card:
            done = SimpleNamespace(returncode=0, stderr="")
            with (
                mock.patch.object(watermark, "_ensure_logo", return_value=Path("logo.png")),
                mock.patch("core.video_facts.probe_stream_info", return_value=STREAM),
                mock.patch.object(watermark.subprocess, "run", return_value=done),
            ):
                watermark.make_posting_copy(
                    Path("in.mp4"), output_path=Path(tmp) / "o.mp4", label="Malang"
                )
        rows = card.call_args.args[0]
        self.assertEqual([icon for icon, _ in rows], ["pin", "speaker"])


class RotationTests(unittest.TestCase):
    def test_portrait_phone_clip_tagged_as_rotated_landscape(self) -> None:
        from core import video_facts

        probe = {
            "format": {"duration": "12.5"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "r_frame_rate": "30000/1001",
                    "side_data_list": [{"rotation": -90}],
                },
                {"codec_type": "audio"},
            ],
        }
        with mock.patch.object(video_facts, "_ffprobe_json", return_value=probe):
            info = video_facts.probe_stream_info(Path("v.mp4"))
        self.assertEqual((info.width, info.height), (1080, 1920))
        self.assertAlmostEqual(info.fps, 29.97, places=2)
        self.assertEqual(info.duration, 12.5)
        self.assertTrue(info.has_audio)


class HookStartParseTests(unittest.TestCase):
    def test_only_sane_numbers(self) -> None:
        self.assertEqual(gemini._hook_start({"hook_start_s": 4}), 4.0)
        self.assertIsNone(gemini._hook_start({"hook_start_s": -1}))
        self.assertIsNone(gemini._hook_start({"hook_start_s": "4"}))
        self.assertIsNone(gemini._hook_start({"hook_start_s": True}))
        self.assertIsNone(gemini._hook_start({}))


class CaptionVariantTests(unittest.TestCase):
    def test_caption_for_each_platform(self) -> None:
        variants = {"youtube_title": "Rain in Malang | raw POV", "threads_text": "Short?"}
        cf = pipeline_main.caption_for
        self.assertEqual(cf("youtube", "Hook\nBody", variants), "Rain in Malang | raw POV\nHook\nBody")
        self.assertEqual(cf("threads", "Hook\nBody", variants), "Short?")
        self.assertEqual(cf("instagram", "Hook\nBody", variants), "Hook\nBody")
        self.assertEqual(cf("youtube", "Hook\nBody", {}), "Hook\nBody")

    def test_variants_are_optional_and_trimmed(self) -> None:
        self.assertEqual(gemini._variants({}), {})
        out = gemini._variants(
            {"youtube_title": "  Two\nlines  ", "threads_text": "x" * 600, "other": "y"}
        )
        self.assertEqual(out["youtube_title"], "Two lines")
        self.assertEqual(len(out["threads_text"]), 500)
        self.assertNotIn("other", out)

    def test_confirmation_shows_variants(self) -> None:
        text = bot._variants_text(
            {"caption_variants": {"youtube_title": "T", "threads_text": "TH"}}
        )
        self.assertIn("Judul YouTube: T", text)
        self.assertIn("Versi Threads:\nTH", text)
        self.assertEqual(bot._variants_text({}), "")


if __name__ == "__main__":
    unittest.main()
