"""Keep credentials out of the log output.

Telegram puts the bot token in the request *path* (``/bot<TOKEN>/getUpdates``)
and python-telegram-bot's HTTP client (httpx) logs every request URL at INFO,
so a plain ``logging.basicConfig(level=INFO)`` writes the token to the
container log on every poll. The Graph API calls in distributors/ carry
``access_token`` in the query string, which urllib3 prints at DEBUG — i.e.
under ``main.py --verbose``.

Call ``quiet_http_loggers()`` right after ``logging.basicConfig()``: it pins
the HTTP-client loggers to WARNING no matter how verbose the root logger is,
while the app's own ``mentahanpov.*`` loggers keep their level.
"""

from __future__ import annotations

import logging

NOISY_HTTP_LOGGERS = ("httpx", "httpcore", "urllib3", "hpack")


def quiet_http_loggers() -> None:
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
