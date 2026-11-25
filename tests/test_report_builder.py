from __future__ import annotations

from analytics.report_builder import (
    _vex_vol_regime,
    apply_pain_points,
    black_scholes_gamma,
    bracketing_atm_iv,
    build_pair_report,
    compute_atm_iv_gap,
    compute_pinning_pressure,
    compute_risk_reversal_25,
    flatten_pair_report,
    inflate_pair_report,
    risk_reversal_label,
)
from domain.requests import RequestSpec


def test_bracketing_atm_iv_brackets_and_one_sided():
    rows = [{"strike": 100.0, "iv": 10.0}, {"strike": 110.0, "iv": 20.0}]
    assert bracketing_atm_iv(rows, spot=105.0, option_type="CE") == 15.0  # (10 + 20) / 2
    assert bracketing_atm_iv(rows, spot=90.0, option_type="CE") is None   # all strikes above → one-sided
    assert bracketing_atm_iv([], spot=105.0, option_type="CE") is None    # no rows


def test_compute_atm_iv_gap_per_side_and_none():
    rows = [
        {"option_type": "CE", "strike": 100.0, "iv": 10.0}, {"option_type": "CE", "strike": 110.0, "iv": 14.0},
        {"option_type": "PE", "strike": 100.0, "iv": 16.0}, {"option_type": "PE", "strike": 110.0, "iv": 20.0},
    ]
    gap = compute_atm_iv_gap(rows, underlying_price=105.0)
    assert gap["atm_call_iv"] == 12.0  # (10 + 14) / 2
    assert gap["atm_put_iv"] == 18.0   # (16 + 20) / 2
    assert gap["atm_gap"] == 6.0       # 18 - 12 (puts bid up at ATM)
    assert compute_atm_iv_gap(rows, underlying_price=0.0) == {"atm_call_iv": None, "atm_put_iv": None, "atm_gap": None}


def _request(expiry: str = "2026-04-30") -> RequestSpec:
    return RequestSpec(
        asset_id=13, segment_code="IDX_I", expiry=expiry,
        display_name="Nifty", lot_size=65, strike_size=50,
    )


def _enriched_rows() -> list[dict]:
    """Hand-built enriched rows with deltas spanning 25Δ on both sides (spot 22500)."""
    spot = 22500.0
    specs = [
        # (strike, type, delta, iv)
        (22300, "CE", 0.75, 13.0), (22400, "CE", 0.60, 13.5), (22500, "CE", 0.50, 14.0),
        (22600, "CE", 0.30, 14.5), (22700, "CE", 0.15, 15.0),
        (22300, "PE", -0.15, 16.0), (22400, "PE", -0.30, 15.5), (22500, "PE", -0.50, 15.0),
        (22600, "PE", -0.70, 14.5), (22700, "PE", -0.85, 14.0),
    ]
    rows = []
    for strike, otype, delta, iv in specs:
        rows.append({
            "strike": float(strike), "option_type": otype, "delta": delta, "iv": iv,
            "gamma": 0.01, "vega": 7.0, "theta": -4.0, "last_price": 100.0,
            "oi_lots": 10.0, "previous_oi_lots": 9.0, "oi_change_lots": 1.0, "volume_lots": 5.0,
            "gamma_exposure": 1.0 if otype == "CE" else -1.0, "volatility_exposure": 70.0,
            "underlying_price": spot, "bs_price": None,
        })
    return rows


def test_black_scholes_gamma_peaks_atm_and_zero_at_degenerate_inputs():
    atm = black_scholes_gamma(spot=22500, strike=22500, time_to_expiry=0.08, rate=0.06, volatility=0.14)
    wing = black_scholes_gamma(spot=22500, strike=24000, time_to_expiry=0.08, rate=0.06, volatility=0.14)
    assert atm > wing > 0  # gamma peaks ATM, decays in the wings
    assert black_scholes_gamma(spot=22500, strike=22500, time_to_expiry=0.0, rate=0.06, volatility=0.14) == 0.0
    assert black_scholes_gamma(spot=22500, strike=22500, time_to_expiry=0.08, rate=0.06, volatility=0.0) == 0.0
    assert black_scholes_gamma(spot=0, strike=22500, time_to_expiry=0.08, rate=0.06, volatility=0.14) == 0.0


