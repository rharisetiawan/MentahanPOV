"""HTTP-client loggers must never emit request URLs (they carry bot/API tokens)."""

from __future__ import annotations

import logging
import unittest

from core import logsafe


class QuietHttpLoggersTests(unittest.TestCase):
    def setUp(self) -> None:
        saved = {n: logging.getLogger(n).level for n in logsafe.NOISY_HTTP_LOGGERS}
        self.addCleanup(self._restore, saved)

    @staticmethod
    def _restore(saved: dict[str, int]) -> None:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)

    def test_info_and_debug_are_suppressed(self) -> None:
        for name in logsafe.NOISY_HTTP_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        logsafe.quiet_http_loggers()
        for name in logsafe.NOISY_HTTP_LOGGERS:
            lg = logging.getLogger(name)
            self.assertFalse(lg.isEnabledFor(logging.INFO), name)
            self.assertFalse(lg.isEnabledFor(logging.DEBUG), name)
            self.assertTrue(lg.isEnabledFor(logging.WARNING), name)

    def test_httpx_covers_the_telegram_polling_leak(self) -> None:
        # python-telegram-bot's client is httpx; it logs the token-bearing
        # URL at INFO on every getUpdates call.
        self.assertIn("httpx", logsafe.NOISY_HTTP_LOGGERS)
        logging.getLogger("httpx").setLevel(logging.INFO)
        logsafe.quiet_http_loggers()
        self.assertFalse(logging.getLogger("httpx").isEnabledFor(logging.INFO))

    def test_app_loggers_are_left_alone(self) -> None:
        app = logging.getLogger("mentahanpov.test_logsafe")
        app.setLevel(logging.INFO)
        logsafe.quiet_http_loggers()
        self.assertTrue(app.isEnabledFor(logging.INFO))


if __name__ == "__main__":
    unittest.main()
