"""Intraday OI-structure directional interpretor — pure compute, no I/O.

Reads one option chain's **open-interest structure** and, wave over wave, synthesises a **categorical**
directional read: per side (call / put) a `(shift, bias)` pair, and a two-layer instrument read (a Shift
layer + a Bias layer, always shown together). It also tracks, as a **separate** standalone concern, each
tracked level's **hit count + confidence %**. Everything is heuristic + transparent; it never predicts
price from price.

This is a **per-pair pure function** over `(prev_state, snapshot, now, config)`:
- `snapshot` = `{"rows": [...], "underlying_price": float, "strike_size": float}` (the full chain — the
  interpretor derives its OWN levels from `oi_lots`, never the app's `report["levels"]`).
- `config` = the interpretor tunables dict (from `config.json` `interpretor` block) **plus** an
  `"oi_floor"` (reused from `rate_alert_floors.oi`). The module imports nothing from `config` — the
  caller (the collector) assembles the dict; tests pass their own.
- returns `(next_state, read)` — `next_state` is JSON-serializable (lives in `interpretor.json`);
  `read` is what the display consumes.

**No I/O, no `sqlite3`, no imports from `features/` or `config`** — same layer discipline as
`analytics/alerts.py` / `analytics/rate_alerts.py`.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from math import isnan
from typing import Any, Mapping

# The first fetch at/after 09:15 IST fixes each side's baseline. The static 09:20 readout is stamped only
# from a fetch that lands in the window [09:20:00, 09:21:00) — 09:20 means 09:20; if no
# fetch lands in that minute it is absent. The streak system is otherwise start-agnostic.
SESSION_GATE = dtime(9, 15)
STAMP_TIME = dtime(9, 20)
STAMP_END = dtime(9, 21)
# A 2nd strike is a tracked level only while its OI ≥ this fraction of its side's primary OI.
QUAL_2ND_RATIO = 0.75

_BULLISH = "bullish"
_BEARISH = "bearish"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if isnan(result) else result


def _elapsed_seconds(since_iso: str | None, now: datetime) -> float | None:
    """Seconds between a stored naive-IST ISO timestamp and ``now`` (also naive IST), or ``None``."""
    if not since_iso:
        return None
    try:
        since = datetime.fromisoformat(str(since_iso))
    except ValueError:
        return None
    return (now - since).total_seconds()


def _side_map(rows, option_type: str) -> dict[float, dict[str, float | None]]:
    """``{strike: {"oi": oi_lots, "pain": pain_point}}`` for one side."""
    out: dict[float, dict[str, float | None]] = {}
    for row in rows or []:
        if row.get("option_type") != option_type:
            continue
        strike = _as_float(row.get("strike"))
        if strike is None:
            continue
        out[strike] = {"oi": _as_float(row.get("oi_lots")) or 0.0, "pain": _as_float(row.get("pain_point"))}
    return out


def _observe_levels(side_map: dict[float, dict], spot: float, oi_floor: float) -> dict | None:
    """The observed primary + qualifying-2nd for one side this wave, or ``None`` (degenerate side).

    Primary = highest-OI strike (tie-break: nearest to spot). ``None`` when the side has no strikes or
    its primary OI is below ``oi_floor`` (degenerate — treated as Neutral, no tracking). ``second`` is
    the 2nd-highest OI strike; ``second_qualifies`` when its OI ≥ 75 % of the primary's.
    """
    if not side_map:
        return None
    oi_by = {strike: (info["oi"] or 0.0) for strike, info in side_map.items()}
    ranked = sorted(oi_by, key=lambda s: (-oi_by[s], abs(s - spot)))
    primary = ranked[0]
    if oi_by[primary] < oi_floor:
        return None
    result: dict[str, Any] = {
        "primary": primary,
        "primary_oi": oi_by[primary],
        "second": None,
        "second_qualifies": False,
    }
    if len(ranked) >= 2:
        second = ranked[1]
        result["second"] = second
        result["second_qualifies"] = oi_by[second] >= QUAL_2ND_RATIO * oi_by[primary]
    return result


def _qual2_position(observed: dict, primary_strike: float) -> str:
    """The qualifying-2nd's position vs the confirmed primary: ``"above"`` / ``"below"`` / ``"absent"``."""
    if not observed.get("second_qualifies") or observed.get("second") is None:
        return "absent"
    second = observed["second"]
    if second > primary_strike:
        return "above"
    if second < primary_strike:
        return "below"
    return "absent"  # exactly on the primary (can't happen — strikes are unique) → not a proto_shift


