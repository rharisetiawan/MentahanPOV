"""Doctor Idol fancam tooling: catalogue checks, highlight picking, ffmpeg graph, cover, captions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from doctor_idol import catalog as cat
from doctor_idol.captions import (
    FANCAM_NOTE,
    TAKEDOWN_NOTE,
    build_captions,
    build_youtube_text,
    youtube_title,
)
from doctor_idol.cover import compose, cover_time, series_tag
from doctor_idol.highlights import best_windows, parse_ebur128
from doctor_idol.render import (
    Card,
    build_command,
    build_filter,
    card_rows,
    is_vertical,
    plan_cards,
)

from doctor_idol.youtube import build_command as youtube_command
from doctor_idol.youtube import long_cut

BASE = """
[settings]
transition_s = 0.3

[[finalist]]
slug = "kartika-widyasari"
episode = 1
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
        self.assertAlmostEqual(f.reel_length(0.3), 35.5)
        self.assertEqual(f.episode, 1)
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
            "episode": '[[finalist]]\nslug = "a"\nepisode = 0\n',
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
        cards = plan_cards(self.f, self.settings)
        graph = build_filter(self.f, self.settings, vertical=True, cards=cards)
        self.assertIn("xfade=transition=fade:duration=0.3:offset=24.500[base]", graph)
        self.assertIn("[a0][a1]acrossfade=d=0.3[amain]", graph)
        self.assertIn("[3:v]format=rgba,fade", graph)  # first card after 2 segments + logo
        self.assertIn("loudnorm=I=-14", graph)

    def test_signature_cards_in_order(self) -> None:
        cards = plan_cards(self.f, self.settings)
        self.assertEqual([c.name for c in cards], ["hook", "name", "score"])
        self.assertEqual((cards[1].start, cards[1].dur), (2.5, 4.0))
        self.assertAlmostEqual(cards[2].start, self.f.reel_length(0.3) - 4.0)
        self.assertEqual(cards[0].rows[0], ("eye", "POV: kamu di kursi penonton"))
        graph = build_filter(self.f, self.settings, vertical=True, cards=cards)
        self.assertIn("setpts=PTS+2.500/TB[c1]", graph)
        self.assertIn("overlay=x=(W-w)/2:y=round(H*0.10)", graph)  # hook centred
        self.assertIn("overlay=x=-w+w*t/35.500", graph)  # progress bar over the whole reel

    def test_score_card_dropped_when_clip_is_short(self) -> None:
        short = cat.parse_catalog('[[finalist]]\nslug = "a"\nname = "X"\nsegments = [[0, 10]]\n')
        self.assertEqual([c.name for c in plan_cards(short.get("a"), short.settings)], ["hook", "name"])

    def test_signature_can_be_switched_off(self) -> None:
        plain = cat.parse_catalog(
            '[settings]\nhook_text = ""\nname_card = false\nscore_text = ""\nprogress_bar = false\n'
            '[[finalist]]\nslug = "a"\nsegments = [[10, 40]]\n'
        )
        one = plain.get("a")
        self.assertEqual(plan_cards(one, plain.settings), [])
        graph = build_filter(one, plain.settings, vertical=False)
        self.assertIn("[v0]null[base]", graph)
        self.assertIn("boxblur=40:4", graph)
        self.assertIn("[marked]null[vout]", graph)

    def test_command_seeks_each_segment(self) -> None:
        card = Card((("eye", "x"),), 0.0, 2.5)
        cmd = build_command(
            self.f, self.settings, source=Path("in.mp4"), output=Path("out.mp4"),
            vertical=True, cards=[(card, Path("hook.png"))],
        )
        self.assertIn("hook.png", cmd)
        self.assertEqual(cmd.count("in.mp4"), 2)
        self.assertIn("198", cmd)
        self.assertEqual(cmd[-1], "out.mp4")

    def test_vertical_detection_and_card(self) -> None:
        self.assertTrue(is_vertical(480, 854))
        self.assertFalse(is_vertical(1920, 1080))
        self.assertEqual(card_rows(self.f)[1], ("speaker", "When I Look at You · Miley Cyrus"))


