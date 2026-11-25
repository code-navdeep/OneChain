from __future__ import annotations

from dataclasses import dataclass

import pytest

from features.required_move_drawer.callbacks import (
    MAX_PINNED,
    _windowed_second_oi_wall,
    bucket_for,
    build_contract,
    clear_bucket,
    is_live_pair,
    push_contract,
    push_to_bucket,
)
from features.required_move_drawer.rendering import build_sparkline_figure


@dataclass
class _Ctx:
    source: str
    kind: str


def _contract(strike: float, side: str = "CE") -> dict:
    return {"asset_id": 13, "segment_code": "IDX_I", "expiry": "2026-04-30", "strike": strike, "option_type": side}


_ROWS = [
    {"strike": 22500.0, "option_type": "CE", "iv": 14.0},
    {"strike": 22500.0, "option_type": "PE", "iv": 15.0},
    {"strike": 22600.0, "option_type": "CE", "iv": 13.0},
]

# Real chain strike cells are multi-line with the straddle price (which carries ₹) on line 2;
# only the spliced underlying row *starts* with ₹.
_TABLE = [
    {"strike": "22400\n(₹110)"},
    {"strike": "22500\n(₹125)"},
    {"strike": "₹22500.00\n(+1.2K)"},  # the spliced underlying row
    {"strike": "22600\n(₹98)"},
]


# (The wall pain-point resolution moved to state.selectors.windowed_max_oi_pain, tested in
# tests/test_selectors.py — it now windows the rows before picking the highest-OI strike.)


# --- push_contract reducer ---


def test_push_contract_newest_first_and_capped():
    contracts: list[dict] = []
    for strike in (22400, 22500, 22600, 22700, 22800, 22900):
        contracts = push_contract(contracts, _contract(strike))
    assert len(contracts) == MAX_PINNED
    assert contracts[0]["strike"] == 22900  # newest first
    assert 22400 not in [c["strike"] for c in contracts]  # oldest dropped past the cap


def test_push_contract_dedups_and_promotes():
    contracts = push_contract([], _contract(22500))
    contracts = push_contract(contracts, _contract(22600))
    contracts = push_contract(contracts, _contract(22500))  # re-select existing
    assert len(contracts) == 2
    assert contracts[0]["strike"] == 22500  # promoted to front, not duplicated


# --- per-pair bucketing (keeps one pill's pins out of every other pill's view) ---

_NIFTY_APR = "13|IDX_I|2026-04-30"
_NIFTY_MAY = "13|IDX_I|2026-05-07"
_BANK_APR = "25|IDX_I|2026-04-30"


def _ck(strike: float, *, asset_id: int = 13, expiry: str = "2026-04-30") -> dict:
    return {"asset_id": asset_id, "segment_code": "IDX_I", "expiry": expiry, "strike": strike, "option_type": "CE"}


def test_buckets_keep_instruments_separate():
    store = push_to_bucket({}, _NIFTY_APR, _ck(22500, asset_id=13))
    store = push_to_bucket(store, _BANK_APR, _ck(48000, asset_id=25))
    # Each pair sees only its own pin — no leakage across instruments.
    assert [c["strike"] for c in bucket_for(store, _NIFTY_APR)] == [22500]
    assert [c["strike"] for c in bucket_for(store, _BANK_APR)] == [48000]


def test_buckets_keep_expiries_separate():
    # Per-PAIR scope: two expiries of the same instrument are two buckets — a pin on one expiry
    # pill neither shows on nor evicts from the other (the single-instrument multi-expiry mix-up).
    store = push_to_bucket({}, _NIFTY_APR, _ck(22500, expiry="2026-04-30"))
    store = push_to_bucket(store, _NIFTY_MAY, _ck(22600, expiry="2026-05-07"))
    assert [c["strike"] for c in bucket_for(store, _NIFTY_APR)] == [22500]
    assert [c["strike"] for c in bucket_for(store, _NIFTY_MAY)] == [22600]


def test_bucket_cap_is_per_pair():
    store: dict = {}
    for strike in (22400, 22500, 22600, 22700, 22800):  # 5 > MAX_PINNED for one pair
        store = push_to_bucket(store, _NIFTY_APR, _ck(strike))
    store = push_to_bucket(store, _NIFTY_MAY, _ck(22500, expiry="2026-05-07"))
    assert len(bucket_for(store, _NIFTY_APR)) == MAX_PINNED  # capped within the pair
    assert len(bucket_for(store, _NIFTY_MAY)) == 1           # the sibling expiry is untouched


def test_clear_bucket_only_drops_active_pair():
    store = push_to_bucket({}, _NIFTY_APR, _ck(22500))
    store = push_to_bucket(store, _NIFTY_MAY, _ck(22600, expiry="2026-05-07"))
    store = clear_bucket(store, _NIFTY_APR)
    assert bucket_for(store, _NIFTY_APR) == []          # cleared
    assert [c["strike"] for c in bucket_for(store, _NIFTY_MAY)] == [22600]  # sibling expiry kept


def test_bucket_for_missing_or_none_key():
    assert bucket_for(None, _NIFTY_APR) == []
    assert bucket_for({_NIFTY_APR: [_ck(22500)]}, None) == []


