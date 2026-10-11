"""core/cleanup.py — the rules that decide what gets deleted from disk.

These are the tests that matter most in the repo: a mistake here deletes
somebody's footage. Run with:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import cleanup

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def _iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


class CleanupPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "incoming").mkdir()
        (self.root / "state" / "posting_copies").mkdir(parents=True)
        self.state_file = self.root / "state" / "posts.json"
        self.entries: dict[str, dict] = {}

        self.cfg = SimpleNamespace(
            state_file=self.state_file,
            posting_copy_dir=Path("state/posting_copies"),
            cleanup_retention_days=3,
            disk_pressure_free_gb=2.0,
            abandoned_retention_days=0,  # off unless a test turns it on
        )
        patches = [
            mock.patch.object(cleanup, "config", self.cfg),
            mock.patch.object(cleanup, "REPO_ROOT", self.root),
            mock.patch.object(cleanup, "INCOMING_DIR", self.root / "incoming"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    # -- helpers --------------------------------------------------------

    def _add(
        self,
        key: str,
        *,
        age: timedelta,
        platforms: dict[str, str] | None = None,
        gdrive: bool = True,
        selected: list[str] | None = None,
        scheduled: bool = False,
        video_in: str = "incoming",
        recorded_video_path: str | None = None,
    ) -> tuple[Path, Path]:
        video = self.root / video_in / f"{key}.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"v" * 1000)
        copy = self.root / "state" / "posting_copies" / f"{key}_post.mp4"
        copy.write_bytes(b"c" * 500)
        entry: dict = {
            "video_path": recorded_video_path or str(video),
            "posting_copy_path": f"state/posting_copies/{key}_post.mp4",
            "updated_at": _iso(age),
            "platforms": {
                name: {"status": status} for name, status in (platforms or {}).items()
            },
        }
        if gdrive:
            entry["gdrive_url"] = "https://drive.example/x"
        if selected is not None:
            entry["selected_platforms"] = selected
        if scheduled:
            entry["scheduled_at"] = (NOW + timedelta(hours=1)).isoformat()
        self.entries[key] = entry
        self.state_file.write_text(json.dumps(self.entries), encoding="utf-8")
        return video, copy

    def _plan(self, **kw):
        kw.setdefault("pressure", False)
        return cleanup.assess(now=NOW, **kw)

    # -- the rules ------------------------------------------------------

    def test_finished_and_old_is_deleted(self) -> None:
        video, copy = self._add("done", age=timedelta(days=5), platforms={"youtube": "ok"})
        report = self._plan()
        self.assertEqual({i.path for i in report.items}, {video, copy})
        self.assertEqual(report.total_bytes, 1500)

    def test_finished_but_recent_is_kept_and_counted(self) -> None:
        self._add("fresh", age=timedelta(days=1), platforms={"youtube": "ok"})
        report = self._plan()
        self.assertEqual(report.items, [])
        self.assertEqual(report.soon_videos, 1)

    def test_any_failed_platform_holds_everything_back(self) -> None:
        self._add(
            "partial",
            age=timedelta(days=30),
            platforms={"youtube": "ok", "tiktok": "error"},
        )
        report = self._plan()
        self.assertEqual(report.items, [])
        self.assertEqual(report.held_back_videos, 1)

    def test_no_drive_backup_is_never_deleted(self) -> None:
        self._add("nodrive", age=timedelta(days=30), platforms={"youtube": "ok"}, gdrive=False)
        self.assertEqual(self._plan().items, [])

    def test_no_platform_results_is_never_deleted(self) -> None:
        self._add("untouched", age=timedelta(days=30), platforms={})
        self.assertEqual(self._plan().items, [])

    def test_run_still_in_flight_is_not_mistaken_for_finished(self) -> None:
        # Parallel posting records platforms one by one: youtube is "ok",
        # facebook was selected but has no result yet.
        self._add(
            "inflight",
            age=timedelta(days=30),
            platforms={"youtube": "ok"},
            selected=["youtube", "facebook"],
        )
        report = self._plan()
        self.assertEqual(report.items, [])
        self.assertEqual(report.held_back_videos, 1)

    def test_selected_all_ok_is_deleted(self) -> None:
        self._add(
            "complete",
            age=timedelta(days=30),
            platforms={"youtube": "ok", "facebook": "ok"},
            selected=["youtube", "facebook"],
        )
        self.assertEqual(len(self._plan().items), 2)

    def test_scheduled_post_keeps_its_files(self) -> None:
        self._add(
            "sched", age=timedelta(days=30), platforms={"youtube": "ok"}, scheduled=True
        )
        self.assertEqual(self._plan().items, [])

    def test_original_outside_incoming_is_never_touched(self) -> None:
        # Someone ran main.py by hand on a file from their own footage
        # folder — that is their original, not a staging copy.
        video, copy = self._add(
            "byhand",
            age=timedelta(days=30),
            platforms={"youtube": "ok"},
            video_in="my-footage",
        )
        report = self._plan()
        self.assertEqual([i.path for i in report.items], [copy])
        self.assertTrue(video.exists())

    def test_container_path_is_mapped_for_a_host_checkout(self) -> None:
        video, copy = self._add(
            "boxrun",
            age=timedelta(days=5),
            platforms={"youtube": "ok"},
            recorded_video_path="/app/incoming/boxrun.mp4",
        )
        report = self._plan()
        self.assertIn(video, {i.path for i in report.items})

    def test_path_traversal_in_state_is_rejected(self) -> None:
        outside = self.root / "precious.txt"
        outside.write_text("keep me")
        self._add(
            "evil",
            age=timedelta(days=30),
            platforms={"youtube": "ok"},
            recorded_video_path=str(self.root / "incoming" / ".." / "precious.txt"),
        )
        report = self._plan()
        self.assertNotIn(outside, {i.path for i in report.items})
        cleanup.execute(report)
        self.assertTrue(outside.exists())

    # -- pressure mode --------------------------------------------------

    def test_pressure_skips_retention_but_keeps_a_floor(self) -> None:
        self._add("old1h", age=timedelta(hours=1), platforms={"youtube": "ok"})
        self._add("just", age=timedelta(minutes=5), platforms={"youtube": "ok"})
        normal = self._plan()
        self.assertEqual(normal.items, [])

        pressured = self._plan(pressure=True)
        self.assertEqual({i.key for i in pressured.items}, {"old1h"})
        self.assertEqual(pressured.soon_videos, 1)

    def test_pressure_still_respects_failures(self) -> None:
        self._add("bad", age=timedelta(hours=5), platforms={"youtube": "error"})
        self.assertEqual(self._plan(pressure=True).items, [])

    def test_exclude_keys(self) -> None:
        self._add("a", age=timedelta(days=5), platforms={"youtube": "ok"})
        self._add("b", age=timedelta(days=5), platforms={"youtube": "ok"})
        report = self._plan(exclude_keys=frozenset({"a"}))
        self.assertEqual({i.key for i in report.items}, {"b"})

    # -- applying -------------------------------------------------------

    def test_apply_deletes_and_counts(self) -> None:
        video, copy = self._add("x", age=timedelta(days=5), platforms={"youtube": "ok"})
        report = cleanup.execute(self._plan())
        self.assertFalse(video.exists())
        self.assertFalse(copy.exists())
        self.assertEqual(report.deleted, 2)
        self.assertEqual(report.freed_bytes, 1500)
        self.assertTrue(report.applied)

    def test_dry_run_deletes_nothing(self) -> None:
        video, _ = self._add("y", age=timedelta(days=5), platforms={"youtube": "ok"})
        cleanup.assess(now=NOW, pressure=False)
        self.assertTrue(video.exists())

    def test_shared_file_is_counted_once(self) -> None:
        # Two state entries pointing at the same posting copy must not make
        # the report double-count (or double-delete) it.
        _, copy = self._add("p", age=timedelta(days=5), platforms={"youtube": "ok"})
        self.entries["q"] = dict(self.entries["p"], video_path=str(self.root / "incoming" / "none.mp4"))
        self.state_file.write_text(json.dumps(self.entries), encoding="utf-8")
        report = self._plan()
        self.assertEqual([i.path for i in report.items].count(copy), 1)


class AbandonedAndOrphanTests(CleanupPlanTests):
    """Reuses the fixture (and so re-runs the base rules once more with the
    abandoned feature switched on — they must all still hold)."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.abandoned_retention_days = 7

    def test_no_platform_results_is_never_deleted(self) -> None:
        # The one base rule deliberately superseded here: with the abandoned
        # window on, a result-less entry *with a Drive backup* is eventually
        # deleted (that's the point) — but only past the window, and the
        # fresh case is covered by test_recent_unconfirmed_video_is_still_waiting.
        self._add("untouched", age=timedelta(days=1), platforms={})
        self.assertEqual(self._plan().items, [])

    def test_dry_run_never_posted_is_deleted_after_the_window(self) -> None:
        video, copy = self._add("ghost", age=timedelta(days=10), platforms={})
        report = self._plan()
        self.assertEqual({i.path for i in report.items}, {video, copy})
        self.assertEqual({i.reason for i in report.items}, {"abandoned"})
        self.assertEqual(report.abandoned_videos, 1)
        self.assertEqual(report.abandoned_bytes, 1500)

    def test_recent_unconfirmed_video_is_still_waiting_for_a_decision(self) -> None:
        self._add("pending", age=timedelta(days=3), platforms={})
        report = self._plan()
        self.assertEqual(report.items, [])
        self.assertEqual(report.held_back_videos, 1)

    def test_abandoned_needs_the_drive_backup(self) -> None:
        self._add("nodrive", age=timedelta(days=30), platforms={}, gdrive=False)
        self.assertEqual(self._plan().items, [])

    def test_abandoned_never_touches_a_scheduled_video(self) -> None:
        self._add("sched", age=timedelta(days=30), platforms={}, scheduled=True)
        self.assertEqual(self._plan().items, [])

    def test_a_video_with_any_posting_attempt_is_not_abandoned(self) -> None:
        self._add("tried", age=timedelta(days=30), platforms={"youtube": "error"})
        report = self._plan()
        self.assertEqual(report.items, [])
        self.assertEqual(report.held_back_videos, 1)

    def test_pressure_shortens_the_abandoned_window_to_a_day(self) -> None:
        self._add("two_days", age=timedelta(days=2), platforms={})
        self.assertEqual(self._plan().items, [])
        self.assertEqual(len(self._plan(pressure=True).items), 2)

    def test_abandoned_can_be_switched_off(self) -> None:
        self.cfg.abandoned_retention_days = 0
        self._add("ghost", age=timedelta(days=30), platforms={})
        self.assertEqual(self._plan().items, [])

    def test_story_copy_goes_with_its_video(self) -> None:
        self._add("withstory", age=timedelta(days=5), platforms={"youtube": "ok"})
        story = self.root / "state" / "posting_copies" / "withstory_story.mp4"
        story.write_bytes(b"s" * 300)
        report = self._plan()
        self.assertIn(story, {i.path for i in report.items})
        self.assertEqual(report.total_bytes, 1800)

    def test_orphans_are_reported_but_never_deleted(self) -> None:
        stray = self.root / "incoming" / "half-downloaded.mp4"
        stray.write_bytes(b"o" * 700)
        old = (NOW - timedelta(days=3)).timestamp()
        import os

        os.utime(stray, (old, old))
        fresh = self.root / "incoming" / "downloading-now.mp4"
        fresh.write_bytes(b"f" * 100)
        os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
        self._add("tracked", age=timedelta(days=5), platforms={"youtube": "ok"})
        os.utime(
            self.root / "incoming" / "tracked.mp4", (old, old)
        )  # old but referenced -> not an orphan

        report = self._plan()
        self.assertEqual(report.orphan_files, 1)
        self.assertEqual(report.orphan_bytes, 700)
        cleanup.execute(report)
        self.assertTrue(stray.exists(), "orphans must never be deleted")
        self.assertTrue(fresh.exists())