# --------------------------------------------------------------------------- #
# Confidence-level slots (the hits/confidence track)
# --------------------------------------------------------------------------- #

def _confidence_start_step(position: str, config: Mapping[str, Any]) -> tuple[float, float]:
    band = config["confidence"][position]
    start = float(band["start"])
    n = max(1, int(band["n"]))
    return start, start / n


def _fresh_level(strike: float, position: str, config: Mapping[str, Any]) -> dict:
    """A reset level slot — full confidence, zero hits, armed (its first hit always counts)."""
    start, _step = _confidence_start_step(position, config)
    return {"strike": strike, "position": position, "confidence": start, "hits": 0, "armed": True}


def _classify_second(second_strike: float, primary_strike: float, spot: float) -> str:
    """A 2nd's position class (drives its confidence start/N), re-evaluated at reset time:
    closer to spot than the primary → ``closer_second``; else ``further_second``."""
    return "closer_second" if abs(second_strike - spot) < abs(primary_strike - spot) else "further_second"


def _reconcile_levels(
    side_state: dict, primary_strike: float, second_strike: float | None, spot: float, config: Mapping[str, Any]
) -> None:
    """Keep each level slot when its strike is unchanged; reset it (fresh confidence, hits→0) on a
    strike change; drop the 2nd slot when there's no qualifying 2nd (reset-on-strike-change)."""
    levels = side_state.setdefault("levels", {"primary": None, "second": None})

    primary_slot = levels.get("primary")
    if primary_slot is None or primary_slot["strike"] != primary_strike:
        levels["primary"] = _fresh_level(primary_strike, "primary", config)

    if second_strike is None:
        levels["second"] = None
    else:
        second_slot = levels.get("second")
        if second_slot is None or second_slot["strike"] != second_strike:
            position = _classify_second(second_strike, primary_strike, spot)
            levels["second"] = _fresh_level(second_strike, position, config)


# --------------------------------------------------------------------------- #
# Shift machine — primary migration, 60s confirm, latch, streak
# --------------------------------------------------------------------------- #

def _update_shift(side_state: dict, observed_primary: float, now: datetime, config: Mapping[str, Any]) -> bool:
    """Advance one side's shift machine against this wave's observed primary. Returns the **pending**
    flag (a new top is awaiting its 60s confirmation). On a confirmed shift it latches the direction,
    updates the streak (reset on reversal), stamps the last-confirm time (the 30-min bias window), and
    moves the confirmed ``primary``."""
    confirm_seconds = float(config["shift_confirm_seconds"])
    confirmed_primary = side_state["primary"]

    if observed_primary == confirmed_primary:
        side_state["pending_top"] = None
        side_state["pending_since"] = None
        return False

    # A different strike is on top — start/hold the confirmation clock.
    if side_state.get("pending_top") != observed_primary:
        side_state["pending_top"] = observed_primary
        side_state["pending_since"] = now.isoformat()
        return True

    elapsed = _elapsed_seconds(side_state.get("pending_since"), now)
    if elapsed is None or elapsed <= confirm_seconds:
        return True  # still pending — keep the existing (unchanged) state

    # ---- Confirm the shift ----
    direction = _BULLISH if observed_primary > confirmed_primary else _BEARISH
    prev_direction = side_state.get("shift_dir")
    if prev_direction is None:
        side_state["streak"] = 1
    elif direction == prev_direction:
        side_state["streak"] = int(side_state.get("streak", 0)) + 1
    else:
        side_state["streak"] = 1  # reversal resets the streak
        # A reversal re-arms this side's levels so re-tests of the new levels can re-trigger.
        for _role in ("primary", "second"):
            _slot = side_state.get("levels", {}).get(_role)
            if _slot:
                _slot["armed"] = True
    side_state["shift_dir"] = direction
    side_state["primary"] = observed_primary
    side_state["last_shift_confirm_ts"] = now.isoformat()
    side_state["pending_top"] = None
    side_state["pending_since"] = None
    return False


# --------------------------------------------------------------------------- #
# Proto-shift / bias machine — qualifying-2nd above/below, 60s, 30-min suppression
# --------------------------------------------------------------------------- #

