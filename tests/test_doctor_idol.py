"""Doctor Idol fancam tooling: catalogue checks, highlight picking, ffmpeg graph, captions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from doctor_idol import catalog as cat
from doctor_idol.captions import FANCAM_NOTE, build_captions
from doctor_idol.highlights import best_windows, parse_ebur128
from doctor_idol.render import build_command, build_filter, card_rows, is_vertical

BASE = """
[settings]
transition_s = 0.3

[[finalist]]
slug = "kartika-widyasari"
name = "dr. Kartika Widyasari"
city = "Malang"
song = "When I Look at You"
artist = "Miley Cyrus"
segments = [[198, 222.8], [268, 279]]
"""


class CatalogTests(unittest.TestCase):
    def test_real_catalogue_loads(self) -> None:
        catalog = cat.load_catalog()
        self.assertTrue(catalog.finalists)

    def test_parses_finalist(self) -> None:
        f = cat.parse_catalog(BASE).get("kartika-widyasari")
        self.assertEqual(f.label, "dr. Kartika Widyasari · Malang")
        self.assertAlmostEqual(f.clip_length, 35.8)
        self.assertEqual(f.missing(), [])

    def test_rejects_bad_input(self) -> None:
        bad = {
            "slug": '[[finalist]]\nslug = "Kartika Idol"\n',
            "duplicate": '[[finalist]]\nslug = "a"\n[[finalist]]\nslug = "a"\n',
            "segment": '[[finalist]]\nslug = "a"\nsegments = [[20, 10]]\n',
            "overlap": '[[finalist]]\nslug = "a"\nsegments = [[0, 10], [5, 12]]\n',
            "status": '[[finalist]]\nslug = "a"\nstatus = "done"\n',
            "column": '[[finalist]]\nslug = "a"\nnmae = "x"\n',
            "setting": '[settings]\nfoo = 1\n',
        }
        for case, text in bad.items():
            with self.subTest(case), self.assertRaises(cat.CatalogError):
                cat.parse_catalog(text)

    def test_missing_fields_listed(self) -> None:
        f = cat.parse_catalog('[[finalist]]\nslug = "baru"\n').get("baru")
        self.assertEqual(f.missing(), ["name", "city", "song", "artist", "segments"])

    def test_stub_is_valid_toml(self) -> None:
        catalog = cat.parse_catalog(BASE + cat.stub_entry("budi-santoso"))
        self.assertEqual(catalog.get("budi-santoso").status, "draft")

    def test_slugify(self) -> None:
        self.assertEqual(cat.slugify("Kartika Idol (1)"), "kartika-idol-1")

    def test_unlisted_videos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            for name in ("kartika-widyasari.mp4", "budi.MOV", "notes.txt"):
                (raw / name).touch()
            catalog = cat.parse_catalog(BASE)
            self.assertEqual([p.name for p in cat.unlisted_videos(catalog, Path(tmp))], ["budi.MOV"])
            self.assertEqual(cat.raw_video("kartika-widyasari", Path(tmp)).name, "kartika-widyasari.mp4")


class HighlightTests(unittest.TestCase):
    def test_parse_ametadata(self) -> None:
        text = (
            "frame:0    pts:0       pts_time:0\nlavfi.r128.S=-120.691\n"
            "frame:1    pts:4800    pts_time:0.5\nlavfi.r128.S=-inf\n"
            "frame:2    pts:48000   pts_time:1.0\nlavfi.r128.S=-12.0\n"
        )
        self.assertEqual(parse_ebur128(text), [-70.0, -12.0])

    def test_picks_loudest_non_overlapping(self) -> None:
        loud = [-30.0] * 120
        loud[60:70] = [-8.0] * 10
        loud[20:30] = [-10.0] * 10
        windows = best_windows(loud, length=10, count=2, skip_head=5, skip_tail=5)
        self.assertEqual([(w.start, w.end) for w in windows], [(60.0, 70.0), (20.0, 30.0)])

    def test_short_recording_still_answers(self) -> None:
        self.assertEqual(len(best_windows([-20.0] * 12, length=10, count=1)), 1)
        self.assertEqual(best_windows([], length=10), [])


class RenderGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = cat.parse_catalog(BASE)
        self.f = catalog.get("kartika-widyasari")
        self.settings = catalog.settings

    def test_crossfade_offsets(self) -> None:
        graph = build_filter(self.f, self.settings, vertical=True, has_card=True)
        self.assertIn("xfade=transition=fade:duration=0.3:offset=24.500[base]", graph)
        self.assertIn("[a0][a1]acrossfade=d=0.3[amain]", graph)
        self.assertIn("[3:v]format=rgba,fade", graph)  # card after 2 segments + logo
        self.assertIn("loudnorm=I=-14", graph)

    def test_single_segment_and_landscape(self) -> None:
        one = cat.parse_catalog('[[finalist]]\nslug = "a"\nsegments = [[10, 40]]\n').get("a")
        graph = build_filter(one, self.settings, vertical=False, has_card=False)
        self.assertIn("[v0]null[base]", graph)
        self.assertIn("boxblur=40:4", graph)
        self.assertIn("[marked]null[vout]", graph)

    def test_command_seeks_each_segment(self) -> None:
        cmd = build_command(
            self.f, self.settings, source=Path("in.mp4"), output=Path("out.mp4"),
            vertical=True, card_png=None,
        )
        self.assertEqual(cmd.count("in.mp4"), 2)
        self.assertIn("198", cmd)
        self.assertEqual(cmd[-1], "out.mp4")

    def test_vertical_detection_and_card(self) -> None:
        self.assertTrue(is_vertical(480, 854))
        self.assertFalse(is_vertical(1920, 1080))
        self.assertEqual(card_rows(self.f)[1], ("speaker", "When I Look at You · Miley Cyrus"))


class CaptionTests(unittest.TestCase):
    def test_says_it_is_a_fancam(self) -> None:
        catalog = cat.parse_catalog(BASE)
        text = build_captions(catalog.get("kartika-widyasari"), catalog.settings)
        self.assertEqual(text.count(FANCAM_NOTE), 3)  # IG, Shorts, TikTok
        self.assertIn("#DokterMalang", text)
        self.assertIn("[akun IG finalis]", text)

    def test_placeholders_for_missing_data(self) -> None:
        catalog = cat.parse_catalog('[[finalist]]\nslug = "a"\ninstagram = "@budi.dr"\n')
        text = build_captions(catalog.get("a"), catalog.settings)
        self.assertIn("[nama finalis]", text)
        self.assertIn("@budi.dr", text)
        self.assertNotIn("@@", text)


if __name__ == "__main__":
    unittest.main()
