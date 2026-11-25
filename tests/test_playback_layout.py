from __future__ import annotations

from features.playback.layout import build_playback_drawer_body


def test_playback_drawer_contains_historical_selectors():
    body = build_playback_drawer_body()
    rendered = repr(body)
    assert "playback-asset-selector" in rendered
    assert "playback-expiry-selector" in rendered
    assert "playback-date-selector" in rendered
    assert "playback-moment-selector" in rendered
    assert "playback-bookmark-jump-selector" in rendered
    assert "playback-back-button" in rendered
