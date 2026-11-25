from __future__ import annotations

from datetime import date, datetime, timedelta

from analytics.required_move import (
    SIGMA_CAP,
    SIGMA_FLOOR,
    _clamp_sigma,
    build_required_move_curve,
)

# Monday 22 Jun 2026, 11:00 IST (naive == IST by convention).
_NOW = datetime(2026, 6, 22, 11, 0)


def _curve(
    *,
    option_type: str = "CE",
    expiry: str = "2026-06-30",
    pain_point: float = 22520.0,
    call_pain_point=None,
    put_pain_point=None,
    call_2nd_pain_point=None,
    put_2nd_pain_point=None,
    ltp_series=None,
    holidays=None,
    now: datetime = _NOW,
):
    return build_required_move_curve(
        strike=22500.0,
        option_type=option_type,
        iv=14.0,
        rate=0.065,
        expiry=expiry,
        segment="IDX_I",
        pain_point=pain_point,
        call_pain_point=call_pain_point,
        put_pain_point=put_pain_point,
        call_2nd_pain_point=call_2nd_pain_point,
        put_2nd_pain_point=put_2nd_pain_point,
        ltp_series=ltp_series,
        holidays=holidays,
        now=now,
    )


def _all_today(xs) -> bool:
    return all(str(x).startswith("2026-06-22T") for x in xs)


# --- Today-session datetime axis (intraday-only; no swing, no rangebreaks) ---


def test_today_session_datetime_axis_and_decay():
    curve = _curve()
    assert curve["ok"] is True
    assert curve["x_is_datetime"] is True
    assert curve["tickformat"] == "%H:%M"
    assert curve["x_title"] == "Time (IST)"
    # A multi-day expiry, but the reversal is drawn over TODAY only, to the true 3:30 close.
    assert _all_today(curve["reversal_x"])
    assert curve["reversal_x"][-1] == "2026-06-22T15:30:00"
    rev_y = curve["reversal_y"]
    assert rev_y[0] > rev_y[-1]  # decays through the day (continuous hours-to-expiry TTE)
    assert all(rev_y[i] >= rev_y[i + 1] - 1e-6 for i in range(len(rev_y) - 1))
    # The removed swing/rangebreak axis spec is gone.
    assert "rangebreaks" not in curve and "tickvals" not in curve and "reversed_axis" not in curve


def test_reversal_extends_to_close_without_ltp():
    curve = _curve()  # no LTP → reversal is the now→close continuation
    assert curve["ltp_x"] == []
    assert curve["reversal_x"][0] == "2026-06-22T11:00:00"  # starts at now
    assert curve["reversal_x"][-1] == "2026-06-22T15:30:00"  # to today's 3:30 close


def test_reversal_uses_ltp_timestamps_and_continues_to_close():
    ltp = [
        {"timestamp": "2026-06-22T10:00:05", "value": 100.0},
        {"timestamp": "2026-06-22T10:30:00", "value": 101.0},
    ]
    curve = _curve(ltp_series=ltp)
    # Historical reversal shares the LTP x-grid…
    assert "2026-06-22T10:00:05" in curve["reversal_x"]
    assert "2026-06-22T10:30:00" in curve["reversal_x"]
    # …and the line continues to today's close.
    assert curve["reversal_x"][-1] == "2026-06-22T15:30:00"
    assert _all_today(curve["reversal_x"])


def test_ltp_filtered_to_today_second_resolution():
    ltp = [
        {"timestamp": "2026-06-19T15:00:00", "value": 90.0},   # Friday — not today, dropped
        {"timestamp": "2026-06-22T10:00:05", "value": 100.0},
        {"timestamp": "2026-06-22T10:00:20", "value": 101.0},  # same minute, different second
    ]
    curve = _curve(ltp_series=ltp)
    assert curve["ltp_x"] == ["2026-06-22T10:00:05", "2026-06-22T10:00:20"]  # seconds kept, distinct
    assert curve["ltp_y"] == [100.0, 101.0]


# --- Rolling 60-minute default window (no centering, permanent 5-min right pad) ---


def test_window_first_point_at_left_edge_early_session():
    now = datetime(2026, 6, 22, 9, 45)
    ltp = [
        {"timestamp": "2026-06-22T09:30:00", "value": 100.0},
        {"timestamp": "2026-06-22T09:40:00", "value": 101.0},
    ]
    lo, hi = _curve(ltp_series=ltp, now=now)["x_range"]
    assert lo == "2026-06-22T09:30:00"  # first data point sits AT the left edge (no centering)
    assert datetime.fromisoformat(hi) - datetime.fromisoformat(lo) == timedelta(minutes=60)


def test_window_rolls_to_now_late_session():
    now = datetime(2026, 6, 22, 14, 0)
    ltp = [{"timestamp": "2026-06-22T09:30:00", "value": 100.0}]
    lo, hi = _curve(ltp_series=ltp, now=now)["x_range"]
    assert hi == "2026-06-22T14:05:00"  # right edge = now + 5-min permanent pad (rolled)
    assert lo == "2026-06-22T13:05:00"  # 60 min back


# --- 0-DTE + closed/holiday guards ---


def test_0dte_runs_to_close():
    curve = _curve(expiry="2026-06-22")
    assert curve["ok"] is True
    assert curve["reversal_x"][0] == "2026-06-22T11:00:00"   # now
    assert curve["reversal_x"][-1] == "2026-06-22T15:30:00"  # true 3:30 close


