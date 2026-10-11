"""core/schedule.py — parsing what a person types after "✍️ Ketik jam"."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core import schedule

WIB = ZoneInfo("Asia/Jakarta")
# Sunday 2026-10-11 14:30 WIB
NOW = datetime(2026, 10, 11, 14, 30, tzinfo=WIB)


class ParseWhenTests(unittest.TestCase):
    def test_clock_later_today(self) -> None:
        self.assertEqual(
            schedule.parse_when("19:30", NOW), NOW.replace(hour=19, minute=30)
        )

    def test_dot_separator(self) -> None:
        self.assertEqual(
            schedule.parse_when("19.30", NOW), NOW.replace(hour=19, minute=30)
        )

    def test_clock_already_passed_means_tomorrow(self) -> None:
        self.assertEqual(
            schedule.parse_when("07:00", NOW),
            (NOW + timedelta(days=1)).replace(hour=7, minute=0),
        )

    def test_besok_and_lusa(self) -> None:
        self.assertEqual(
            schedule.parse_when("besok 07:00", NOW),
            (NOW + timedelta(days=1)).replace(hour=7, minute=0),
        )
        self.assertEqual(
            schedule.parse_when("Lusa 18:00", NOW),
            (NOW + timedelta(days=2)).replace(hour=18, minute=0),
        )

    def test_relative(self) -> None:
        self.assertEqual(
            schedule.parse_when("+90m", NOW), (NOW + timedelta(minutes=90)).replace(second=0)
        )
        self.assertEqual(
            schedule.parse_when("+2j", NOW), (NOW + timedelta(hours=2)).replace(second=0)
        )
        self.assertEqual(
            schedule.parse_when("+1 jam", NOW), (NOW + timedelta(hours=1)).replace(second=0)
        )

    def test_garbage_and_out_of_range(self) -> None:
        for bad in ("", "nanti aja", "25:00", "12:61", "besok", "+0m", "-5m"):
            self.assertIsNone(schedule.parse_when(bad, NOW), bad)

    def test_today_explicit_but_passed_is_rejected_not_rolled_over(self) -> None:
        # "hari ini 08:00" at 14:30 is a typo, not "tomorrow".
        self.assertIsNone(schedule.parse_when("hari ini 08:00", NOW))

    def test_too_close_is_rejected(self) -> None:
        self.assertIsNone(schedule.parse_when("14:31", NOW))


class SlotTests(unittest.TestCase):
    def test_next_slots_roll_over_midnight(self) -> None:
        slots = schedule.upcoming_slots(NOW, [7, 12, 19], 3)
        self.assertEqual(
            slots,
            [
                NOW.replace(hour=19, minute=0),
                (NOW + timedelta(days=1)).replace(hour=7, minute=0),
                (NOW + timedelta(days=1)).replace(hour=12, minute=0),
            ],
        )

    def test_slot_too_close_is_skipped(self) -> None:
        near = NOW.replace(hour=18, minute=59)
        slots = schedule.upcoming_slots(near, [19], 1)
        self.assertEqual(slots, [(near + timedelta(days=1)).replace(hour=19, minute=0)])


class FormatTests(unittest.TestCase):
    def test_today_tomorrow_and_later(self) -> None:
        self.assertEqual(
            schedule.format_when(NOW.replace(hour=19, minute=0), NOW), "hari ini 19:00"
        )
        self.assertEqual(
            schedule.format_when((NOW + timedelta(days=1)).replace(hour=7, minute=0), NOW),
            "besok 07:00",
        )
        self.assertEqual(
            schedule.format_when((NOW + timedelta(days=3)).replace(hour=7, minute=0), NOW),
            "Rab 14 Okt 07:00",
        )


if __name__ == "__main__":
    unittest.main()
