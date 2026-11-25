"""state.selectors.windowed_max_oi_pain — the windowed OI-wall pain point.

Both the price-chart pain lines and the reversal-curve walls use this to pick the highest-OI
strike's pain point from the strikes currently in the strike-range window (not the full chain).
"""

from __future__ import annotations

from state.selectors import windowed_max_oi_pain, windowed_max_oi_strike


def _row(strike: float, side: str, oi: float, pain) -> dict:
    return {"strike": strike, "option_type": side, "oi_lots": oi, "pain_point": pain}


# Spot 22450 sits between 22400 and 22500. The far 23000 CE has the biggest OI in the whole chain,
# but it's outside a ±1 window.
_REPORT = {
    "underlying_price": 22450.0,
    "rows": [
        _row(22000.0, "CE", 5, 22050.0),
        _row(22400.0, "CE", 8, 22450.0),
        _row(22500.0, "CE", 12, 22550.0),
        _row(22600.0, "CE", 20, 22650.0),
        _row(23000.0, "CE", 100, 23050.0),   # full-chain CE wall, far OTM
        _row(22400.0, "PE", 9, 22350.0),
        _row(22500.0, "PE", 15, 22450.0),
    ],
}


def test_windowed_pain_picks_in_view_wall_not_the_far_full_chain_one():
    # ±1 keeps {22400, 22500}; the in-window CE wall is 22500 (oi 12), NOT 23000 (oi 100).
    assert windowed_max_oi_pain(_REPORT, "1", "CE") == 22550.0


def test_all_returns_the_full_chain_wall():
    # "All" → no windowing → the global CE wall (23000) and its pain point.
    assert windowed_max_oi_pain(_REPORT, "All", "CE") == 23050.0


def test_put_side_resolved_independently():
    # ±1 keeps {22400, 22500}; PE wall is 22500 (oi 15) > 22400 (oi 9).
    assert windowed_max_oi_pain(_REPORT, "1", "PE") == 22450.0


def test_none_when_side_has_no_rows():
    report = {"underlying_price": 22450.0, "rows": [_row(22500.0, "CE", 12, 22550.0)]}
    assert windowed_max_oi_pain(report, "All", "PE") is None


def test_none_when_wall_has_no_usable_pain_point():
    report = {"underlying_price": 22450.0, "rows": [_row(22500.0, "CE", 12, None)]}
    assert windowed_max_oi_pain(report, "All", "CE") is None


def test_windowed_max_oi_strike_returns_the_strike_in_view():
    # The reversal-curve legend labels the wall by its strike (not its pain point): ±1 → 22500 CE.
    assert windowed_max_oi_strike(_REPORT, "1", "CE") == 22500.0
    assert windowed_max_oi_strike(_REPORT, "1", "PE") == 22500.0


def test_windowed_max_oi_strike_all_is_full_chain_wall():
    assert windowed_max_oi_strike(_REPORT, "All", "CE") == 23000.0


def test_windowed_max_oi_strike_none_when_side_empty():
    report = {"underlying_price": 22450.0, "rows": [_row(22500.0, "CE", 12, 22550.0)]}
    assert windowed_max_oi_strike(report, "All", "PE") is None