def test_compute_risk_reversal_linear_in_delta():
    rows = [
        {"option_type": "CE", "delta": d, "iv": iv, "strike": 0}
        for d, iv in [(0.4, 12.0), (0.3, 13.0), (0.2, 14.0), (0.1, 15.0)]
    ] + [
        {"option_type": "PE", "delta": d, "iv": iv, "strike": 0}
        for d, iv in [(-0.4, 16.0), (-0.3, 17.0), (-0.2, 18.0), (-0.1, 19.0)]
    ]
    skew = compute_risk_reversal_25(rows)
    # 0.25 sits halfway between the 0.2 and 0.3 delta points on each side.
    assert skew["call_iv_25d"] == 13.5
    assert skew["put_iv_25d"] == 17.5
    assert skew["rr_25"] == -4.0


def test_compute_risk_reversal_none_when_25delta_unreachable():
    # All call deltas > 0.25 → the 25Δ call leg can't be interpolated → rr is None.
    rows = [
        {"option_type": "CE", "delta": d, "iv": 14.0, "strike": 0} for d in (0.6, 0.5, 0.4)
    ] + [
        {"option_type": "PE", "delta": d, "iv": 15.0, "strike": 0} for d in (-0.4, -0.3, -0.2)
    ]
    skew = compute_risk_reversal_25(rows)
    assert skew["call_iv_25d"] is None
    assert skew["rr_25"] is None


def test_risk_reversal_label_is_factual_sign_only():
    assert risk_reversal_label(-2.0) == "Put skew"
    assert risk_reversal_label(2.0) == "Call skew"
    assert risk_reversal_label(0.0) == "Flat"
    assert risk_reversal_label(None) is None


def test_build_pair_report_attaches_skew():
    report = build_pair_report(_request(), _enriched_rows())
    skew = report["skew"]
    assert skew["call_iv_25d"] == 14.67
    assert skew["put_iv_25d"] == 15.67
    assert skew["rr_25"] == -1.0
    # ATM parity gap: spot 22500 is on a strike → calls avg with the higher strike (22600),
    # puts with the lower (22400). call: (14.0+14.5)/2=14.25; put: (15.0+15.5)/2=15.25.
    assert skew["atm_call_iv"] == 14.25
    assert skew["atm_put_iv"] == 15.25
    assert skew["atm_gap"] == 1.0
    assert "atm_iv" not in skew                 # the old crude ATM IV is gone
    assert "volatility_metrics" not in report   # dead vol-metrics set removed


def test_skew_round_trips_through_flatten_and_inflate():
    report = build_pair_report(_request(), _enriched_rows())
    columns = flatten_pair_report(report)
    assert columns["risk_reversal_25"] == -1.0
    assert columns["call_iv_25d"] == 14.67
    assert columns["put_iv_25d"] == 15.67

    inflated = inflate_pair_report(columns, report["rows"], _request())
    assert inflated["skew"]["rr_25"] == -1.0
    assert inflated["skew"]["call_iv_25d"] == 14.67
    assert inflated["skew"]["put_iv_25d"] == 15.67
    # ATM trio is recomputed from rows on inflate → identical to the live build
    assert inflated["skew"]["atm_call_iv"] == report["skew"]["atm_call_iv"]
    assert inflated["skew"]["atm_put_iv"] == report["skew"]["atm_put_iv"]
    assert inflated["skew"]["atm_gap"] == report["skew"]["atm_gap"]