def test_after_close_not_ok():
    assert _curve(now=datetime(2026, 6, 22, 16, 0))["ok"] is False


def test_holiday_today_not_ok():
    assert _curve(holidays=frozenset({date(2026, 6, 22)}))["ok"] is False


# --- Extra wall reversal lines (call/put OI pain points) + self-dedup ---


def test_wall_lines_drawn_when_distinct():
    curve = _curve(pain_point=22520.0, call_pain_point=22700.0, put_pain_point=22300.0)
    assert curve["draw_self"] is True
    # Both extra lines share the self reversal x-grid and are fully populated.
    assert len(curve["call_reversal_y"]) == len(curve["reversal_x"])
    assert len(curve["put_reversal_y"]) == len(curve["reversal_x"])
    # Different spot → different prices from the self line.
    assert curve["call_reversal_y"] != curve["reversal_y"]
    assert curve["put_reversal_y"] != curve["reversal_y"]
    assert curve["call_pain_point"] == 22700.0
    assert curve["put_pain_point"] == 22300.0


def test_self_line_suppressed_when_equals_call_pain():
    # Selected contract IS the highest-OI call → self pain == call pain → don't draw the self line.
    curve = _curve(pain_point=22700.0, call_pain_point=22700.0, put_pain_point=22300.0)
    assert curve["draw_self"] is False
    assert curve["call_reversal_y"] == curve["reversal_y"]  # the call line carries the same values
    assert curve["put_reversal_y"]  # the put line is still drawn


def test_self_line_suppressed_when_equals_put_pain():
    curve = _curve(pain_point=22300.0, call_pain_point=22700.0, put_pain_point=22300.0)
    assert curve["draw_self"] is False
    assert curve["put_reversal_y"] == curve["reversal_y"]


def test_wall_lines_absent_without_pain_points():
    curve = _curve()  # no wall pain points supplied
    assert curve["call_reversal_y"] == []
    assert curve["put_reversal_y"] == []
    assert curve["draw_self"] is True
    # Non-positive wall pain points are ignored too.
    zeroed = _curve(call_pain_point=0.0, put_pain_point=None)
    assert zeroed["call_reversal_y"] == [] and zeroed["call_pain_point"] is None


# --- 2nd-highest-OI wall reversal lines (same contract, spot at the 2nd walls' pain points) ---


def test_second_wall_lines_drawn_when_supplied():
    curve = _curve(
        pain_point=22520.0, call_pain_point=22700.0, put_pain_point=22300.0,
        call_2nd_pain_point=22650.0, put_2nd_pain_point=22350.0,
    )
    # Both 2nd lines share the reversal x-grid, are fully populated, and differ from the self line.
    assert len(curve["call_2nd_reversal_y"]) == len(curve["reversal_x"])
    assert len(curve["put_2nd_reversal_y"]) == len(curve["reversal_x"])
    assert curve["call_2nd_reversal_y"] != curve["reversal_y"]
    assert curve["put_2nd_reversal_y"] != curve["reversal_y"]
    assert curve["call_2nd_pain_point"] == 22650.0 and curve["put_2nd_pain_point"] == 22350.0
    assert curve["draw_self"] is True


def test_second_wall_lines_absent_when_not_supplied_or_non_positive():
    curve = _curve(call_pain_point=22700.0, put_pain_point=22300.0)  # only the primary walls
    assert curve["call_2nd_reversal_y"] == [] and curve["put_2nd_reversal_y"] == []
    assert curve["call_2nd_pain_point"] is None and curve["put_2nd_pain_point"] is None
    zeroed = _curve(call_2nd_pain_point=0.0, put_2nd_pain_point=None)
    assert zeroed["call_2nd_reversal_y"] == [] and zeroed["call_2nd_pain_point"] is None


def test_self_line_suppressed_when_equals_a_second_wall():
    # Selected contract IS the 2nd-highest-OI call → self pain == call 2nd pain → suppress the self line.
    curve = _curve(pain_point=22650.0, call_pain_point=22700.0, call_2nd_pain_point=22650.0)
    assert curve["draw_self"] is False
    assert curve["call_2nd_reversal_y"] == curve["reversal_y"]  # the 2nd line carries the same values


# --- Readout + validation + clamp ---


def test_readout_has_pain_now_and_close():
    ro = _curve()["readout"]
    assert ro["pain_point"] == 22520.0
    assert ro["reversal_now"] is not None and ro["reversal_close"] is not None


def test_curve_rejects_invalid_inputs():
    assert _curve(pain_point=0.0)["ok"] is False
    assert build_required_move_curve(
        strike=22500, option_type="CE", iv=0.0, rate=0.065,
        expiry="2026-06-30", segment="IDX_I", pain_point=22520, now=_NOW,
    )["ok"] is False
    assert build_required_move_curve(
        strike=22500, option_type="CE", iv=14.0, rate=None,
        expiry="2026-06-30", segment="IDX_I", pain_point=22520, now=_NOW,
    )["ok"] is False


def test_sigma_clamp():
    assert _clamp_sigma(0.0) == SIGMA_FLOOR
    assert _clamp_sigma(50.0) == SIGMA_CAP
    assert _clamp_sigma(0.2) == 0.2
