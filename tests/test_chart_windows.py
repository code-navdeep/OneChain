from __future__ import annotations

from datetime import datetime, timedelta

from analytics.chart_windows import rolling_window_range


def test_early_session_first_point_near_left_edge():
    # Little data yet: first point sits left_pad minutes from the left edge, window stays full width.
    first = datetime(2026, 6, 22, 9, 16)
    newest = datetime(2026, 6, 22, 9, 22)  # 6 min of data
    left, right = rolling_window_range(
        first_t=first, right_anchor=newest,
        window_minutes=30, left_pad_minutes=3, right_pad_minutes=3,
    )
    assert right - left == timedelta(minutes=30)          # always full window width
    assert first - left == timedelta(minutes=3)           # first sits 3 min in from the left (near, not stuck)


def test_window_rolls_once_anchor_advances():
    # Plenty of data: the right edge tracks (newest + right_pad), window rolls, first scrolls off-view.
    first = datetime(2026, 6, 22, 9, 16)
    newest = datetime(2026, 6, 22, 12, 0)
    left, right = rolling_window_range(
        first_t=first, right_anchor=newest,
        window_minutes=30, left_pad_minutes=3, right_pad_minutes=3,
    )
    assert right == newest + timedelta(minutes=3)         # 3 min of right padding past the newest point
    assert right - left == timedelta(minutes=30)


def test_price_style_centred_anchor():
    # The price chart's centring rule (left_pad = window/2 → first candle centred early-session), here
    # with a 2h window; the price chart itself uses 3h.
    first = datetime(2026, 6, 22, 9, 16)
    newest = datetime(2026, 6, 22, 9, 30)
    left, right = rolling_window_range(
        first_t=first, right_anchor=newest,
        window_minutes=120, left_pad_minutes=60, right_pad_minutes=15,
    )
    # max(first + 1h, newest + 15min) = max(10:16, 09:45) = 10:16; left = 08:16 → first is 1h in (centred).
    assert right == first + timedelta(hours=1)
    assert first - left == timedelta(hours=1)