# --- build_contract resolution ---


def test_build_contract_resolves_call_price_cell():
    contract = build_contract(
        {"row": 1, "column_id": "call_last_price"}, _TABLE,
        asset_id=13, segment_code="IDX_I", expiry="2026-04-30", rows=_ROWS,
    )
    assert contract == _contract(22500.0, "CE")


def test_build_contract_resolves_put_price_cell():
    contract = build_contract(
        {"row": 1, "column_id": "put_last_price"}, _TABLE,
        asset_id=13, segment_code="IDX_I", expiry="2026-04-30", rows=_ROWS,
    )
    assert contract["option_type"] == "PE"


def test_build_contract_rejects_non_price_column():
    assert build_contract(
        {"row": 1, "column_id": "strike"}, _TABLE,
        asset_id=13, segment_code="IDX_I", expiry="2026-04-30", rows=_ROWS,
    ) is None


def test_build_contract_rejects_underlying_row():
    assert build_contract(
        {"row": 2, "column_id": "call_last_price"}, _TABLE,
        asset_id=13, segment_code="IDX_I", expiry="2026-04-30", rows=_ROWS,
    ) is None


def test_build_contract_rejects_unmatched_strike():
    assert build_contract(
        {"row": 0, "column_id": "put_last_price"}, _TABLE,  # 22400 has no PE row
        asset_id=13, segment_code="IDX_I", expiry="2026-04-30", rows=_ROWS,
    ) is None


# --- live + per-pair gating ---


def test_is_live_pair_gate():
    assert is_live_pair(_Ctx(source="live", kind="pair")) is True
    assert is_live_pair(_Ctx(source="playback", kind="pair")) is False
    assert is_live_pair(_Ctx(source="live", kind="aggregate")) is False
    assert is_live_pair(None) is False


# --- _windowed_second_oi_wall: the local 75%-gated 2nd-highest-OI wall (windowed) ---


def _second_report():
    return {
        "underlying_price": 22500.0,
        "rows": [
            {"strike": 22600.0, "option_type": "CE", "oi_lots": 5000.0, "pain_point": 22650.0},  # top CE
            {"strike": 22700.0, "option_type": "CE", "oi_lots": 4000.0, "pain_point": 22740.0},  # 2nd, 80% ≥ 75% → qual
            {"strike": 22800.0, "option_type": "CE", "oi_lots": 1000.0, "pain_point": 22820.0},
            {"strike": 22400.0, "option_type": "PE", "oi_lots": 5000.0, "pain_point": 22360.0},  # top PE
            {"strike": 22300.0, "option_type": "PE", "oi_lots": 3000.0, "pain_point": 22280.0},  # 2nd, 60% < 75% → no
        ],
    }


def test_second_wall_qualifies_at_or_above_75pct():
    assert _windowed_second_oi_wall(_second_report(), "All", "CE") == (22700.0, 22740.0)


def test_second_wall_below_75pct_is_none():
    assert _windowed_second_oi_wall(_second_report(), "All", "PE") is None


def test_second_wall_none_when_fewer_than_two_strikes():
    report = {"underlying_price": 22500.0,
              "rows": [{"strike": 22600.0, "option_type": "CE", "oi_lots": 5000.0, "pain_point": 22650.0}]}
    assert _windowed_second_oi_wall(report, "All", "CE") is None


def test_second_wall_none_when_qualifying_second_has_no_pain_point():
    report = {"underlying_price": 22500.0, "rows": [
        {"strike": 22600.0, "option_type": "CE", "oi_lots": 5000.0, "pain_point": 22650.0},
        {"strike": 22700.0, "option_type": "CE", "oi_lots": 4500.0, "pain_point": 0},  # qual by OI, no pain
    ]}
    assert _windowed_second_oi_wall(report, "All", "CE") is None


# --- Sparkline y-range: the LTP band keeps ≥ ⅕ of the height; far reversals clip (⅕ cap) ---


def _yrange(curve: dict):
    return build_sparkline_figure(curve, title="t").layout.yaxis.range


def test_yrange_price_band_dominates_and_far_reversal_clips():
    curve = {
        "ok": True, "reversal_x": ["2026-06-22T10:00:00", "2026-06-22T10:05:00"],
        "ltp_x": ["2026-06-22T10:00:00", "2026-06-22T10:05:00"], "ltp_y": [100.0, 110.0],  # band span 10
        "reversal_y": [105.0, 106.0],        # near the price band → pulled in
        "call_reversal_y": [500.0, 490.0],   # far (> 3×span above) → clipped off-range
    }
    lo, hi = _yrange(curve)
    # LTP band [100,110] + 10% pad; the far 500 line is left off-screen (does not stretch the range).
    assert lo == pytest.approx(99.0) and hi == pytest.approx(111.0)
    assert hi < 200


def test_yrange_spans_all_when_no_price_line():
    curve = {"ok": True, "reversal_x": ["2026-06-22T10:00:00"], "ltp_y": [], "reversal_y": [100.0, 200.0]}
    lo, hi = _yrange(curve)
    # No LTP band to anchor on → fall back to spanning every series (± 10% pad).
    assert lo == pytest.approx(90.0) and hi == pytest.approx(210.0)
