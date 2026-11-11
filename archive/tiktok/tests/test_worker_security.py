"""worker_service.py fails closed and bounds its inputs (archived with TikTok).

Run from the repo root after restoring the files (see ../README.md).
"""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest import mock

import worker_service


class _FakeHandler:
    """Just enough of BaseHTTPRequestHandler for Handler.do_POST."""

    def __init__(self, body: bytes, length: str | None = None) -> None:
        self.path = "/tiktok-job"
        self.headers = {"Content-Length": length if length is not None else str(len(body))}
        self.rfile = io.BytesIO(body)
        self.sent: list[tuple[int, dict]] = []

    def _send_json(self, status: int, body: dict) -> None:
        self.sent.append((status, body))


def _post(body: dict | bytes, secret: str, length: str | None = None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    h = _FakeHandler(raw, length)
    cfg = mock.Mock(tiktok_worker_shared_secret=secret)
    with mock.patch.object(worker_service, "config", cfg), mock.patch.object(
        worker_service, "_download"
    ) as dl, mock.patch.object(worker_service.tiktok, "post", return_value={"url": "u"}):
        worker_service.Handler.do_POST(h)  # type: ignore[arg-type]
    return h.sent[0], dl


class WorkerAuthTests(unittest.TestCase):
    def test_empty_secret_rejects_everything(self) -> None:
        (status, _), dl = _post({"video_url": "http://x/v.mp4", "secret": ""}, "")
        self.assertEqual(status, 403)
        dl.assert_not_called()

    def test_wrong_secret_rejected(self) -> None:
        (status, _), _ = _post({"video_url": "http://x/v.mp4", "secret": "no"}, "s")
        self.assertEqual(status, 403)

    def test_good_secret_runs_job(self) -> None:
        (status, body), dl = _post({"video_url": "http://x/v.mp4", "secret": "s"}, "s")
        self.assertEqual((status, body["status"]), (200, "ok"))
        dl.assert_called_once()

    def test_bad_content_length(self) -> None:
        (status, _), _ = _post(b"{}", "s", length="abc")
        self.assertEqual(status, 400)
        (status, _), _ = _post(b"{}", "s", length=str(worker_service.MAX_REQUEST_BYTES + 1))
        self.assertEqual(status, 400)

    def test_non_object_body(self) -> None:
        (status, _), _ = _post(b"[1]", "s")
        self.assertEqual(status, 400)

    def test_download_rejects_non_http_scheme(self) -> None:
        with self.assertRaises(ValueError):
            worker_service._download("file:///etc/passwd", Path("x"))



if __name__ == "__main__":
    unittest.main()
