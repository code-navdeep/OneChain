"""Chain-table reference hovers — A1 (yesterday IV), A3 (interpretor cells), A2 display."""

from __future__ import annotations

from features.chain.table_model import (
    _apply_hover,
    _yesterday_iv_hover,
    build_chain_table_data,
    build_tooltips,
    extract_strike_value,
)
from ui.formatters import interp_hover_line


# --- A1: Totals-row yesterday IV hover shows both IV types ------------------------------------- #


def test_a1_yesterday_iv_hover_shows_both_when_atm_present():
    assert _yesterday_iv_hover(12.5, 13.2) == "Yesterday: 12.50 | 13.20"


def test_a1_yesterday_iv_hover_avg_only_when_atm_none():
    # ATM unreachable → drop that half, never fabricate a value.
    assert _yesterday_iv_hover(12.5, None) == "Yesterday: 12.50"


def test_a1_yesterday_iv_hover_none_when_no_usable_average():
    assert _yesterday_iv_hover(0, 13.2) is None
    assert _yesterday_iv_hover(None, 13.2) is None
    assert _yesterday_iv_hover("", None) is None


# --- A3: interpretor-cell hover (per-side streak + 09:20 stamp) -------------------------------- #


def test_a3_hover_line_full_when_stamp_present():
    side = {"streak": 3, "shift": "bullish"}
    stamp_side = {"primary": 22600.0, "second": 22400.0}
    assert interp_hover_line(side, stamp_side, stamp_present=True) == "streak +3 | 09:20 22600 (2nd 22400)"


def test_a3_hover_line_dash_for_missing_wall_within_present_stamp():
    side = {"streak": 2, "shift": "bearish"}
    assert interp_hover_line(side, {"primary": 22300.0, "second": None}, stamp_present=True) == "streak -2 | 09:20 22300 (2nd -)"


def test_a3_hover_line_late_start_drops_the_0920_half():
    side = {"streak": 3, "shift": "bullish"}
    # No genuine 09:20 data → just the streak, never a fabricated '-'.
    assert interp_hover_line(side, None, stamp_present=False) == "streak +3"


def test_a3_hover_line_empty_when_no_stamp_and_no_shift():
    # No 09:20 stamp AND no shift (streak em-dash) → empty line → build_tooltips renders no tooltip.
    assert interp_hover_line({"streak": 0, "shift": None}, None, stamp_present=False) == ""


def test_a3_hover_line_present_stamp_still_shows_with_no_shift():
    # A present 09:20 stamp always renders, even with no shift — the walls are real info.
    line = interp_hover_line({"streak": 0, "shift": None}, {"primary": 22600.0, "second": None}, stamp_present=True)
    assert line == "streak — | 09:20 22600 (2nd -)"


def _interp_report():
    return {
        "underlying_price": 22500.0,
        "lot_size": 50,
        "rows": [
            {"option_type": "CE", "strike": 22600.0, "oi_lots": 5000.0, "last_price": 100.0,
             "volume_lots": 10.0, "iv": 12.0, "gamma_exposure": 1.0, "previous_oi_lots": 4000.0},
            {"option_type": "PE", "strike": 22400.0, "oi_lots": 5000.0, "last_price": 90.0,
             "volume_lots": 8.0, "iv": 13.0, "gamma_exposure": -1.0, "previous_oi_lots": 3500.0},
        ],
    }


def _underlying(data):
    return next(row for row in data if row.get("is_underlying_row"))


def test_a3_apply_interpretor_sets_underlying_row_hovers_when_stamp_present():
    read = {
        "call": {"streak": 3, "shift": "bullish"},
        "put": {"streak": 2, "shift": "bearish"},
        "instrument": {"shift_layer": "Bullish", "bias_layer": "Bearish Bias"},
        "stamp_0920": {"call": {"primary": 22600.0, "second": 22400.0},
                       "put": {"primary": 22300.0, "second": None}},
    }
    under = _underlying(build_chain_table_data(_interp_report(), interpretor=read))
    assert under["call_hover"] == "streak +3 | 09:20 22600 (2nd 22400)"
    assert under["put_hover"] == "streak -2 | 09:20 22300 (2nd -)"


def test_a3_apply_interpretor_late_start_hover_is_streak_only():
    read = {
        "call": {"streak": 3, "shift": "bullish"},
        "put": {"streak": 2, "shift": "bearish"},
        "instrument": {"shift_layer": "Bullish", "bias_layer": "Bearish Bias"},
        "stamp_0920": None,
    }
    under = _underlying(build_chain_table_data(_interp_report(), interpretor=read))
    assert under["call_hover"] == "streak +3"
    assert under["put_hover"] == "streak -2"


# --- A2: 09:20 snapshot hovers (_apply_hover) — {"strikes": …, "totals": …} -------------------- #


def _snap_entry(**kwargs):
    entry = {"call_oi": None, "put_oi": None, "call_iv": None, "put_iv": None, "pcr": None, "straddle_oi": None}
    entry.update(kwargs)
    return entry


def _totals(**kwargs):
    block = {"call_oi": None, "put_oi": None, "pcr": None, "call_atm_iv": None, "put_atm_iv": None}
    block.update(kwargs)
    return block