class CoverTests(unittest.TestCase):
    def test_series_tag_and_time(self) -> None:
        catalog = cat.parse_catalog(BASE)
        f = catalog.get("kartika-widyasari")
        self.assertEqual(series_tag(f, catalog.settings), "POV KURSI PENONTON #1")
        t = cover_time(f, catalog.settings)
        self.assertTrue(6.5 < t < f.reel_length(0.3) - 4.0)

    def test_compose_writes_a_vertical_jpeg(self) -> None:
        from PIL import Image

        catalog = cat.parse_catalog(BASE)
        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "frame.png"
            Image.new("RGB", (1080, 1920), (40, 120, 90)).save(frame)
            out = compose(frame, catalog.get("kartika-widyasari"), catalog.settings, Path(tmp) / "cover.jpg")
            with Image.open(out) as img:
                self.assertEqual(img.size, (1080, 1920))

    def test_long_name_card_shrinks_to_fit(self) -> None:
        from PIL import Image

        from core.watermark import render_overlay_card

        row = [("trophy", "Juara 3 · dr. Abdurachman Omar Baabdullah, Sp.PD")]
        with tempfile.TemporaryDirectory() as tmp:
            wide = render_overlay_card(row, video_size=(1080, 1920), path=Path(tmp) / "a.png")
            fit = render_overlay_card(
                row, video_size=(1080, 1920), path=Path(tmp) / "b.png", max_width=1002
            )
            with Image.open(wide) as a, Image.open(fit) as b:
                self.assertGreater(a.width, 1002)
                self.assertLessEqual(b.width, 1002)

    def test_new_icons_render(self) -> None:
        from core.watermark import render_overlay_card

        with tempfile.TemporaryDirectory() as tmp:
            png = render_overlay_card(
                [("eye", "POV"), ("chat", "Skor?")], video_size=(1080, 1920), path=Path(tmp) / "c.png"
            )
            self.assertTrue(png.exists())


ANNOUNCEMENT = """
[[finalist]]
slug = "pengumuman-juara"
kind = "announcement"
name = "Pengumuman Juara"
hook = "POV: kamu di kursi penonton saat juara diumumkan"
card = ["Juara 1 · dr. A", "Juara 2 · dr. B", "Juara 3 · dr. C", "Most Loved Doctor · dr. D"]
segments = [[90, 122], [159, 170]]
"""


class AnnouncementTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = cat.parse_catalog(ANNOUNCEMENT)
        self.f = catalog.get("pengumuman-juara")
        self.settings = catalog.settings

    def test_own_hook_trophy_card_and_verdict_prompt(self) -> None:
        cards = plan_cards(self.f, self.settings)
        self.assertEqual(cards[0].rows[0][1], "POV: kamu di kursi penonton saat juara diumumkan")
        self.assertEqual(cards[1].rows[0], ("trophy", "Juara 1 · dr. A"))
        self.assertEqual(cards[1].dur, 4.0 + 1.5 * 2)  # four winners need longer on screen
        self.assertEqual(cards[2].rows[0], ("chat", "Setuju sama juri?"))

    def test_missing_and_bad_kind(self) -> None:
        self.assertEqual(self.f.missing(), [])
        with self.assertRaises(cat.CatalogError):
            cat.parse_catalog('[[finalist]]\nslug = "a"\nkind = "final"\n')

    def test_captions_list_winners(self) -> None:
        text = build_captions(self.f, self.settings)
        self.assertIn("🏆 Juara 2 · dr. B", text)
        self.assertEqual(text.count(FANCAM_NOTE), 3)
        self.assertEqual(text.count(TAKEDOWN_NOTE), 3)
        self.assertNotIn("[nama finalis]", text)


class YoutubeTests(unittest.TestCase):
    def test_long_cut_defaults_to_whole_recording_and_clamps(self) -> None:
        whole = cat.parse_catalog('[[finalist]]\nslug = "a"\n').get("a")
        self.assertEqual(long_cut(whole, 200.0), cat.Segment(0.0, 200.0))
        trimmed = cat.parse_catalog('[[finalist]]\nslug = "a"\nfull = [30, 500]\n').get("a")
        self.assertEqual(long_cut(trimmed, 200.0), cat.Segment(30.0, 200.0))

    def test_command_is_landscape_with_one_seek(self) -> None:
        settings = cat.parse_catalog(BASE).settings
        cmd = youtube_command(settings, source=Path("in.mp4"), output=Path("out.mp4"), cut=cat.Segment(30.0, 200.0))
        self.assertEqual(cmd[cmd.index("-ss") + 1], "30")
        self.assertEqual(cmd[cmd.index("-t") + 1], "170")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("crop=1920:1080", graph)
        self.assertIn("loudnorm=I=-14", graph)


