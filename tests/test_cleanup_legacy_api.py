"""The cleanup contract that was already deployed on the box (b81febb).

scripts/mentahan.sh (`mentahan cleanup`) calls, inside the bot container:

    c = cleanup.plan(config.state_file,
                     retention_days=config.cleanup_retention_days,
                     ignore_platforms=config.cleanup_ignore_platforms,
                     allowed_roots=[root / "incoming", config.posting_copy_dir],
                     posting_copy_dir=config.posting_copy_dir)
    files, freed = cleanup.apply(c)

If the engine behind that is ever changed in a way that breaks this exact call,
the box's own maintenance helper silently stops working — so it is pinned here
as a test instead of in someone's memory.
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


class LegacyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "incoming").mkdir()
        (self.root / "state" / "posting_copies").mkdir(parents=True)
        self.state_file = self.root / "state" / "posts.json"
        self.cfg = SimpleNamespace(
            state_file=self.state_file,
            posting_copy_dir=Path("state/posting_copies"),
            cleanup_retention_days=3,
            cleanup_ignore_platforms=("tiktok",),
            disk_pressure_free_gb=2.0,
            abandoned_retention_days=7,  # on, as shipped — plan() must still behave
        )
        for p in (
            mock.patch.object(cleanup, "config", self.cfg),
            mock.patch.object(cleanup, "REPO_ROOT", self.root),
            mock.patch.object(cleanup, "INCOMING_DIR", self.root / "incoming"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.entries: dict[str, dict] = {}

    def _add(self, key: str, platforms: dict[str, str], *, age_days: float = 5) -> tuple[Path, Path]:
        video = self.root / "incoming" / f"{key}.mp4"
        video.write_bytes(b"v" * 100)
        copy = self.root / "state" / "posting_copies" / f"{key}_post.mp4"
        copy.write_bytes(b"c" * 50)
        self.entries[key] = {
            "video_path": str(video),
            "posting_copy_path": f"state/posting_copies/{key}_post.mp4",
            "gdrive_url": "https://drive.example/x",
            "updated_at": (NOW - timedelta(days=age_days)).isoformat(),
            "platforms": {n: {"status": s} for n, s in platforms.items()},
        }
        self.state_file.write_text(json.dumps(self.entries), encoding="utf-8")
        return video, copy

    def _mentahan_sh_call(self):
        """Exactly the keyword set scripts/mentahan.sh uses."""
        return cleanup.plan(
            self.cfg.state_file,
            retention_days=self.cfg.cleanup_retention_days,
            ignore_platforms=self.cfg.cleanup_ignore_platforms,
            allowed_roots=[self.root / "incoming", self.cfg.posting_copy_dir],
            posting_copy_dir=self.cfg.posting_copy_dir,
            now=NOW,
        )

    def test_returns_candidates_with_path_size_key(self) -> None:
        video, copy = self._add("done", {"youtube": "ok", "facebook": "ok"})
        candidates = self._mentahan_sh_call()
        self.assertEqual({c.path for c in candidates}, {video, copy})
        by_path = {c.path: c for c in candidates}
        self.assertEqual(by_path[video].size, 100)
        self.assertEqual(by_path[video].key, "done")

    def test_apply_returns_files_and_bytes_and_deletes(self) -> None:
        video, copy = self._add("done", {"youtube": "ok"})
        files, freed = cleanup.apply(self._mentahan_sh_call())
        self.assertEqual((files, freed), (2, 150))
        self.assertFalse(video.exists())
        self.assertFalse(copy.exists())

    def test_ignored_platform_error_does_not_block(self) -> None:
        # TikTok is a manual hand-off: its "error" must not pin the files.
        video, _ = self._add("tt", {"youtube": "ok", "tiktok": "error"})
        self.assertIn(video, {c.path for c in self._mentahan_sh_call()})

    def test_non_ignored_error_still_blocks(self) -> None:
        self._add("fb", {"youtube": "ok", "facebook": "error", "tiktok": "ok"})
        self.assertEqual(self._mentahan_sh_call(), [])

    def test_only_an_ignored_platform_result_is_not_enough(self) -> None:
        # Nothing but a TikTok result says nothing about the real platforms.
        self._add("onlytt", {"tiktok": "ok"})
        self.assertEqual(self._mentahan_sh_call(), [])

    def test_retention_is_honoured_and_pressure_never_applies(self) -> None:
        self._add("fresh", {"youtube": "ok"}, age_days=1)
        self.assertEqual(self._mentahan_sh_call(), [])

    def test_engine_default_uses_configured_ignore_platforms(self) -> None:
        # The bot path (no explicit overrides) must honour the same setting.
        video, _ = self._add("tt", {"youtube": "ok", "tiktok": "error"})
        report = cleanup.assess(now=NOW, pressure=False)
        self.assertIn(video, {i.path for i in report.items})

    def test_selected_platform_without_result_still_protects_in_flight_runs(self) -> None:
        self._add("inflight", {"youtube": "ok"})
        self.entries["inflight"]["selected_platforms"] = ["youtube", "facebook"]
        self.state_file.write_text(json.dumps(self.entries), encoding="utf-8")
        self.assertEqual(self._mentahan_sh_call(), [])


if __name__ == "__main__":
    unittest.main()
