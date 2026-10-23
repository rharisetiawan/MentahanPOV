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
            mock.patch.object(watermark, "_posting_size", return_value=(1080, 1920)),
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

    def test_no_label_keeps_the_old_graph(self) -> None:
        done = SimpleNamespace(returncode=0, stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(watermark, "_ensure_logo", return_value=Path("logo.png")),
            mock.patch.object(watermark, "_posting_size", return_value=(1080, 1920)),
            mock.patch.object(watermark.subprocess, "run", return_value=done) as run,
        ):
            watermark.make_posting_copy(Path("in.mp4"), output_path=Path(tmp) / "o.mp4")
        self.assertNotIn("-loop", run.call_args.args[0])


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