class CaptionTests(unittest.TestCase):
    def test_says_it_is_a_fancam(self) -> None:
        catalog = cat.parse_catalog(BASE)
        text = build_captions(catalog.get("kartika-widyasari"), catalog.settings)
        self.assertEqual(text.count(FANCAM_NOTE), 3)  # IG, Shorts, TikTok
        self.assertEqual(text.count(TAKEDOWN_NOTE), 3)
        self.assertIn("#DokterMalang", text)
        self.assertIn("POV Kursi Penonton #1", text)
        self.assertIn("#POVKursiPenonton", text)
        self.assertIn("[akun IG finalis]", text)

    def test_placeholders_for_missing_data(self) -> None:
        catalog = cat.parse_catalog('[[finalist]]\nslug = "a"\ninstagram = "@budi.dr"\n')
        text = build_captions(catalog.get("a"), catalog.settings)
        self.assertIn("[nama finalis]", text)
        self.assertIn("@budi.dr", text)
        self.assertNotIn("@@", text)

    def test_verified_finalist_has_no_disclaimer(self) -> None:
        catalog = cat.parse_catalog(BASE)
        text = build_captions(catalog.get("kartika-widyasari"), catalog.settings)
        self.assertNotIn("belum kami pastikan", text)
        self.assertIn('"When I Look at You" (Miley Cyrus)', text)

    def test_missing_city_and_song_read_clean_with_disclaimer(self) -> None:
        catalog = cat.parse_catalog(
            '[[finalist]]\nslug = "a"\nname = "dr. Budi"\ninstagram = "budi"\n'
        )
        text = build_captions(catalog.get("a"), catalog.settings)
        for leftover in ("[kota]", "[judul lagu]", "[penyanyi asli]", "membawakan", "dari ,"):
            self.assertNotIn(leftover, text)
        self.assertEqual(text.count("Kota dan judul lagu belum kami pastikan"), 3)
        self.assertEqual(text.count(FANCAM_NOTE), 3)
        self.assertIn("dokter ini naik panggung", text)

    def test_uncertain_song_and_extra_detail_in_disclaimer(self) -> None:
        catalog = cat.parse_catalog(
            '[[finalist]]\nslug = "a"\nname = "B"\ncity = "Malang"\nsong = "Lagu (?)"\n'
            'artist = "C"\nunconfirmed = ["gelar"]\n'
        )
        f = catalog.get("a")
        self.assertEqual(f.uncertain(), ["judul lagu", "gelar"])
        text = build_captions(f, catalog.settings)
        self.assertIn('"Lagu (?)" (C)', text)
        self.assertIn("Judul lagu dan gelar belum kami pastikan", text)

    def test_youtube_text_keeps_the_notes_and_the_title_limit(self) -> None:
        catalog = cat.parse_catalog(
            BASE + '\n[[finalist]]\nslug = "panjang"\nname = "dr. Nama Yang Sangat Panjang Sekali Sampai '
            'Melewati Batas Judul Youtube, Sp.PD"\nsong = "Judul Lagu Yang Juga Cukup Panjang"\n'
        )
        for f in catalog.finalists:
            self.assertLessEqual(len(youtube_title(f, catalog.settings)), 100)
            text = build_youtube_text(f, catalog.settings)
            self.assertIn(FANCAM_NOTE, text)
            self.assertIn(TAKEDOWN_NOTE, text)
            self.assertIn("Monetisasi: nonaktif", text)
        self.assertEqual(
            youtube_title(catalog.get("kartika-widyasari"), catalog.settings),
            "When I Look at You - dr. Kartika Widyasari | Doctor Idol Competition Season IV (Fancam)",
        )

    def test_unconfirmed_must_be_a_list(self) -> None:
        with self.assertRaises(cat.CatalogError):
            cat.parse_catalog('[[finalist]]\nslug = "a"\nunconfirmed = "gelar"\n')


if __name__ == "__main__":
    unittest.main()
