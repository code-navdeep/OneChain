"""Tests for the optional Telegram notifier (best-effort transport, no network)."""

from __future__ import annotations

import pytest

from services.telegram_notifier import TelegramNotifier


def test_from_config_returns_none_without_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramNotifier.from_config(object()) is None


def test_from_config_needs_both(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramNotifier.from_config(object()) is None


def test_from_config_builds_instance(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert isinstance(TelegramNotifier.from_config(object()), TelegramNotifier)


def test_send_posts_expected_payload():
    captured: list[tuple[str, dict]] = []
    notifier = TelegramNotifier("tok", "123", poster=lambda url, payload: captured.append((url, payload)))

    notifier.send("<b>hi</b>")

    assert len(captured) == 1
    url, payload = captured[0]
    assert url == "https://api.telegram.org/bottok/sendMessage"
    assert payload == {"chat_id": "123", "text": "<b>hi</b>", "parse_mode": "HTML"}


def test_send_empty_is_noop():
    captured: list = []
    notifier = TelegramNotifier("tok", "123", poster=lambda url, payload: captured.append(payload))
    notifier.send("")
    assert captured == []


def test_send_swallows_poster_failure():
    def _boom(url, payload):
        raise RuntimeError("network down")

    notifier = TelegramNotifier("tok", "123", poster=_boom)
    # Must not raise into the caller (a Dash callback).
    notifier.send("message")
