"""No duplicate posts on a lost reply, Gemini uploads cleaned up, Threads token renewal."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from core import gemini, threads_token
from distributors import facebook, instagram, meta_graph, youtube


def _resp(status: int = 200, body: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body or {}).encode()
    r.url = "https://graph.facebook.com/v19.0/x?access_token=SECRET"
    return r


class DescribeHttpErrorTests(unittest.TestCase):
    def test_keeps_meta_message_and_drops_the_url(self) -> None:
        r = _resp(400, {"error": {"message": "Media ID is not available"}})
        with self.assertRaises(requests.HTTPError) as ctx:
            r.raise_for_status()
        text = meta_graph.describe_http_error(ctx.exception)
        self.assertEqual(text, "HTTP 400: Media ID is not available")
        self.assertNotIn("SECRET", text)


class PublishContainerTests(unittest.TestCase):
    def _publish(self, post_side_effect, status: str | None):
        with (
            mock.patch.object(
                meta_graph.requests, "post", side_effect=post_side_effect
            ) as post,
            mock.patch.object(meta_graph, "container_status", return_value=status),
            mock.patch.object(meta_graph.time, "sleep"),
        ):
            result = meta_graph.publish_container(
                publish_url="p",
                status_url="s",
                status_field="status_code",
                creation_id="C1",
                token="t",
                label="instagram",
            )
        return result, post

    def test_lost_reply_but_published_is_not_published_again(self) -> None:
        result, post = self._publish(requests.Timeout(), "PUBLISHED")
        self.assertIsNone(result)
        self.assertEqual(post.call_count, 1)

    def test_retry_reuses_the_same_container(self) -> None:
        result, post = self._publish(
            [requests.Timeout(), _resp(200, {"id": "M1"})], "FINISHED"
        )
        self.assertEqual(result, "M1")
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["data"]["creation_id"], "C1")

    def test_gives_up_with_a_reason(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            self._publish(lambda *a, **k: _resp(500), "FINISHED")


class InstagramNoDuplicateTests(unittest.TestCase):
    def test_lost_publish_reply_creates_one_container_only(self) -> None:
        cfg = SimpleNamespace(ig_user_id="IG", fb_page_access_token="tok")
        with (
            mock.patch.object(instagram, "config", cfg),
            mock.patch.object(
                instagram, "_create_container", return_value="C1"
            ) as create,
            mock.patch.object(instagram, "wait_container_finished"),
            mock.patch.object(instagram, "publish_container", return_value=None),
            mock.patch.object(
                instagram, "find_recent_media", return_value=("M9", "https://ig/p/x")
            ),
        ):
            out = instagram.post(Path("v.mp4"), "cap", post_url="https://u")
        create.assert_called_once()
        self.assertEqual(out, {"id": "M9", "url": "https://ig/p/x"})


class FacebookFinishTests(unittest.TestCase):
    def test_lost_finish_reply_with_video_present_is_success(self) -> None:
        with (
            mock.patch.object(
                facebook.requests, "post", side_effect=requests.Timeout()
            ) as post,
            mock.patch.object(facebook, "_video_exists", return_value=True),
            mock.patch.object(facebook.time, "sleep"),
        ):
            facebook._finish("ep", "S1", "V1", "cap", "tok")
        self.assertEqual(post.call_count, 1)

    def test_finish_failing_for_real_raises(self) -> None:
        with (
            mock.patch.object(facebook.requests, "post", side_effect=requests.Timeout()),
            mock.patch.object(facebook, "_video_exists", return_value=False),
            mock.patch.object(facebook.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "finish phase failed"),
        ):
            facebook._finish("ep", "S1", "V1", "cap", "tok")


class YouTubeResumeTests(unittest.TestCase):
    def test_no_whole_upload_retry_and_chunk_retries_on(self) -> None:
        self.assertFalse(hasattr(youtube.post, "retry"))
        request = mock.Mock()
        request.next_chunk.return_value = (None, {"id": "abc"})
        service = mock.Mock()
        service.videos.return_value.insert.return_value = request
        with (
            mock.patch.object(youtube, "_service", return_value=service),
            mock.patch.object(youtube, "MediaFileUpload"),
        ):
            out = youtube.post(Path("v.mp4"), "Title\nbody")
        self.assertEqual(out["id"], "abc")
        request.next_chunk.assert_called_with(num_retries=youtube.UPLOAD_RETRIES)


class GeminiCleanupTests(unittest.TestCase):
    def test_upload_deleted_even_when_generation_fails(self) -> None:
        client = mock.Mock()
        upload = SimpleNamespace(name="files/abc")
        cfg = mock.Mock(gemini_api_key="k", gemini_model="m", gemini_fallback_model="")
        with (
            mock.patch.object(gemini, "config", cfg),
            mock.patch.object(gemini.genai, "Client", return_value=client),
            mock.patch.object(gemini, "_upload_and_wait", return_value=upload),
            mock.patch.object(gemini, "_load_prompt", return_value="p"),
            mock.patch.object(
                gemini, "_generate_and_parse", side_effect=RuntimeError("503")
            ),
            self.assertRaises(RuntimeError),
        ):
            gemini.generate_metadata(
                Path("v.mp4"),
                date="d",
                address="a",
                coordinates="c",
                duration_s=1,
                resolution="r",
            )
        client.files.delete.assert_called_once_with(name="files/abc")


class ThreadsTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "threads-token.json"
        self.cfg = SimpleNamespace(
            threads_token_file=self.file,
            threads_access_token="env-token",
            threads_user_id="42",
            threads_api_version="v1.0",
        )
        patcher = mock.patch.object(threads_token, "config", self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_env_token_until_a_refresh_is_stored(self) -> None:
        self.assertEqual(threads_token.current(), "env-token")
        self.assertTrue(threads_token.needs_refresh())
        self.file.write_text(
            json.dumps({"access_token": "new", "expires_at": time.time() + 50 * 86400})
        )
        self.assertEqual(threads_token.current(), "new")
        self.assertFalse(threads_token.needs_refresh())

    def _get(self, me_id: str):
        def fake_get(url, params=None, timeout=None):
            if url == threads_token.REFRESH_URL:
                return _resp(200, {"access_token": "new", "expires_in": 60 * 86400})
            return _resp(200, {"id": me_id})

        return fake_get

    def test_refresh_stores_a_verified_token(self) -> None:
        with mock.patch.object(
            threads_token.requests, "get", side_effect=self._get("42")
        ):
            left = threads_token.refresh()
        self.assertGreater(left, 59)
        self.assertEqual(json.loads(self.file.read_text())["access_token"], "new")

    def test_token_for_another_account_is_not_stored(self) -> None:
        with (
            mock.patch.object(
                threads_token.requests, "get", side_effect=self._get("999")
            ),
            self.assertRaisesRegex(RuntimeError, "different account"),
        ):
            threads_token.refresh()
        self.assertFalse(self.file.exists())

    def test_failure_message_has_no_token(self) -> None:
        leak = requests.ConnectionError("https://x?access_token=env-token")
        with (
            mock.patch.object(threads_token.requests, "get", side_effect=leak),
            self.assertRaises(RuntimeError) as ctx,
        ):
            threads_token.refresh()
        self.assertNotIn("env-token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