def _bias_from_transition(old: str, new: str) -> str | None:
    """The latched bias set by a confirmed qual2 transition ``old`` → ``new``:
    appearing above → bullish / below → bearish; disappearing flips (above→bearish, below→bullish);
    a side-flip is disappear-old + appear-new (net = the new side). ``None`` when nothing changes."""
    if old == new:
        return None
    if new == "above":            # appear-above (or flip below→above)
        return _BULLISH
    if new == "below":            # appear-below (or flip above→below)
        return _BEARISH
    # new == "absent" → the qual2 disappeared; the sign flips off its old side.
    if old == "above":
        return _BEARISH
    if old == "below":
        return _BULLISH
    return None


def _update_bias(side_state: dict, observed_pos: str, now: datetime, config: Mapping[str, Any]) -> None:
    """Advance one side's proto-shift/bias machine. Suppressed for ``bias_suppress_seconds`` after any
    confirmed shift (bias frozen; hits/confidence untouched — handled elsewhere); on the suppression
    lift it **re-baselines** the bias from the current qual2. Otherwise event-driven with the
    same 60s confirmation on appear/disappear/flip."""
    suppress_seconds = float(config["bias_suppress_seconds"])
    confirm_seconds = float(config["shift_confirm_seconds"])

    since_shift = _elapsed_seconds(side_state.get("last_shift_confirm_ts"), now)
    suppressed_now = since_shift is not None and since_shift < suppress_seconds

    if suppressed_now:
        # Freeze the machine while suppressed; the OUTPUT bias-state is silenced downstream.
        side_state["pending_qual2"] = None
        side_state["pending_qual2_since"] = None
        side_state["suppressed_prev"] = True
        return

    if side_state.get("suppressed_prev"):
        # First wave after the 30-min window: re-baseline the bias from the current qual2.
        side_state["qual2_state"] = observed_pos
        side_state["bias"] = None if observed_pos == "absent" else (_BULLISH if observed_pos == "above" else _BEARISH)
        side_state["pending_qual2"] = None
        side_state["pending_qual2_since"] = None
        side_state["suppressed_prev"] = False
        return

    committed_pos = side_state.get("qual2_state", "absent")
    if observed_pos == committed_pos:
        side_state["pending_qual2"] = None
        side_state["pending_qual2_since"] = None
        return

    # A changed qual2 position — hold it for >60s before committing the transition.
    if side_state.get("pending_qual2") != observed_pos:
        side_state["pending_qual2"] = observed_pos
        side_state["pending_qual2_since"] = now.isoformat()
        return
    elapsed = _elapsed_seconds(side_state.get("pending_qual2_since"), now)
    if elapsed is None or elapsed <= confirm_seconds:
        return

    new_bias = _bias_from_transition(committed_pos, observed_pos)
    if new_bias is not None:
        side_state["bias"] = new_bias
    elif observed_pos == "absent":
        side_state["bias"] = None
    side_state["qual2_state"] = observed_pos
    side_state["pending_qual2"] = None
    side_state["pending_qual2_since"] = None


def _bias_output(side_state: dict, now: datetime, config: Mapping[str, Any]) -> str | None:
    """The bias-state to SHOW — ``None`` while within ``bias_suppress_seconds`` of a confirmed shift."""
    since_shift = _elapsed_seconds(side_state.get("last_shift_confirm_ts"), now)
    if since_shift is not None and since_shift < float(config["bias_suppress_seconds"]):
        return None
    return side_state.get("bias")


# --------------------------------------------------------------------------- #
# Hits + confidence erosion — a separate, standalone track
# --------------------------------------------------------------------------- #

def _touched(level_slot: dict | None, side_map: dict[float, dict], spot: float, tolerance: float) -> bool:
    """Is spot within ``tolerance`` of this level's CURRENT pain point? (pain re-read each wave.)"""
    if not level_slot:
        return False
    info = side_map.get(level_slot["strike"])
    pain = (info or {}).get("pain") if info else None
    pain = _as_float(pain)
    if pain is None or pain <= 0:
        return False
    return abs(spot - pain) <= tolerance


