from __future__ import annotations

import pytest

from analytics.historical_metrics import (
    WINDOWS,
    build_constant_maturity_atm_iv_series,
    build_historical_metric_payload,
    historical_metric_columns,
    iv_rv_spread,
    iv_window_metrics,
)


# Default expiry is exactly 30 calendar days from the 2026-04-01 test date, so a single
# expiry exact-hits the CM-30 target and the CM IV equals that expiry's bracketing ATM IV —
# letting the bracketing-rule tests below exercise just the per-expiry ATM logic.
def _ctx(date, option_type, strike, iv, spot, expiry="2026-05-01"):
    return {
        "date": date,
        "expiry": expiry,
        "option_type": option_type,
        "strike": strike,
        "iv": iv,
        "underlying_price": spot,
    }


# --- per-expiry bracketing ATM IV (single 30-DTE expiry → CM exact-hit) ------------------ #


def test_bracketing_atm_iv_uses_bracketing_strikes_when_spot_between():
    rows = [
        _ctx("2026-04-01", "CE", 95.0, 10.0, 100.0),
        _ctx("2026-04-01", "CE", 105.0, 20.0, 100.0),
        _ctx("2026-04-01", "PE", 95.0, 30.0, 100.0),
        _ctx("2026-04-01", "PE", 105.0, 40.0, 100.0),
    ]
    series = build_constant_maturity_atm_iv_series(rows)
    assert series["CE"] == [("2026-04-01", 15.0)]  # (10 + 20) / 2
    assert series["PE"] == [("2026-04-01", 35.0)]  # (30 + 40) / 2


def test_bracketing_atm_iv_spot_on_strike_is_directional():
    # Spot sits exactly on 100: call averages 100 with the higher (105); put averages 100 with the lower (95).
    rows = [
        _ctx("2026-04-01", "CE", 95.0, 10.0, 100.0),
        _ctx("2026-04-01", "CE", 100.0, 12.0, 100.0),
        _ctx("2026-04-01", "CE", 105.0, 16.0, 100.0),
        _ctx("2026-04-01", "PE", 95.0, 20.0, 100.0),
        _ctx("2026-04-01", "PE", 100.0, 14.0, 100.0),
        _ctx("2026-04-01", "PE", 105.0, 8.0, 100.0),
    ]
    series = build_constant_maturity_atm_iv_series(rows)
    assert series["CE"] == [("2026-04-01", 14.0)]  # (12 + 16) / 2, higher neighbour
    assert series["PE"] == [("2026-04-01", 17.0)]  # (14 + 20) / 2, lower neighbour


def test_bracketing_atm_iv_one_sided_is_dropped():
    # All strikes above spot: no bracketing pair for either side => no ATM IV for that date.
    rows = [
        _ctx("2026-04-01", "CE", 105.0, 10.0, 100.0),
        _ctx("2026-04-01", "CE", 110.0, 12.0, 100.0),
        _ctx("2026-04-01", "PE", 105.0, 20.0, 100.0),
        _ctx("2026-04-01", "PE", 110.0, 22.0, 100.0),
    ]
    series = build_constant_maturity_atm_iv_series(rows)
    assert series["CE"] == []
    assert series["PE"] == []


# --- constant-maturity variance interpolation across expiries ---------------------------- #


def test_cm_flat_term_structure_returns_the_flat_iv():
    # Two expiries bracketing 30 DTE (15 and 45), both ATM IV 18 → CM-30 = 18 (flat vol).
    rows = [
        _ctx("2026-04-01", "CE", 95.0, 18.0, 100.0, expiry="2026-04-16"),   # DTE 15
        _ctx("2026-04-01", "CE", 105.0, 18.0, 100.0, expiry="2026-04-16"),
        _ctx("2026-04-01", "CE", 95.0, 18.0, 100.0, expiry="2026-05-16"),   # DTE 45
        _ctx("2026-04-01", "CE", 105.0, 18.0, 100.0, expiry="2026-05-16"),
    ]
    series = build_constant_maturity_atm_iv_series(rows)
    assert series["CE"][0][0] == "2026-04-01"
    assert series["CE"][0][1] == pytest.approx(18.0)


def test_cm_interpolates_in_total_variance_between_bracketing_expiries():
    # DTE 15 @ 10% and DTE 45 @ 30%, target 30 is the DTE-midpoint → variance midpoint.
    # var* = (0.10² · 15/365 + 0.30² · 45/365) / 2; cm = sqrt(var* · 365/30)·100 = sqrt(0.07)·100 ≈ 26.46.
    rows = [
        _ctx("2026-04-01", "CE", 95.0, 10.0, 100.0, expiry="2026-04-16"),   # DTE 15
        _ctx("2026-04-01", "CE", 105.0, 10.0, 100.0, expiry="2026-04-16"),
        _ctx("2026-04-01", "CE", 95.0, 30.0, 100.0, expiry="2026-05-16"),   # DTE 45
        _ctx("2026-04-01", "CE", 105.0, 30.0, 100.0, expiry="2026-05-16"),
    ]
    series = build_constant_maturity_atm_iv_series(rows)
    assert series["CE"][0][1] == pytest.approx(26.46, abs=0.05)