def _pin_rows(gamma_by_strike: dict[float, float]) -> list[dict]:
    """CE+PE rows at each strike with a given gamma (oi_lots fixed), spot 100."""
    rows = []
    for strike, gamma in gamma_by_strike.items():
        for otype in ("CE", "PE"):
            rows.append({"strike": float(strike), "option_type": otype, "gamma": gamma, "oi_lots": 10.0})
    return rows


def test_pinning_picks_nearest_concentrated_gamma_strike():
    rows = _pin_rows({90: 0.005, 100: 0.02, 110: 0.005})
    pin = compute_pinning_pressure(rows, underlying_price=100.0, strike_size=10, time_to_expiry=0.05)
    assert pin["pin_strike"] == 100.0
    assert pin["distance_pts"] == 0.0


def test_pinning_time_urgency_raises_strength():
    rows = _pin_rows({90: 0.005, 100: 0.02, 110: 0.005})
    near = compute_pinning_pressure(rows, underlying_price=100.0, strike_size=10, time_to_expiry=0.005)
    far = compute_pinning_pressure(rows, underlying_price=100.0, strike_size=10, time_to_expiry=0.5)
    assert near["pin_strike"] == far["pin_strike"] == 100.0
    # Same chain, nearer expiry → larger strength (the time-urgency factor survives).
    assert near["pin_strength"] > far["pin_strength"]


def test_pinning_none_without_gamma_or_spot():
    flat = compute_pinning_pressure(_pin_rows({100: 0.0}), underlying_price=100.0, strike_size=10, time_to_expiry=0.05)
    assert flat["pin_strike"] is None
    nospot = compute_pinning_pressure(_pin_rows({100: 0.02}), underlying_price=0.0, strike_size=10, time_to_expiry=0.05)
    assert nospot["pin_strike"] is None


def test_build_pair_report_attaches_pinning_and_volume_strikes():
    report = build_pair_report(_request(), _enriched_rows())
    # Uniform gamma mass, proximity peaks at spot → pin strike is the spot strike.
    assert report["pinning"]["pin_strike"] == 22500.0
    assert isinstance(report["levels"]["max_call_volume_strike"], float)
    assert isinstance(report["levels"]["max_put_volume_strike"], float)
    # 2nd-highest OI/Vol strikes (current value) — 5 strikes per side, so each is a float.
    for field in (
        "second_max_call_oi_strike", "second_max_put_oi_strike",
        "second_max_call_volume_strike", "second_max_put_volume_strike",
    ):
        assert isinstance(report["levels"][field], float)


def test_pinning_round_trips_through_inflate():
    report = build_pair_report(_request(), _enriched_rows())
    columns = flatten_pair_report(report)
    inflated = inflate_pair_report(columns, report["rows"], _request())
    assert inflated["pinning"] == report["pinning"]


def _pain_rows() -> list[dict]:
    """A CE/PE pair at strike 22500 with equal IV (iv_ratio = 1) and vol/oi = 15 on each side,
    so the raw pain signal is 15 on both sides."""
    return [
        {"strike": 22500.0, "option_type": "CE", "iv": 20.0, "volume": 15000, "oi": 1000},
        {"strike": 22500.0, "option_type": "PE", "iv": 20.0, "volume": 15000, "oi": 1000},
    ]


def _pain_points(strike_size: int) -> tuple[float, float]:
    rows = _pain_rows()
    apply_pain_points(rows, strike_size)
    ce = next(r["pain_point"] for r in rows if r["option_type"] == "CE")
    pe = next(r["pain_point"] for r in rows if r["option_type"] == "PE")
    return ce, pe


def test_pain_point_symmetric_and_unchanged_on_reference_grid():
    # signal = 15, S_ref = 50, strike_size = 50 → offset = (15/50)*50 = 15.
    ce, pe = _pain_points(50)
    assert ce == 22515.0          # strike + 15 (CE behaviour preserved on the 50-grid)
    assert pe == 22485.0          # strike - 15 (symmetric; no strike_size/2 floor)
    assert ce > 22500.0 > pe      # call pain above the strike, put pain below