def _apply_hits(
    call_state: dict, put_state: dict, call_map: dict, put_map: dict,
    spot: float, prev_spot: float | None, tolerance: float, config: Mapping[str, Any],
) -> None:
    """Register hits + erode confidence for this wave.

    A touch = spot within ±tolerance of a tracked level's pain point. Co-located levels (same strike
    tracked on both sides, both touched) are attributed by approach direction (rising→call, falling→put);
    a stationary reading or the first wave (no previous spot) has no approach, so it is credited to
    **neither** side (it can't be a genuine two-sided re-test and must not self-satisfy the opposite-side
    rule). A touched level counts a hit only when **armed** (armed after a strike-reset or an
    opposite-side touch; disarmed after a counted hit — the opposite-side-travel anti-jitter rule). Each
    counted hit subtracts the level's step, floored at 0."""

    def _side_touches(side_state, side_map) -> list[tuple[str, dict]]:
        hits = []
        for role in ("primary", "second"):
            slot = side_state["levels"].get(role)
            if _touched(slot, side_map, spot, tolerance):
                hits.append((role, slot))
        return hits

    call_touches = _side_touches(call_state, call_map)
    put_touches = _side_touches(put_state, put_map)

    # Co-located attribution: a strike touched on BOTH sides is credited by approach direction —
    # rising → the call side (testing resistance), falling → the put side. A stationary reading or the
    # first wave (no previous spot) has no approach → credited to NEITHER (can't self-satisfy the
    # opposite-side rule with no real price travel).
    call_strikes = {slot["strike"] for _role, slot in call_touches}
    shared = call_strikes & {slot["strike"] for _role, slot in put_touches}
    if shared:
        approach = None if prev_spot is None else (spot - prev_spot)
        if approach is not None and approach > 0:      # rising → call keeps
            put_touches = [t for t in put_touches if t[1]["strike"] not in shared]
        elif approach is not None and approach < 0:    # falling → put keeps
            call_touches = [t for t in call_touches if t[1]["strike"] not in shared]
        else:                                          # stationary / first reading → credit neither
            call_touches = [t for t in call_touches if t[1]["strike"] not in shared]
            put_touches = [t for t in put_touches if t[1]["strike"] not in shared]

    call_touched_any = bool(call_touches)
    put_touched_any = bool(put_touches)

    def _count(side_state, touches):
        for _role, slot in touches:
            if not slot.get("armed"):
                continue
            _start, step = _confidence_start_step(slot["position"], config)
            slot["confidence"] = max(0.0, float(slot["confidence"]) - step)
            slot["hits"] = int(slot["hits"]) + 1
            slot["armed"] = False

    _count(call_state, call_touches)
    _count(put_state, put_touches)

    # Re-arm from an opposite-side touch this wave (takes effect for subsequent waves).
    if put_touched_any:
        for role in ("primary", "second"):
            if call_state["levels"].get(role):
                call_state["levels"][role]["armed"] = True
    if call_touched_any:
        for role in ("primary", "second"):
            if put_state["levels"].get(role):
                put_state["levels"][role]["armed"] = True


# --------------------------------------------------------------------------- #
# Per-side + instrument display
# --------------------------------------------------------------------------- #

def _side_display(shift: str | None, bias: str | None) -> str:
    if shift is None and bias is None:
        return "Neutral"
    if shift is None:
        return "Bullish Bias" if bias == _BULLISH else "Bearish Bias"
    if bias is None or bias == shift:
        return "Bullish" if shift == _BULLISH else "Bearish"
    # conflict — show both
    return f"{'Bullish' if shift == _BULLISH else 'Bearish'} shift, {'Bullish' if bias == _BULLISH else 'Bearish'} bias"


def _combine(call: str | None, put: str | None, labels: Mapping[str, str]) -> str:
    """Symmetric two-side combine. ``labels`` keys: ``strong_bull``, ``strong_bear``, ``bull``,
    ``bear``, ``conflict``, ``neither``."""
    if call == _BULLISH and put == _BULLISH:
        return labels["strong_bull"]
    if call == _BEARISH and put == _BEARISH:
        return labels["strong_bear"]
    if (call == _BULLISH and put == _BEARISH) or (call == _BEARISH and put == _BULLISH):
        return labels["conflict"]
    directional = call if call is not None else put  # the one directional side (other is None)
    if directional == _BULLISH:
        return labels["bull"]
    if directional == _BEARISH:
        return labels["bear"]
    return labels["neither"]