def test_cm_dropped_when_no_bracket_around_target():
    # Only expiries below 30 DTE (5 and 12) → no upper bracket → no extrapolation → date dropped.
    rows = [
        _ctx("2026-04-01", "CE", 95.0, 12.0, 100.0, expiry="2026-04-06"),   # DTE 5
        _ctx("2026-04-01", "CE", 105.0, 12.0, 100.0, expiry="2026-04-06"),
        _ctx("2026-04-01", "CE", 95.0, 14.0, 100.0, expiry="2026-04-13"),   # DTE 12
        _ctx("2026-04-01", "CE", 105.0, 14.0, 100.0, expiry="2026-04-13"),
    ]
    series = build_constant_maturity_atm_iv_series(rows)
    assert series["CE"] == []


# --- window metrics / spread / columns (unchanged) -------------------------------------- #


def test_iv_window_metrics_percentile_and_skew_no_rank():
    series = {
        "CE": [("d1", 10.0), ("d2", 12.0), ("d3", 11.0), ("d4", 14.0), ("d5", 13.0)],
        "PE": [("d1", 20.0), ("d2", 22.0), ("d3", 21.0), ("d4", 24.0), ("d5", 18.0)],
    }
    metrics = iv_window_metrics(series, window_days=5)
    assert metrics["call_iv_current"] == 13.0
    assert metrics["call_iv_percentile"] == 60.0  # 3 of 5 below 13
    assert metrics["put_iv_current"] == 18.0
    assert metrics["put_iv_percentile"] == 0.0  # nothing below the minimum
    assert metrics["iv_skew"] == 5.0  # put_current - call_current
    assert "call_iv_rank" not in metrics and "put_iv_rank" not in metrics  # rank removed


def test_iv_window_metrics_skips_side_below_window_length():
    series = {"CE": [("d1", 10.0), ("d2", 11.0)], "PE": [("d1", 20.0), ("d2", 21.0)]}
    metrics = iv_window_metrics(series, window_days=5)
    assert metrics == {}


def test_iv_rv_spread():
    # Spread only — the over/under/fair signal was removed.
    assert iv_rv_spread(130.0, 100.0) == 30.0
    assert iv_rv_spread(80.0, 100.0) == -20.0
    assert iv_rv_spread(None, 100.0) is None
    assert iv_rv_spread(120.0, None) is None
    assert iv_rv_spread(120.0, 0.0) is None


def test_historical_metric_columns_shape():
    columns = historical_metric_columns()
    # 4 all-window REAL families × 4 windows (16) + 2 percentile families × 2 long windows (4) = 20.
    assert len(columns) == 20
    assert all(sql_type == "REAL" for sql_type in columns.values())  # no TEXT/signal columns
    assert columns["rv_22d"] == "REAL"
    assert columns["rv_252d"] == "REAL"
    # percentile only at the long windows
    assert columns["call_iv_percentile_180d"] == "REAL"
    assert columns["call_iv_percentile_252d"] == "REAL"
    assert "call_iv_percentile_22d" not in columns
    assert "put_iv_percentile_90d" not in columns
    # rank and signal removed entirely
    assert "call_iv_rank_252d" not in columns
    assert "call_signal_22d" not in columns
    assert "put_signal_180d" not in columns


def test_build_payload_populates_every_column_with_honest_none():
    # 24 closes → 23 log-returns, enough for the 22d window (needs window+1 = 23 closes); the
    # longer windows (90/180/252) stay short → None. Preserves the "smallest populates, longer
    # are None" intent under the reshaped window set.
    closes = [
        {"underlying_price": price}
        for price in (
            100.0, 104.0, 103.0, 107.0, 106.0, 110.0, 109.0, 113.0,
            112.0, 116.0, 115.0, 119.0, 118.0, 122.0, 121.0, 125.0,
            124.0, 128.0, 127.0, 131.0, 130.0, 134.0, 133.0, 137.0,
        )
    ]
    payload = build_historical_metric_payload(
        date="2026-04-10",
        asset_id=13,
        segment_code="IDX_I",
        daily_closes=closes,
        context_rows=[],
    )
    # 3 key columns (date, asset, segment — no expiry) + 20 metric columns.
    assert len(payload) == 3 + 20
    assert "expiry" not in payload
    assert payload["rv_22d"] is not None  # 24 closes is enough for the 22d window
    assert payload["rv_90d"] is None  # not enough history for longer windows
    assert "call_iv_percentile_22d" not in payload  # percentile only at long windows
    assert "call_iv_rank_22d" not in payload  # rank removed
    assert "call_signal_22d" not in payload  # signal removed
    assert payload["call_iv_percentile_180d"] is None  # long-window column present, no context → None
    assert payload["call_iv_rv_spread_22d"] is None  # no context rows → no IV → no spread


def test_windows_constant_includes_the_one_year_window():
    assert WINDOWS == (22, 90, 180, 252)
