"""Optional Telegram forwarding of alerts — best-effort, non-blocking transport only.

The one outbound-HTTP concern for pushing pre-formatted alert messages to a Telegram chat. Optional:
``from_config`` returns ``None`` when the credentials are absent, so the feature is simply off and the
app runs normally (unlike the mandatory Dhan creds). ``send`` never blocks the caller and never raises —
the POST runs on a throwaway daemon thread with a short timeout, so a slow/hung/failed Telegram can never
lag the ~1s render callbacks that call it or break alert detection.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

import requests

from config import AppConfig

_LOGGER = logging.getLogger("onechain.telegram")
_TIMEOUT = 5.0

# poster signature: (url, payload) -> None. Injectable so tests capture the payload without a network call.
Poster = Callable[[str, dict], None]


def _threaded_post(url: str, payload: dict) -> None:
    """Fire the Telegram POST on a daemon thread; log a failure or a refusal (best-effort).

    The URL carries the bot token, so a log line here never includes the URL or a traceback (the
    ``requests`` error text quotes the request path, token and all): a failure logs only the
    exception's type, a refusal only Telegram's status code and its own one-line reason."""

    def _worker() -> None:
        try:
            response = requests.post(url, json=payload, timeout=_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - best-effort; a Telegram failure must never propagate
            _LOGGER.error("Telegram send failed: %s", type(exc).__name__)
            return
        if not response.ok:
            _LOGGER.error("Telegram refused the message: HTTP %s %s", response.status_code, _refusal_reason(response))

    threading.Thread(target=_worker, daemon=True).start()


def _refusal_reason(response) -> str:
    """Telegram's own reason for a refusal (its ``description`` field, e.g. ``Bad Request: chat not
    found``), or an empty string when the reply carries none."""
    try:
        return str(response.json().get("description", ""))
    except (ValueError, AttributeError):
        return ""


class TelegramNotifier:
    """Sends pre-formatted HTML messages to one Telegram chat. Optional + best-effort."""

    def __init__(self, token: str, chat_id: str, *, poster: Poster | None = None) -> None:
        self._chat_id = chat_id
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._poster = poster or _threaded_post

    @classmethod
    def from_config(cls, config: AppConfig) -> "TelegramNotifier | None":
        """Build from ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` env vars; ``None`` when either is unset."""
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return None
        return cls(token, chat_id)

    def send(self, text: str) -> None:
        """Dispatch one HTML message (no-op on empty text). Never blocks, never raises."""
        if not text:
            return
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        try:
            self._poster(self._url, payload)
        except Exception as exc:  # noqa: BLE001 - even dispatch must never raise into the callback
            _LOGGER.error("Telegram dispatch failed: %s", type(exc).__name__)