_SHIFT_LABELS = {
    "strong_bull": "Strong Bullish", "strong_bear": "Strong Bearish",
    "bull": "Bullish", "bear": "Bearish", "conflict": "Undecided", "neither": "Range-Bound",
}
_BIAS_LABELS = {
    "strong_bull": "Strong Bullish Bias", "strong_bear": "Strong Bearish Bias",
    "bull": "Bullish Bias", "bear": "Bearish Bias", "conflict": "No Clear Bias", "neither": "No Bias",
}


def _level_readout(side_state: dict) -> list[dict]:
    out: list[dict] = []
    for role in ("primary", "second"):
        slot = side_state["levels"].get(role)
        if slot:
            out.append({
                "role": role,
                "strike": slot["strike"],
                "confidence": round(float(slot["confidence"]), 1),
                "hits": int(slot["hits"]),
            })
    return out


# --------------------------------------------------------------------------- #
# State construction
# --------------------------------------------------------------------------- #

def _fresh_side_state() -> dict:
    return {
        "primary": None, "shift_dir": None, "streak": 0,
        "pending_top": None, "pending_since": None,
        "bias": None, "qual2_state": "absent",
        "pending_qual2": None, "pending_qual2_since": None,
        "last_shift_confirm_ts": None, "suppressed_prev": False,
        "degenerate": False,
        "levels": {"primary": None, "second": None},
    }


def _baseline_side(side_state: dict, observed: dict | None, spot: float, config: Mapping[str, Any]) -> None:
    """Fix a side's baseline at the first ≥09:15 fetch: confirmed primary = observed primary, no shift
    yet, and any qualifying-2nd present sets the bias immediately."""
    if observed is None:  # degenerate side at baseline — stays Neutral until it has a real primary
        return
    side_state["primary"] = observed["primary"]
    pos = _qual2_position(observed, observed["primary"])
    side_state["qual2_state"] = pos
    side_state["bias"] = None if pos == "absent" else (_BULLISH if pos == "above" else _BEARISH)
    second = observed["second"] if observed.get("second_qualifies") else None
    _reconcile_levels(side_state, observed["primary"], second, spot, config)