def test_pain_point_offset_scales_linearly_with_strike_size():
    # Same inputs (signal = 15): offset = (15/50)*strike_size, so it scales with the grid.
    ce_small, pe_small = _pain_points(5)      # offset 1.5
    ce_big, pe_big = _pain_points(1000)       # offset 300
    assert ce_small == 22501.5 and pe_small == 22498.5   # tiny grid no longer overshoots
    assert ce_big == 22800.0 and pe_big == 22200.0       # huge grid no longer pins to the strike
    # offsets are in a clean 5 : 1000 ratio (strike-size-normalized).
    assert round((ce_big - 22500.0) / (ce_small - 22500.0), 6) == 200.0  # 300 / 1.5


def test_pain_point_honors_decimal_strike_size():
    # ITC / TATASTEEL trade on a 2.5-wide grid: offset = (15/50)*2.5 = 0.75 — a decimal strike size
    # must flow through unchanged (not truncated to 2).
    ce, pe = _pain_points(2.5)
    assert ce == 22500.75
    assert pe == 22499.25


def _rows_with_pain_walls() -> list[dict]:
    """Enriched rows with a clear max-OI strike per side + a pain_point on every row."""
    rows = _enriched_rows()
    for r in rows:
        r["pain_point"] = r["strike"] + (5.0 if r["option_type"] == "CE" else -5.0)
        if r["strike"] == 22700 and r["option_type"] == "CE":
            r["oi_lots"] = 100.0   # highest-call-OI strike
        if r["strike"] == 22300 and r["option_type"] == "PE":
            r["oi_lots"] = 100.0   # highest-put-OI strike
    return rows


def test_pain_point_walls_are_report_scalars():
    report = build_pair_report(_request(), _rows_with_pain_walls())
    assert report["levels"]["resistance_strong"] == 22705.0  # 22700 CE pain (+5)
    assert report["levels"]["support_strong"] == 22295.0     # 22300 PE pain (-5)


def test_pain_point_walls_recomputed_on_inflate():
    from analytics.report_builder import flatten_pair_report, inflate_pair_report

    report = build_pair_report(_request(), _rows_with_pain_walls())
    inflated = inflate_pair_report(flatten_pair_report(report), report["rows"], _request())
    assert inflated["levels"]["resistance_strong"] == 22705.0
    assert inflated["levels"]["support_strong"] == 22295.0


def test_vex_vol_regime_names_one_sided_chains_directly():
    # Both sides zero → no VEX at all.
    assert _vex_vol_regime(0.0, 0.0) is None
    # One-sided chains are named directly — a zero side can never invert the label
    # (the old ratio path sent an all-call chain into the < 0.7 bucket = "Put Vol Heavy").
    assert _vex_vol_regime(500.0, 0.0) == "Call Vol Heavy"
    assert _vex_vol_regime(0.0, 500.0) == "Put Vol Heavy"


def test_vex_vol_regime_two_sided_ratio_buckets_unchanged():
    assert _vex_vol_regime(200.0, 100.0) == "Call Vol Heavy"   # 2.0 > 1.5
    assert _vex_vol_regime(120.0, 100.0) == "Call Vol Biased"  # 1.2
    assert _vex_vol_regime(100.0, 100.0) == "Vol Balanced"     # 1.0
    assert _vex_vol_regime(85.0, 100.0) == "Put Vol Biased"    # 0.85
    assert _vex_vol_regime(60.0, 100.0) == "Put Vol Heavy"     # 0.6


def test_vex_vol_regime_divides_exactly_below_one():
    # Sub-1.0 put VEX must divide exactly — the old max(put_vex, 1.0) floor turned this
    # call-ratio-1.2 chain into "Put Vol Biased" (0.72 / 1.0 = 0.72 < 0.9).
    assert _vex_vol_regime(0.72, 0.6) == "Call Vol Biased"
