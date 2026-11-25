"""Phase 3 tests — the interpretor overlay onto the chain table (central-row labels + OI hovers)."""

from __future__ import annotations

from features.chain.table_model import build_chain_table_data, extract_strike_value


def _report():
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


_INTERP = {
    "call": {"shift": "bullish", "bias": None, "pending": False, "streak": 1,
             "levels": [{"role": "primary", "strike": 22600.0, "confidence": 75.0, "hits": 1}]},
    "put": {"shift": None, "bias": "bearish", "pending": True, "streak": 0,
            "levels": [{"role": "primary", "strike": 22400.0, "confidence": 50.0, "hits": 2}]},
    "instrument": {"shift_layer": "Bullish", "bias_layer": "Bearish Bias"},
    "stamp_0920": None,
}


def _underlying(data):
    return next(row for row in data if row.get("is_underlying_row"))


def _data_row(data, strike):
    return next(
        row for row in data
        if not row.get("is_underlying_row") and extract_strike_value(row.get("strike", "")) == strike
    )


def test_apply_interpretor_fills_central_row_two_line_cells():
    data = build_chain_table_data(_report(), interpretor=_INTERP)
    under = _underlying(data)
    assert under["call_last_price"] == "Bullish\n"           # shift Bullish · bias None → blank line
    assert under["put_last_price"] == "🚩\n🔴"              # blank shift + pending 🚩 · bias 🔴 dot
    assert under["strike_straddle"] == "Bullish\n🔴"        # Shift layer / Bias layer (dot)


def test_apply_interpretor_sets_tracked_strike_hovers_only():
    data = build_chain_table_data(_report(), interpretor=_INTERP)
    assert _data_row(data, 22600.0)["call_oi_hover"] == "Conf 75% · Hits 1"
    assert _data_row(data, 22400.0)["put_oi_hover"] == "Conf 50% · Hits 2"
    # the call side's OI hover is not set on the put-only strike, and vice-versa
    assert not _data_row(data, 22400.0).get("call_oi_hover")
    assert not _data_row(data, 22600.0).get("put_oi_hover")


def test_no_interpretor_leaves_central_row_and_hovers_empty():
    data = build_chain_table_data(_report(), interpretor=None)
    under = _underlying(data)
    assert under["call_last_price"] == ""
    assert under["put_last_price"] == ""
    assert under["strike_straddle"] == ""
    for row in data:
        assert not row.get("call_oi_hover")
        assert not row.get("put_oi_hover")


# --- The new display forms: dots, "S." abbreviation, In-Range, and the blank/empty cases ---------- #

_QUIET_SIDE = {"shift": None, "bias": None, "pending": False, "streak": 0, "levels": []}


def _interp(instrument, *, call=None, put=None):
    return {
        "call": call or dict(_QUIET_SIDE),
        "put": put or dict(_QUIET_SIDE),
        "instrument": instrument,
        "stamp_0920": None,
    }


def test_instrument_cell_strong_abbreviates_and_double_dots():
    interp = _interp({"shift_layer": "Strong Bullish", "bias_layer": "Strong Bullish Bias"})
    assert _underlying(build_chain_table_data(_report(), interpretor=interp))["strike_straddle"] == "S.Bull\n🟢🟢"


def test_instrument_cell_strong_bearish_double_red_dots():
    interp = _interp({"shift_layer": "Strong Bearish", "bias_layer": "Strong Bearish Bias"})
    assert _underlying(build_chain_table_data(_report(), interpretor=interp))["strike_straddle"] == "S.Bear\n🔴🔴"


def test_instrument_cell_range_bound_reads_in_range_and_no_bias_blank():
    interp = _interp({"shift_layer": "Range-Bound", "bias_layer": "No Bias"})
    assert _underlying(build_chain_table_data(_report(), interpretor=interp))["strike_straddle"] == "In-Range\n"


def test_instrument_cell_undecided_and_no_clear_bias_stay_words():
    interp = _interp({"shift_layer": "Undecided", "bias_layer": "No Clear Bias"})
    assert _underlying(build_chain_table_data(_report(), interpretor=interp))["strike_straddle"] == "Undecided\nNo Clear Bias"


def test_per_side_fully_neutral_is_empty_cell():
    interp = _interp({"shift_layer": "Range-Bound", "bias_layer": "No Bias"})
    under = _underlying(build_chain_table_data(_report(), interpretor=interp))
    assert under["call_last_price"] == "" and under["put_last_price"] == ""


def test_per_side_bias_only_leaves_a_blank_shift_line():
    interp = _interp(
        {"shift_layer": "Range-Bound", "bias_layer": "No Bias"},
        call={"shift": None, "bias": "bullish", "pending": False, "streak": 0, "levels": []},
    )
    assert _underlying(build_chain_table_data(_report(), interpretor=interp))["call_last_price"] == "\n🟢"