def _neutral_read(stamp: dict | None = None) -> dict:
    empty = {"shift": None, "bias": None, "display": "Neutral", "pending": False, "streak": 0, "levels": []}
    return {
        "call": dict(empty), "put": dict(empty),
        "instrument": {"shift_layer": "Range-Bound", "bias_layer": "No Bias"},
        "stamp_0920": stamp,
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def interpret_pair(
    prev_state: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
    *,
    now: datetime,
    config: Mapping[str, Any],
) -> tuple[dict, dict]:
    """Advance one pair's interpretor state by one wave and return ``(next_state, read)``.

    ``now`` is a naive-IST datetime. Before the first fetch ≥ 09:15 IST nothing is baselined and a
    Neutral read is returned. ``config`` carries the tunables plus ``oi_floor``. ``next_state`` is
    JSON-serializable; ``read`` is what the display consumes.
    """
    rows = snapshot.get("rows") or []
    spot = _as_float(snapshot.get("underlying_price"))
    strike_size = _as_float(snapshot.get("strike_size")) or 0.0
    oi_floor = float(config.get("oi_floor", 0.0))
    tolerance = float(config.get("hit_tolerance_pct", 0.0)) * strike_size

    state = dict(prev_state) if prev_state else {}
    baseline_taken = bool(state.get("baseline_taken"))

    # 09:15 gate — do nothing (no baseline) before the session open; keep prior state intact.
    if not baseline_taken and now.time() < SESSION_GATE:
        state.setdefault("call", _fresh_side_state())
        state.setdefault("put", _fresh_side_state())
        return state, _neutral_read(state.get("stamp_0920"))

    if spot is None or spot <= 0:
        # No usable spot this wave — hold state, emit neutral (never fabricate).
        state.setdefault("call", _fresh_side_state())
        state.setdefault("put", _fresh_side_state())
        return state, _neutral_read(state.get("stamp_0920"))

    call_map = _side_map(rows, "CE")
    put_map = _side_map(rows, "PE")
    call_obs = _observe_levels(call_map, spot, oi_floor)
    put_obs = _observe_levels(put_map, spot, oi_floor)

    call_state = dict(state.get("call") or _fresh_side_state())
    put_state = dict(state.get("put") or _fresh_side_state())

    if not baseline_taken:
        _baseline_side(call_state, call_obs, spot, config)
        _baseline_side(put_state, put_obs, spot, config)
        state["baseline_taken"] = True
    else:
        _advance_side(call_state, call_obs, spot, now, config)
        _advance_side(put_state, put_obs, spot, now, config)

    # Hits + confidence run every wave once each side has a baseline primary (a separate track — never
    # suppressed, unlike the bias). Only sides with a confirmed primary participate.
    if call_state.get("primary") is not None or put_state.get("primary") is not None:
        _apply_hits(call_state, put_state, call_map, put_map, spot, state.get("prev_spot"), tolerance, config)

    # 09:20 static stamp — recorded once, only from a fetch that lands in the window [09:20, 09:21) (the
    # earliest wave in that minute wins). No fetch in the window → no stamp, ever, for that pair.
    if state.get("stamp_0920") is None and STAMP_TIME <= now.time() < STAMP_END:
        state["stamp_0920"] = _build_0920_stamp(call_obs, put_obs)

    state["call"] = call_state
    state["put"] = put_state
    state["prev_spot"] = spot

    # A side degenerate THIS wave (no primary above the floor) reads Neutral while its latched state is
    # held frozen — so a mid-session liquidity gap doesn't show a stale directional read.
    return state, _build_read(
        call_state, put_state, now, config, state.get("stamp_0920"),
        call_degenerate=call_obs is None, put_degenerate=put_obs is None,
    )


def _advance_side(side_state: dict, observed: dict | None, spot: float, now: datetime, config: Mapping[str, Any]) -> None:
    """One post-baseline wave for a side: shift machine, bias machine, then level reconciliation.

    A degenerate side (no observed primary above the floor) is Neutral this wave and is FLAGGED, so that
    when a real primary returns it is **re-baselined fresh** — never resumed from its stale latched state.
    A side that has never yet baselined a primary is likewise baselined on its first real primary
    (start-agnostic). Re-baseline reuses the exact day-start baseline logic (immediate bias included)."""
    if observed is None:
        side_state["degenerate"] = True
        return
    if side_state.get("primary") is None or side_state.get("degenerate"):
        # First real primary for this side, or a return from a degenerate gap → re-baseline FRESH: drop
        # any latched shift / bias / streak / levels, then re-run the day-start baseline logic.
        side_state.update(_fresh_side_state())
        _baseline_side(side_state, observed, spot, config)
        return
    _update_shift(side_state, observed["primary"], now, config)
    confirmed_primary = side_state["primary"]
    observed_pos = _qual2_position(observed, confirmed_primary)
    _update_bias(side_state, observed_pos, now, config)
    second = observed["second"] if observed.get("second_qualifies") else None
    _reconcile_levels(side_state, confirmed_primary, second, spot, config)


def _build_0920_stamp(call_obs: dict | None, put_obs: dict | None) -> dict:
    def _one(obs):
        if obs is None:
            return {"primary": None, "second": None}
        return {"primary": obs["primary"], "second": obs["second"] if obs.get("second_qualifies") else None}
    return {"call": _one(call_obs), "put": _one(put_obs)}


def _build_read(
    call_state: dict, put_state: dict, now: datetime, config: Mapping[str, Any], stamp: dict | None,
    *, call_degenerate: bool = False, put_degenerate: bool = False,
) -> dict:
    def _side(side_state: dict, degenerate: bool) -> dict:
        if degenerate:  # skipped (Neutral) until it has a real primary above the floor
            return {"shift": None, "bias": None, "display": "Neutral", "pending": False, "streak": 0, "levels": []}
        shift = side_state.get("shift_dir")
        bias = _bias_output(side_state, now, config)
        return {
            "shift": shift,
            "bias": bias,
            "display": _side_display(shift, bias),
            "pending": side_state.get("pending_top") is not None,
            "streak": int(side_state.get("streak", 0)),
            "levels": _level_readout(side_state),
        }

    call = _side(call_state, call_degenerate)
    put = _side(put_state, put_degenerate)
    return {
        "call": call,
        "put": put,
        "instrument": {
            "shift_layer": _combine(call["shift"], put["shift"], _SHIFT_LABELS),
            "bias_layer": _combine(call["bias"], put["bias"], _BIAS_LABELS),
        },
        "stamp_0920": stamp,
    }
