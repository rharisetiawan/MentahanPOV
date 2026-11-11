"""scripts/set_insights_creds.py: only a fully checked credential reaches .env."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import set_insights_creds as creds  # noqa: E402

ALL_SCOPES = sorted(creds.POSTING_SCOPES | creds.INSIGHT_SCOPES)
TOKEN = "SECRET-PAGE-TOKEN"
KEY = "SECRET-YT-KEY"


def fake_fetch(*, scopes=ALL_SCOPES, type_="PAGE", valid=True, me="111", expires=0,
               views_status=200, yt_status=200, yt_items=("c",)):
    def fetch(url, params):
        if "youtube" in url:
            body = {"items": list(yt_items)} if yt_status == 200 else {
                "error": {"message": f"bad key {params['key']}"}
            }
            return yt_status, body
        if url.endswith("/debug_token"):
            return 200, {"data": {"is_valid": valid, "type": type_, "scopes": scopes,
                                  "expires_at": expires}}
        if url.endswith("/me"):
            return 200, {"id": me}
        if url.endswith("/media"):
            return 200, {"data": [{"id": "9"}]}
        if url.endswith("/insights"):
            if views_status != 200:
                return views_status, {"error": {"message": "(#10) no permission"}}
            return 200, {"data": []}
        raise AssertionError(url)

    return fetch


class MetaTokenTests(unittest.TestCase):
    def _check(self, **kw):
        return creds.check_meta_token(
            TOKEN, page_id="111", ig_user_id="222", version="v19.0", fetch=fake_fetch(**kw)
        )

    def test_good_token_passes(self) -> None:
        self.assertIsNone(self._check())

    def test_user_token_is_refused(self) -> None:
        self.assertIn("not a Page token", self._check(type_="USER"))

    def test_missing_posting_scope_is_refused(self) -> None:
        problem = self._check(scopes=[s for s in ALL_SCOPES if s != "pages_manage_posts"])
        self.assertIn("pages_manage_posts", problem)

    def test_missing_insight_scope_is_refused(self) -> None:
        problem = self._check(scopes=sorted(creds.POSTING_SCOPES))
        self.assertIn("read_insights", problem)

    def test_other_page_invalid_short_lived_and_no_views(self) -> None:
        self.assertIn("different Page", self._check(me="999"))
        self.assertIn("not valid", self._check(valid=False))
        self.assertIn("7 days", self._check(expires=creds.time.time() + 3600))
        self.assertIn("views", self._check(views_status=400))


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.env = self.dir / ".env"
        self.original = b"FB_PAGE_ID=111\r\nIG_USER_ID=222\r\nFB_PAGE_ACCESS_TOKEN=old\r\nX=1\r\n"
        self.env.write_bytes(self.original)

    def _run(self, answers, **kw):
        out = io.StringIO()
        it = iter(answers)
        with contextlib.redirect_stdout(out):
            code = creds.run(
                self.env, restart=False, fetch=fake_fetch(**kw), ask=lambda _prompt: next(it)
            )
        return code, out.getvalue()

    def test_saves_both_and_keeps_the_rest_and_a_backup(self) -> None:
        code, out = self._run([KEY, TOKEN])
        self.assertEqual(code, 0)
        text = self.env.read_bytes()
        self.assertIn(f"FB_PAGE_ACCESS_TOKEN={TOKEN}\r\n".encode(), text)
        self.assertIn(f"YOUTUBE_API_KEY={KEY}".encode(), text)
        self.assertIn(b"X=1\r\n", text)
        backups = list(self.dir.glob(".env.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(KEY, out)

    def test_rejected_token_leaves_env_untouched(self) -> None:
        code, out = self._run(["", TOKEN], type_="USER")
        self.assertEqual(code, 1)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertEqual(list(self.dir.glob(".env.bak-*")), [])
        self.assertIn("not changed", out)

    def test_good_key_is_saved_even_if_the_token_fails(self) -> None:
        code, _ = self._run([KEY, TOKEN], valid=False)
        self.assertEqual(code, 1)
        text = self.env.read_bytes()
        self.assertIn(f"YOUTUBE_API_KEY={KEY}".encode(), text)
        self.assertIn(b"FB_PAGE_ACCESS_TOKEN=old", text)

    def test_secret_never_printed_when_refused(self) -> None:
        code, out = self._run([KEY, ""], yt_status=400)
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, out)
        self.assertIn("HTTP 400", out)

    def test_skipping_both_changes_nothing(self) -> None:
        code, _ = self._run(["", ""])
        self.assertEqual(code, 0)
        self.assertEqual(self.env.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
