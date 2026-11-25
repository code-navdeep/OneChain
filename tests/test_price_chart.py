"""Price-chart rebuild: session-anchored read + rolling-3h viewport + padded y-axis."""

from __future__ import annotations

from pathlib import Path

import pytest

from features.bottom_panels.rendering import build_price_figure
from repositories.market_data_repository import MarketDataRepository


def _series(*pairs: tuple[str, float]) -> list[dict]:
    """price_series rows: each pair is (HH:MM:SS, spot) on a fixed session date."""
    return [{"timestamp": f"2026-06-13T{hms}", "underlying_price": price} for hms, price in pairs]


def _candle_count(fig) -> int:
    return len(fig.data[0].x)


# --- viewport geometry -------------------------------------------------------------- #


def test_early_session_centres_the_first_candle():
    # 3 candles spanning 10 min (early session) → right edge = first + 1.5h, first candle centred.
    fig = build_price_figure(
        _series(("09:15:10", 100), ("09:18:00", 101), ("09:22:00", 102), ("09:26:00", 103)), "Nifty"
    )
    left, right = fig.layout.xaxis.range
    assert right.endswith("10:45:00")  # first(09:15) + 1.5h
    assert left.endswith("07:45:00")   # 3h-wide window → first candle sits at the centre
    assert _candle_count(fig) == 3


def test_steady_session_rolls_with_newest_pinned_off_the_right_edge():
    # Candles every 5 min from 09:15 to 13:00 (span 3h45m > 3h) → rolling window.
    ticks = []
    minute = 15
    hour = 9
    while (hour, minute) <= (13, 0):
        ticks.append((f"{hour:02d}:{minute:02d}:05", 100 + minute))
        minute += 5
        if minute == 60:
            minute, hour = 0, hour + 1
    fig = build_price_figure(_series(*ticks), "Nifty")
    left, right = fig.layout.xaxis.range
    assert right.endswith("13:15:00")  # newest(13:00) + 15min (3-candle margin)
    assert left.endswith("10:15:00")   # right - 3h
    # Every session candle is in the trace even though the default view is only 3h
    # (candles before 10:15 are off-view-left but pannable).
    assert _candle_count(fig) == len(ticks)
    assert _candle_count(fig) > 36  # more than fit in the 3h window → proves nothing is truncated


def test_window_is_always_three_hours_wide():
    from datetime import datetime

    fig = build_price_figure(_series(("09:15:00", 100), ("09:20:00", 101)), "Nifty")
    left, right = fig.layout.xaxis.range
    span = datetime.fromisoformat(right) - datetime.fromisoformat(left)
    assert span.total_seconds() == 3 * 3600  # fixed 3h → fixed candle width


# --- y-axis + hover ----------------------------------------------------------------- #


def test_y_range_is_explicit_and_padded_fifteen_percent():
    # Two candles within view: high 110, low 100 → pad = 0.15 * 10 = 1.5 → range [98.5, 111.5].
    fig = build_price_figure(_series(("09:15:00", 100), ("09:20:00", 110)), "Nifty")
    lo, hi = fig.layout.yaxis.range
    assert lo == pytest.approx(98.5)
    assert hi == pytest.approx(111.5)


def test_pain_lines_drawn_and_expand_y_range():
    # Candles 100–110 (span 10 → ⅓ ceiling 3×=30). A put line at 90 makes the band [90,110]=20 ≤ 30,
    # so it's pulled in; before the 15% pad: span 20 → pad 0.15*20=3 → [87, 113]. Two lines drawn.
    fig = build_price_figure(
        _series(("09:15:00", 100), ("09:20:00", 110)), "Nifty",
        call_pain_level=108.0, put_pain_level=90.0,
    )
    line_shapes = [s for s in fig.layout.shapes if s.type == "line"]
    assert len(line_shapes) == 2
    lo, hi = fig.layout.yaxis.range
    assert lo == pytest.approx(87.0)
    assert hi == pytest.approx(113.0)


def test_far_pain_line_vanishes_keeping_candle_band_above_one_third():
    # Candles 100–110 (span 10 → ⅓ ceiling 30). A put wall at 50 would make the band [50,110]=60 > 30,
    # crushing the candles below ⅓ → it's dropped from the y-range (still drawn, but off-screen). The
    # range stays on the candles: candle-only [100,110] + 15% pad → [98.5, 111.5].
    fig = build_price_figure(
        _series(("09:15:00", 100), ("09:20:00", 110)), "Nifty",
        put_pain_level=50.0,
    )
    line_shapes = [s for s in fig.layout.shapes if s.type == "line"]
    assert len(line_shapes) == 1            # the wall line is still drawn...
    lo, hi = fig.layout.yaxis.range
    assert lo == pytest.approx(98.5)        # ...but the range stays on the candles (line off-screen)
    assert hi == pytest.approx(111.5)
    assert lo > 50.0                        # the far wall sits below the visible range
    assert 10.0 >= (hi - lo) / 3            # candle band ≥ ⅓ of the visible (padded) height


def test_hover_shows_two_decimals_and_no_si_suffix():
    fig = build_price_figure(_series(("09:15:00", 22500.5), ("09:20:00", 22501.25)), "Nifty")
    assert fig.layout.yaxis.hoverformat == ".2f"


# --- empty / partial state ---------------------------------------------------------- #


def test_single_candle_renders_a_candlestick_not_the_empty_note():
    fig = build_price_figure(_series(("09:15:30", 100)), "Nifty")
    assert len(fig.data) == 1
    assert fig.data[0].type == "candlestick"
    assert not fig.layout.annotations  # no "No price history" note


def test_no_candles_shows_the_dark_empty_note():
    fig = build_price_figure([], "Nifty")
    assert len(fig.data) == 0
    assert fig.layout.annotations[0].text == "No price history available."


# --- repository session window ------------------------------------------------------ #


def _preport(timestamp: str, spot: float, group: str) -> dict:
    """A minimal pair_reports row — the spot is now read per-fetch from pair_reports, not averaged
    out of option_rows. insert_pair_report defaults the unset columns to None."""
    return {
        "timestamp": timestamp, "fetch_group_id": group, "underlying_asset_id": 13,
        "segment_code": "IDX_I", "expiry": "2026-04-30", "underlying_price": spot,
    }


def test_list_underlying_price_in_range_session_window_excludes_other_days(tmp_path: Path):
    repo = MarketDataRepository(tmp_path / "market.db")
    repo.insert_pair_report(_preport("2026-06-12T14:00:00", 22000.0, "g-prev"))  # previous day — excluded
    repo.insert_pair_report(_preport("2026-06-13T09:15:00", 22500.0, "g-1"))
    repo.insert_pair_report(_preport("2026-06-13T09:20:00", 22510.0, "g-2"))
    repo.insert_pair_report(_preport("2026-06-13T09:25:00", 22520.0, "g-3"))

    series = repo.list_underlying_price_in_range(
        13, "IDX_I", "2026-04-30", since_iso="2026-06-13T00:00:00"
    )

    spots = [round(float(row["underlying_price"]), 1) for row in series]
    # exclusive `since` drops the previous day; session day only, ascending, no cap.
    assert spots == [22500.0, 22510.0, 22520.0]
