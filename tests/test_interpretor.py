"""Phase 1 tests — the pure interpretor compute (analytics/interpretor.py).

Each signal is exercised in isolation with hand-built snapshots (plain dicts, no wiring). Times are
naive-IST datetimes; the 60 s / 30-min timers are driven by advancing ``now``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from analytics.interpretor import (
    _BIAS_LABELS,
    _SHIFT_LABELS,
    _combine,
    _side_display,
    interpret_pair,
)


CONFIG = {
    "shift_confirm_seconds": 60,
    "bias_suppress_seconds": 1800,
    "hit_tolerance_pct": 0.10,   # ×50 strike-size ⇒ ±5.0 pts band
    "confidence": {
        "primary": {"start": 100.0, "n": 4},          # step 25
        "closer_second": {"start": 50.0, "n": 5},      # step 10
        "further_second": {"start": 100.0, "n": 4},    # step 25
    },
    "oi_floor": 1000.0,
}

T0 = datetime(2026, 7, 20, 10, 0, 0)  # a Monday, 10:00 IST (≥ 09:15)


def _snap(spot, call_oi: dict, put_oi: dict, *, pains: dict | None = None, strike_size: float = 50.0):
    """Build a snapshot. ``call_oi``/``put_oi`` = {strike: oi_lots}; ``pains`` overrides a (strike, side)
    pain point (default = the strike itself)."""
    rows = []
    for strike, oi in call_oi.items():
        rows.append({
            "option_type": "CE", "strike": float(strike), "oi_lots": float(oi),
            "pain_point": float((pains or {}).get((strike, "CE"), strike)),
        })
    for strike, oi in put_oi.items():
        rows.append({
            "option_type": "PE", "strike": float(strike), "oi_lots": float(oi),
            "pain_point": float((pains or {}).get((strike, "PE"), strike)),
        })
    return {"rows": rows, "underlying_price": float(spot), "strike_size": float(strike_size)}


def _level(read_side: dict, role: str) -> dict | None:
    return next((lvl for lvl in read_side["levels"] if lvl["role"] == role), None)


# --- 09:15 gate + baseline ---------------------------------------------------------------------- #


def test_pre_0915_does_not_baseline():
    snap = _snap(22500, {22600: 5000, 22700: 3000}, {22400: 5000})
    state, read = interpret_pair(None, snap, now=datetime(2026, 7, 20, 9, 10), config=CONFIG)
    assert not state.get("baseline_taken")
    assert read["call"]["display"] == "Neutral"
    assert read["instrument"] == {"shift_layer": "Range-Bound", "bias_layer": "No Bias"}


def test_baseline_fixes_primary_no_qualifying_second():
    # call 2nd (3000) is 0.6 of the primary (5000) → below 75% → not a tracked level → no bias.
    snap = _snap(22500, {22600: 5000, 22700: 3000}, {22400: 5000})
    state, read = interpret_pair(None, snap, now=datetime(2026, 7, 20, 9, 16), config=CONFIG)
    assert state["baseline_taken"] is True
    assert state["call"]["primary"] == 22600
    assert read["call"]["shift"] is None
    assert read["call"]["bias"] is None
    assert read["call"]["display"] == "Neutral"


def test_baseline_qualifying_second_sets_bias_immediately():
    # call 2nd 22700 (4000) is 0.8 of primary (5000) → qualifies, above → Bullish Bias at baseline.
    snap = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000})
    _state, read = interpret_pair(None, snap, now=datetime(2026, 7, 20, 9, 16), config=CONFIG)
    assert read["call"]["bias"] == "bullish"
    assert read["call"]["display"] == "Bullish Bias"


def test_degenerate_side_below_floor_reads_neutral():
    snap = _snap(22500, {22600: 500, 22700: 300}, {22400: 5000})  # call OIs below the 1000 floor
    state, read = interpret_pair(None, snap, now=datetime(2026, 7, 20, 9, 16), config=CONFIG)
    assert read["call"]["display"] == "Neutral"
    assert state["call"]["primary"] is None
    assert state["put"]["primary"] == 22400


def test_degenerate_side_rebaselines_fresh_on_return():
    base = _snap(22500, {22600: 5000}, {22400: 5000})
    state, _ = interpret_pair(None, base, now=T0, config=CONFIG)  # call primary 22600
    up = _snap(22500, {22700: 6000, 22600: 5000}, {22400: 5000})
    state, read = _confirm_shift(state, up, start_at=T0 + timedelta(minutes=1))  # bullish, streak 1, primary 22700
    assert read["call"]["shift"] == "bullish" and read["call"]["streak"] == 1
    # Call goes degenerate (all call OI below the 1000 floor) → Neutral this wave + flagged.
    dead = _snap(22500, {22600: 500, 22700: 400}, {22400: 5000})
    state, read = interpret_pair(state, dead, now=T0 + timedelta(minutes=3), config=CONFIG)
    assert read["call"]["display"] == "Neutral"
    assert state["call"]["degenerate"] is True
    # Returns with a NEW highest-OI strike → re-baselined FRESH: no shift, streak 0, primary reset,
    # not a pending shift from the stale 22700.
    back = _snap(22500, {22900: 6000}, {22400: 5000})
    state, read = interpret_pair(state, back, now=T0 + timedelta(minutes=4), config=CONFIG)
    assert read["call"]["shift"] is None
    assert read["call"]["streak"] == 0
    assert read["call"]["pending"] is False
    assert state["call"]["primary"] == 22900


# --- Shift: 60s confirm, latch, flip-back, reversal --------------------------------------------- #


def _confirm_shift(state, snap, *, start_at):
    """Drive a pending → confirmed shift: one pending wave then one wave > 60 s later."""
    state, _ = interpret_pair(state, snap, now=start_at + timedelta(seconds=5), config=CONFIG)
    state, read = interpret_pair(state, snap, now=start_at + timedelta(seconds=70), config=CONFIG)
    return state, read


def test_shift_needs_60s_then_latches_bullish():
    base = _snap(22500, {22600: 5000}, {22400: 5000})
    state, _ = interpret_pair(None, base, now=T0, config=CONFIG)  # primary 22600
    up = _snap(22500, {22700: 6000, 22600: 5000}, {22400: 5000})
    state, read = interpret_pair(state, up, now=T0 + timedelta(seconds=20), config=CONFIG)
    assert read["call"]["shift"] is None and read["call"]["pending"] is True
    state, read = interpret_pair(state, up, now=T0 + timedelta(seconds=85), config=CONFIG)
    assert read["call"]["shift"] == "bullish"
    assert read["call"]["pending"] is False
    assert read["call"]["streak"] == 1
    assert state["call"]["primary"] == 22700


def test_shift_flip_back_before_60s_never_counts():
    base = _snap(22500, {22600: 5000}, {22400: 5000})
    state, _ = interpret_pair(None, base, now=T0, config=CONFIG)
    up = _snap(22500, {22700: 6000, 22600: 5000}, {22400: 5000})
    state, read = interpret_pair(state, up, now=T0 + timedelta(seconds=20), config=CONFIG)
    assert read["call"]["pending"] is True
    state, read = interpret_pair(state, base, now=T0 + timedelta(seconds=40), config=CONFIG)
    assert read["call"]["shift"] is None
    assert read["call"]["pending"] is False
    assert state["call"]["primary"] == 22600


def test_reversal_resets_streak_and_flips_direction():
    base = _snap(22500, {22600: 5000}, {22400: 5000})
    state, _ = interpret_pair(None, base, now=T0, config=CONFIG)
    up1 = _snap(22500, {22700: 6000, 22600: 5000}, {22400: 5000})
    state, read = _confirm_shift(state, up1, start_at=T0 + timedelta(minutes=1))
    assert read["call"]["shift"] == "bullish" and read["call"]["streak"] == 1
    up2 = _snap(22500, {22800: 7000, 22700: 6000}, {22400: 5000})
    state, read = _confirm_shift(state, up2, start_at=T0 + timedelta(minutes=3))
    assert read["call"]["shift"] == "bullish" and read["call"]["streak"] == 2
    down = _snap(22500, {22700: 8000, 22800: 7000}, {22400: 5000})
    state, read = _confirm_shift(state, down, start_at=T0 + timedelta(minutes=5))
    assert read["call"]["shift"] == "bearish"
    assert read["call"]["streak"] == 1  # reversal reset


# --- Proto-shift / bias: appear + 30-min suppression + re-baseline ------------------------------ #


def test_proto_shift_appear_above_sets_bullish_bias_after_60s():
    base = _snap(22500, {22600: 5000, 22700: 2000}, {22400: 5000})  # 2nd not qualifying
    state, read = interpret_pair(None, base, now=T0, config=CONFIG)
    assert read["call"]["bias"] is None
    up = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000})  # 2nd now qualifies, above
    state, read = interpret_pair(state, up, now=T0 + timedelta(seconds=20), config=CONFIG)
    assert read["call"]["bias"] is None  # pending
    state, read = interpret_pair(state, up, now=T0 + timedelta(seconds=90), config=CONFIG)
    assert read["call"]["bias"] == "bullish"


def test_bias_suppressed_30min_after_shift_then_rebaselines():
    base = _snap(22500, {22600: 6000}, {22400: 5000})
    state, _ = interpret_pair(None, base, now=T0, config=CONFIG)  # primary 22600
    up = _snap(22500, {22700: 6000, 22600: 5000}, {22400: 5000})  # demoted 22600 is a ~0.83 qual-2nd below
    state, _ = interpret_pair(state, up, now=T0 + timedelta(seconds=10), config=CONFIG)  # pending
    t_conf = T0 + timedelta(seconds=80)
    state, read = interpret_pair(state, up, now=t_conf, config=CONFIG)  # confirm bullish shift
    assert read["call"]["shift"] == "bullish"
    # within the window: the below qual-2nd would say bearish, but bias is suppressed → None
    state, read = interpret_pair(state, up, now=t_conf + timedelta(minutes=5), config=CONFIG)
    assert read["call"]["bias"] is None
    # after 30 min: re-baseline from the current qual-2nd (22600 below) → bearish
    state, read = interpret_pair(state, up, now=t_conf + timedelta(minutes=31), config=CONFIG)
    assert read["call"]["bias"] == "bearish"


# --- Hits + confidence + opposite-side hysteresis ----------------------------------------------- #


def test_first_hit_counts_and_erodes_primary_confidence():
    snap = _snap(22600, {22600: 5000}, {22400: 5000})  # spot AT the call primary's pain point
    _state, read = interpret_pair(None, snap, now=T0, config=CONFIG)
    primary = _level(read["call"], "primary")
    assert primary["hits"] == 1
    assert primary["confidence"] == 75.0  # 100 − 25


def test_repeat_hit_requires_opposite_side_touch():
    at_call = _snap(22600, {22600: 5000}, {22400: 5000})
    state, _ = interpret_pair(None, at_call, now=T0, config=CONFIG)  # hit #1 (armed→disarmed)
    state, read = interpret_pair(state, at_call, now=T0 + timedelta(seconds=5), config=CONFIG)
    assert _level(read["call"], "primary")["hits"] == 1  # no opposite touch → not a new hit
    at_put = _snap(22400, {22600: 5000}, {22400: 5000})   # touch the put wall → re-arms the call side
    state, _ = interpret_pair(state, at_put, now=T0 + timedelta(seconds=10), config=CONFIG)
    state, read = interpret_pair(state, at_call, now=T0 + timedelta(seconds=15), config=CONFIG)
    primary = _level(read["call"], "primary")
    assert primary["hits"] == 2
    assert primary["confidence"] == 50.0


def test_closer_second_starts_at_50_with_10_step():
    # spot 22550: primary 22700 (far), 2nd 22550 (0.8 qualifies, closer to spot) → closer_second.
    snap = _snap(22550, {22700: 5000, 22550: 4000}, {22300: 5000})
    _state, read = interpret_pair(None, snap, now=T0, config=CONFIG)
    second = _level(read["call"], "second")
    assert second is not None and second["strike"] == 22550
    assert second["hits"] == 1              # spot at its pain
    assert second["confidence"] == 40.0     # 50 − 10


def test_colocated_hit_attributed_to_call_when_rising():
    pains = {(22500, "CE"): 22502, (22500, "PE"): 22498}
    snap_low = _snap(22490, {22500: 5000}, {22500: 5000}, pains=pains)   # below both bands (sets prev_spot)
    snap_hit = _snap(22500, {22500: 5000}, {22500: 5000}, pains=pains)   # in both bands
    state, _ = interpret_pair(None, snap_low, now=T0, config=CONFIG)
    _state, read = interpret_pair(state, snap_hit, now=T0 + timedelta(seconds=5), config=CONFIG)
    assert _level(read["call"], "primary")["hits"] == 1  # rising → call keeps the co-located hit
    assert _level(read["put"], "primary")["hits"] == 0


def test_colocated_hit_attributed_to_put_when_falling():
    pains = {(22500, "CE"): 22502, (22500, "PE"): 22498}
    snap_high = _snap(22510, {22500: 5000}, {22500: 5000}, pains=pains)  # above both bands
    snap_hit = _snap(22500, {22500: 5000}, {22500: 5000}, pains=pains)
    state, _ = interpret_pair(None, snap_high, now=T0, config=CONFIG)
    _state, read = interpret_pair(state, snap_hit, now=T0 + timedelta(seconds=5), config=CONFIG)
    assert _level(read["put"], "primary")["hits"] == 1   # falling → put keeps it
    assert _level(read["call"], "primary")["hits"] == 0


def test_colocated_stationary_and_first_reading_credit_neither():
    pains = {(22500, "CE"): 22502, (22500, "PE"): 22498}
    snap = _snap(22500, {22500: 5000}, {22500: 5000}, pains=pains)  # spot in BOTH bands
    # First wave (prev_spot is None → no approach) → neither side credited.
    state, read = interpret_pair(None, snap, now=T0, config=CONFIG)
    assert _level(read["call"], "primary")["hits"] == 0
    assert _level(read["put"], "primary")["hits"] == 0
    # Second wave, same spot (approach == 0, stationary) → still neither.
    _state, read = interpret_pair(state, snap, now=T0 + timedelta(seconds=5), config=CONFIG)
    assert _level(read["call"], "primary")["hits"] == 0
    assert _level(read["put"], "primary")["hits"] == 0


# --- Combine tables + per-side display ---------------------------------------------------------- #


def test_combine_shift_layer():
    assert _combine("bullish", "bullish", _SHIFT_LABELS) == "Strong Bullish"
    assert _combine("bearish", "bearish", _SHIFT_LABELS) == "Strong Bearish"
    assert _combine("bullish", "bearish", _SHIFT_LABELS) == "Undecided"
    assert _combine("bullish", None, _SHIFT_LABELS) == "Bullish"
    assert _combine(None, "bearish", _SHIFT_LABELS) == "Bearish"
    assert _combine(None, None, _SHIFT_LABELS) == "Range-Bound"


def test_combine_bias_layer():
    assert _combine("bullish", "bullish", _BIAS_LABELS) == "Strong Bullish Bias"
    assert _combine("bearish", "bullish", _BIAS_LABELS) == "No Clear Bias"
    assert _combine("bullish", None, _BIAS_LABELS) == "Bullish Bias"
    assert _combine(None, None, _BIAS_LABELS) == "No Bias"


def test_side_display_agreement_and_conflict():
    assert _side_display(None, None) == "Neutral"
    assert _side_display(None, "bullish") == "Bullish Bias"
    assert _side_display("bearish", None) == "Bearish"
    assert _side_display("bullish", "bullish") == "Bullish"
    assert _side_display("bullish", "bearish") == "Bullish shift, Bearish bias"


# --- 09:20 stamp + serializability -------------------------------------------------------------- #


def test_0920_stamp_recorded_once_inside_the_window():
    base = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000})
    state, read = interpret_pair(None, base, now=datetime(2026, 7, 20, 9, 17), config=CONFIG)  # before the window
    assert read["stamp_0920"] is None  # before 09:20 → nothing yet
    at = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000})
    state, read = interpret_pair(state, at, now=datetime(2026, 7, 20, 9, 20, 30), config=CONFIG)  # inside [09:20, 09:21)
    assert read["stamp_0920"]["call"] == {"primary": 22600, "second": 22700}
    snap2 = _snap(22500, {22800: 9000}, {22400: 5000})
    _state, read = interpret_pair(state, snap2, now=datetime(2026, 7, 20, 9, 30), config=CONFIG)
    assert read["stamp_0920"]["call"]["primary"] == 22600  # frozen — recorded once


def test_no_stamp_before_0920():
    snap = _snap(22500, {22600: 5000}, {22400: 5000})
    _state, read = interpret_pair(None, snap, now=datetime(2026, 7, 20, 9, 17), config=CONFIG)
    assert read["stamp_0920"] is None


def test_no_stamp_when_the_window_is_missed():
    # Baselined before 09:20 but no fetch lands in [09:20, 09:21) — a gap over that minute, next fetch at
    # 09:25. The stamp must stay absent, never back-filled from the later chain.
    base = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000})
    state, read = interpret_pair(None, base, now=datetime(2026, 7, 20, 9, 16), config=CONFIG)  # before the window
    assert read["stamp_0920"] is None
    after = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000})
    state, read = interpret_pair(state, after, now=datetime(2026, 7, 20, 9, 25), config=CONFIG)  # after the window
    assert read["stamp_0920"] is None
    later = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000})
    _state, read = interpret_pair(state, later, now=datetime(2026, 7, 20, 10, 0), config=CONFIG)
    assert read["stamp_0920"] is None  # still nothing, later in the session too


def test_0920_stamp_records_when_first_fetch_is_inside_the_window():
    # A pair whose very first fetch lands in [09:20, 09:21) is stamped — no pre-window sighting needed.
    snap = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000})
    _state, read = interpret_pair(None, snap, now=datetime(2026, 7, 20, 9, 20, 45), config=CONFIG)
    assert read["stamp_0920"]["call"] == {"primary": 22600, "second": 22700}


def test_state_is_json_serializable():
    snap = _snap(22500, {22600: 5000, 22700: 4000}, {22400: 5000, 22300: 4000})
    state, _ = interpret_pair(None, snap, now=T0, config=CONFIG)
    json.dumps(state)  # must not raise
