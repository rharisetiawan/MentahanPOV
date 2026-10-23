"""scripts/watchdog.py against a throwaway local HTTP server."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "watchdog_script", Path(__file__).resolve().parent.parent / "scripts" / "watchdog.py"
)
watchdog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watchdog)


class _Handler(BaseHTTPRequestHandler):
    body: dict | None = {"heartbeat_age_s": 30.0}
    status = 200

    def do_GET(self) -> None:  # noqa: N802
        payload = json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence
        pass


class WatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "wd.json"
        _Handler.body, _Handler.status = {"heartbeat_age_s": 30.0}, 200
        self.sent: list[str] = []
        p = mock.patch.object(
            watchdog, "_send", side_effect=lambda t, c, text: self.sent.append(text) or True
        )
        p.start()
        self.addCleanup(p.stop)

    def _run(self, port: int | None = None, *extra: str) -> int:
        argv = [
            "watchdog.py",
            "--url", f"http://127.0.0.1:{port or self.port}/heartbeat",
            "--token", "T", "--chat-id", "1",
            "--state-file", str(self.state),
            "--timeout", "2",
            *extra,
        ]  # fmt: skip
        with mock.patch.object(sys, "argv", argv):
            return watchdog.main()

    def test_healthy_is_silent(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.sent, [])

    def test_unreachable_alerts_once_after_threshold_then_reports_recovery(self) -> None:
        dead_port = 1  # nothing listens here
        for _ in range(2):
            self.assertEqual(self._run(dead_port), 1)
        self.assertEqual(self.sent, [], "must not alert before the threshold")
        self._run(dead_port)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("bermasalah", self.sent[0])
        self._run(dead_port)
        self._run(dead_port)
        self.assertEqual(len(self.sent), 1, "must not repeat the alert every check")

        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("pulih", self.sent[1])

    def test_stale_heartbeat_counts_as_down_even_though_the_dashboard_answers(self) -> None:
        _Handler.body = {"heartbeat_age_s": 3 * 3600.0}
        for _ in range(3):
            self.assertEqual(self._run(), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("heartbeat basi", self.sent[0])

    def test_never_beat_is_reported(self) -> None:
        _Handler.body = {"heartbeat_age_s": None}
        for _ in range(3):
            self._run()
        self.assertIn("belum pernah", self.sent[0])

    def test_missing_config_exits_2(self) -> None:
        import os

        with mock.patch.object(sys, "argv", ["watchdog.py"]), mock.patch.dict(
            "os.environ"
        ):
            for name in ("WATCHDOG_URL", "WATCHDOG_TELEGRAM_TOKEN", "WATCHDOG_CHAT_ID"):
                os.environ.pop(name, None)
            self.assertEqual(watchdog.main(), 2)


if __name__ == "__main__":
    unittest.main()
