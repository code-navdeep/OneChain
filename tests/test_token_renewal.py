from __future__ import annotations

from pathlib import Path

from services.token_renewal_service import _write_access_token_to_env, renew_access_token


class _FakeApi:
    def __init__(self, token):
        self._token = token

    def renew_token(self):
        return self._token


def test_writeback_replaces_only_token_line_and_preserves_others(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("CLIENT_ID=abc\nACCESS_TOKEN=OLD\n# a comment\nDEBUG=false\n", encoding="utf-8")
    _write_access_token_to_env(env, "NEW")
    text = env.read_text(encoding="utf-8")
    assert "ACCESS_TOKEN=NEW\n" in text
    assert "ACCESS_TOKEN=OLD" not in text
    # every other line preserved byte-for-byte
    assert "CLIENT_ID=abc\n" in text
    assert "# a comment\n" in text
    assert "DEBUG=false\n" in text


def test_writeback_appends_when_key_missing(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("CLIENT_ID=abc", encoding="utf-8")  # no trailing newline
    _write_access_token_to_env(env, "NEW")
    text = env.read_text(encoding="utf-8")
    assert "CLIENT_ID=abc\n" in text
    assert "ACCESS_TOKEN=NEW\n" in text


def test_renew_access_token_writes_new_token(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ACCESS_TOKEN=OLD\n", encoding="utf-8")
    result = renew_access_token(_FakeApi("NEW"), env)
    assert result == "NEW"
    assert "ACCESS_TOKEN=NEW" in env.read_text(encoding="utf-8")


def test_renew_access_token_none_and_file_untouched_without_api_service(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ACCESS_TOKEN=OLD\n", encoding="utf-8")
    assert renew_access_token(None, env) is None
    assert env.read_text(encoding="utf-8") == "ACCESS_TOKEN=OLD\n"  # untouched


def test_renew_access_token_none_and_file_untouched_when_api_returns_nothing(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ACCESS_TOKEN=OLD\n", encoding="utf-8")
    assert renew_access_token(_FakeApi(None), env) is None
    assert env.read_text(encoding="utf-8") == "ACCESS_TOKEN=OLD\n"  # untouched