def test_a2_apply_hover_sets_per_strike_hovers_on_data_rows():
    rows = [{"strike": "22600"}]
    entry = _snap_entry(call_oi=5000.0, put_oi=3000.0, call_iv=12.0, put_iv=13.0, pcr=0.6, straddle_oi=8000.0)
    _apply_hover(rows, {"strikes": {"22600": entry}})
    r = rows[0]
    assert r["call_oi_hover"] == "09:20 OI: 5.0K"
    assert r["put_oi_hover"] == "09:20 OI: 3.0K"
    assert r["call_iv_hover"] == "09:20 IV: 12.00"
    assert r["put_iv_hover"] == "09:20 IV: 13.00"
    assert r["strike_pcr_hover"] == "09:20 PCR: 0.60"
    assert r["strike_straddle_hover"] == "09:20 Straddle: 8.0K | IV 12.50"  # both sides → avg(12,13)


def test_a2_apply_hover_appends_on_tracked_oi_cell():
    # A tracked strike already carries `Conf X% · Hits N` (from _apply_interpretor, which runs first).
    rows = [{"strike": "22600", "call_oi_hover": "Conf 75% · Hits 2"}]
    _apply_hover(rows, {"strikes": {"22600": _snap_entry(call_oi=5000.0, straddle_oi=5000.0)}})
    assert rows[0]["call_oi_hover"] == "Conf 75% · Hits 2 | 09:20 OI: 5.0K"


def test_a2_apply_hover_straddle_iv_both_sides():
    rows = [{"strike": "22600"}]
    _apply_hover(rows, {"strikes": {"22600": _snap_entry(call_iv=12.0, put_iv=14.0, straddle_oi=8000.0)}})
    assert rows[0]["strike_straddle_hover"] == "09:20 Straddle: 8.0K | IV 13.00"


def test_a2_apply_hover_straddle_iv_omitted_when_one_sided():
    rows = [{"strike": "22600"}]
    _apply_hover(rows, {"strikes": {"22600": _snap_entry(call_iv=12.0, straddle_oi=5000.0)}})  # put_iv None
    assert rows[0]["strike_straddle_hover"] == "09:20 Straddle: 5.0K"


def test_a2_apply_hover_none_value_sets_no_hover():
    rows = [{"strike": "22400"}]
    entry = _snap_entry(put_oi=3000.0, put_iv=13.0, straddle_oi=3000.0)  # call side + pcr are None
    _apply_hover(rows, {"strikes": {"22400": entry}})
    r = rows[0]
    assert "call_oi_hover" not in r and "call_iv_hover" not in r and "strike_pcr_hover" not in r
    assert r["put_oi_hover"] == "09:20 OI: 3.0K"
    assert r["put_iv_hover"] == "09:20 IV: 13.00"
    assert r["strike_straddle_hover"] == "09:20 Straddle: 3.0K"  # one-sided → no IV


def test_a2_apply_hover_underlying_row_appends_totals():
    rows = [{"strike": "₹22500", "is_underlying_row": True,
             "call_oi_hover": "Yesterday: 1.2K", "put_oi_hover": "Yesterday: 900",
             "strike_pcr_hover": "Yesterday: 0.85 (0.90)", "call_iv_hover": "Yesterday: 12.50 | 13.20"}]
    totals = _totals(call_oi=5200.0, put_oi=4500.0, pcr=0.88, call_atm_iv=13.5)
    _apply_hover(rows, {"strikes": {}, "totals": totals})
    r = rows[0]
    assert r["call_oi_hover"] == "Yesterday: 1.2K · 09:20: 5.2K"
    assert r["put_oi_hover"] == "Yesterday: 900 · 09:20: 4.5K"
    assert r["strike_pcr_hover"] == "Yesterday: 0.85 (0.90) · 09:20: 0.88"
    assert r["call_iv_hover"] == "Yesterday: 12.50 | 13.20 · 09:20: ATM IV - 13.50"


def test_a2_apply_hover_underlying_row_alone_when_no_yesterday():
    rows = [{"strike": "₹22500", "is_underlying_row": True}]  # no yesterday hovers set
    _apply_hover(rows, {"strikes": {}, "totals": _totals(call_oi=5200.0, call_atm_iv=13.5)})
    r = rows[0]
    assert r["call_oi_hover"] == "09:20: 5.2K"           # 09:20 part alone (no yesterday to join)
    assert r["call_iv_hover"] == "09:20: ATM IV - 13.50"


def test_a2_apply_hover_underlying_row_untouched_without_totals():
    rows = [{"strike": "₹22500", "is_underlying_row": True, "call_oi_hover": "Yesterday: 1.2K"}]
    _apply_hover(rows, {"strikes": {}, "totals": {}})  # empty totals block → yesterday hover untouched
    assert rows[0]["call_oi_hover"] == "Yesterday: 1.2K"


def test_a2_apply_hover_noop_without_a_snapshot():
    rows = [{"strike": "22600", "call_oi_hover": "Conf 75% · Hits 2"}]
    _apply_hover(rows, None)
    _apply_hover(rows, {})
    assert rows[0] == {"strike": "22600", "call_oi_hover": "Conf 75% · Hits 2"}


def test_a2_build_tooltips_maps_the_straddle_hover():
    tips = build_tooltips([{"strike_straddle_hover": "09:20 Straddle: 8.0K"}])
    assert tips[0]["strike_straddle"] == "09:20 Straddle: 8.0K"


def test_a2_hover_threads_through_build_chain_table_data():
    hover = {"strikes": {"22600": _snap_entry(call_oi=5000.0, call_iv=12.0, straddle_oi=5000.0)}}
    data = build_chain_table_data(_interp_report(), hover=hover)
    row = next(r for r in data if not r.get("is_underlying_row") and extract_strike_value(r.get("strike", "")) == 22600.0)
    assert row["call_oi_hover"] == "09:20 OI: 5.0K"
    assert row["call_iv_hover"] == "09:20 IV: 12.00"
    assert row["strike_straddle_hover"] == "09:20 Straddle: 5.0K"