class RunFromRepoRootTests(CleanupPlanTests):
    """The production case: the bot's cwd *is* the repo root (/app), so the
    relative posting_copy_path recorded in state.json resolves from the
    current directory. This is the setup that hid a real bug — every other
    test ran from some unrelated cwd, where relative paths didn't exist and
    took the join-with-REPO_ROOT branch instead."""

    def setUp(self) -> None:
        super().setUp()
        import os

        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def test_relative_posting_copy_is_found_and_deleted(self) -> None:
        video, copy = self._add("inbox", age=timedelta(days=5), platforms={"youtube": "ok"})
        report = self._plan()
        self.assertEqual({i.path for i in report.items}, {video, copy})
        self.assertEqual(report.orphan_files, 0, "tracked copy must not look like an orphan")
        cleanup.execute(report)
        self.assertFalse(copy.exists())

    def test_localize_always_returns_an_absolute_path(self) -> None:
        (self.root / "state" / "posting_copies" / "r_post.mp4").write_bytes(b"x")
        result = cleanup.localize("state/posting_copies/r_post.mp4")
        self.assertTrue(result.is_absolute())
        self.assertEqual(result, self.root / "state" / "posting_copies" / "r_post.mp4")


class PressureThresholdTests(unittest.TestCase):
    """Numbers are the HG680's real ones, read off its CasaOS dashboard on
    2026-10-11: 6.47 GB internal storage, 4.74 GB used."""

    GB = 1024**3

    def _under(self, free_gb: float, total_gb: float, setting: float = 2.0) -> bool:
        usage = SimpleNamespace(free=int(free_gb * self.GB), total=int(total_gb * self.GB))
        cfg = SimpleNamespace(disk_pressure_free_gb=setting)
        with (
            mock.patch.object(cleanup, "config", cfg),
            mock.patch.object(cleanup, "disk_usage", return_value=usage),
        ):
            return cleanup.under_pressure()

    def test_threshold_is_capped_on_a_small_disk(self) -> None:
        cfg = SimpleNamespace(disk_pressure_free_gb=2.0)
        total = int(6.47 * self.GB)
        with mock.patch.object(cleanup, "config", cfg):
            self.assertAlmostEqual(cleanup.pressure_threshold_bytes(total), total * 0.25)

    def test_large_disk_uses_the_configured_value(self) -> None:
        cfg = SimpleNamespace(disk_pressure_free_gb=2.0)
        with mock.patch.object(cleanup, "config", cfg):
            self.assertEqual(
                cleanup.pressure_threshold_bytes(500 * self.GB), 2.0 * self.GB
            )

    def test_the_real_box_is_not_in_permanent_pressure(self) -> None:
        self.assertFalse(self._under(free_gb=6.47 - 4.74, total_gb=6.47))

    def test_but_it_is_when_it_actually_gets_tight(self) -> None:
        self.assertTrue(self._under(free_gb=1.2, total_gb=6.47))

    def test_big_disk_still_alarms_at_the_configured_value(self) -> None:
        self.assertTrue(self._under(free_gb=1.5, total_gb=500))
        self.assertFalse(self._under(free_gb=3.0, total_gb=500))


class LocalizeTests(unittest.TestCase):
    def test_relative_path_resolves_against_repo(self) -> None:
        with mock.patch.object(cleanup, "REPO_ROOT", Path("/srv/repo")):
            self.assertEqual(
                cleanup.localize("state/posting_copies/a.mp4"),
                Path("/srv/repo") / "state/posting_copies/a.mp4",
            )

    def test_app_prefix_is_remapped(self) -> None:
        with mock.patch.object(cleanup, "REPO_ROOT", Path("/srv/repo")):
            self.assertEqual(
                cleanup.localize("/app/incoming/a.mp4"),
                Path("/srv/repo") / "incoming" / "a.mp4",
            )


if __name__ == "__main__":
    unittest.main()
