from __future__ import annotations

import json
from datetime import date

import pytest

from analytics.advanced_gex import (
    analyze_strike_across_expiries,
    build_comprehensive_advanced_analysis,
    calculate_gamma_profile,
    determine_market_regime,
    find_zero_gamma_level,
    group_rows_by_strike,
    option_gex,
)


def test_option_gex_sign_scale_no_threshold():
    # Scale: oi_lots * gamma * spot * 0.01, dealer-signed mainstream convention
    # (dealers long calls → +CE, short puts → -PE).
    assert option_gex(22500, 0.01, 1000, True) == pytest.approx(1000 * 0.01 * 22500 * 0.01)
    assert option_gex(22500, 0.01, 1000, False) == pytest.approx(-1000 * 0.01 * 22500 * 0.01)
    assert option_gex(22500, 0.01, 5, True) == pytest.approx(5 * 0.01 * 22500 * 0.01)  # small OI still computes (no cutoff)
    assert option_gex(22500, 0.0, 1000, True) == 0.0    # gamma 0
    assert option_gex(0, 0.01, 1000, True) == 0.0       # spot 0


def test_find_zero_gamma_interpolation_from_profile():
    # find_zero_gamma_level now reads the spot-level gamma profile (Perfiliev), not a strike axis.
    profile = {
        "spot_levels": [22400.0, 22500.0],
        "gex_values": [-100.0, 100.0],
        "current_total_gex": 100.0,
    }
    result = find_zero_gamma_level(profile)
    # 22500 - (100)*100/(100 - (-100)) = 22450 → a flip PRICE
    assert result["zero_gamma_strike"] == pytest.approx(22450.0)
    assert result["zero_crossings_count"] == 1


def test_find_zero_gamma_none_when_monotonic():
    profile = {"spot_levels": [22400.0, 22500.0], "gex_values": [50.0, 100.0], "current_total_gex": 100.0}
    assert find_zero_gamma_level(profile)["zero_gamma_strike"] is None


def test_market_regime_sign_only_and_distance():
    # Sign-only regime (no magnitude cutoffs): positive → Stabilizing, else Destabilizing.
    assert determine_market_regime(2_000_000, {"zero_gamma_strike": None}, 22500)["regime_name"] == "Stabilizing"
    assert determine_market_regime(0.5, {"zero_gamma_strike": None}, 22500)["regime_name"] == "Stabilizing"
    assert determine_market_regime(-0.5, {"zero_gamma_strike": None}, 22500)["regime_name"] == "Destabilizing"
    assert determine_market_regime(-2_000_000, {"zero_gamma_strike": None}, 22500)["regime_name"] == "Destabilizing"
    # No flip in range → distance is None (never fabricated); the removed vol-outlook / hedging /
    # key-levels keys are gone.
    no_flip = determine_market_regime(100, {"zero_gamma_strike": None}, 22500)
    assert no_flip["distance_to_zero_gamma_pts"] is None
    assert no_flip["distance_to_zero_gamma_pct"] is None
    assert "volatility_outlook" not in no_flip and "hedging_flow_direction" not in no_flip
    assert "key_trading_levels" not in no_flip
    # Signed distance: flip 225 pts above spot 22500 → +225 pts, +1.0%.
    near = determine_market_regime(100, {"zero_gamma_strike": 22725}, 22500)
    assert near["distance_to_zero_gamma_pts"] == pytest.approx(225.0)
    assert near["distance_to_zero_gamma_pct"] == pytest.approx(1.0)


def test_volatility_surface_per_side_bracketing_atm_iv():
    # ATM IV is per-side bracketing IV (skew-row primitive), not nearest-strike or full-range mean.
    analysis = build_comprehensive_advanced_analysis(
        _expiry_rows(), 22500.0, today=date(2026, 6, 1),
        rate=0.06, tte_by_expiry={"2026-06-26": 0.07, "2026-07-31": 0.16},
    )
    term = analysis["volatility_surface"]["term_structure"]
    assert term  # 2026-06-26 brackets spot (22400/22600); 2026-07-31 is one-sided → dropped
    item = term[0]
    assert item["atm_call_iv"] == pytest.approx(14.0)   # avg(14, 14)
    assert item["atm_put_iv"] == pytest.approx(15.5)     # avg(15, 16)
    assert "atm_iv" not in item                          # old nearest-strike single value gone
    sample = next(iter(analysis["volatility_surface"]["iv_by_expiry"].values()))
    assert "average_iv" not in sample                    # full-range mean removed
    assert {"atm_call_iv", "atm_put_iv", "days_to_expiry"} <= set(sample)


def _expiry_rows() -> dict[str, list[dict]]:
    return {
        "2026-06-26": [
            {"strike": 22400, "option_type": "CE", "oi_lots": 77, "gamma": 0.01, "iv": 14, "underlying_price": 22500},
            {"strike": 22400, "option_type": "PE", "oi_lots": 92, "gamma": 0.01, "iv": 15, "underlying_price": 22500},
            {"strike": 22600, "option_type": "CE", "oi_lots": 123, "gamma": 0.012, "iv": 14, "underlying_price": 22500},
            {"strike": 22600, "option_type": "PE", "oi_lots": 46, "gamma": 0.009, "iv": 16, "underlying_price": 22500},
        ],
        "2026-07-31": [
            {"strike": 22400, "option_type": "CE", "oi_lots": 30, "gamma": 0.006, "iv": 17, "underlying_price": 22500},
            {"strike": 22400, "option_type": "PE", "oi_lots": 38, "gamma": 0.006, "iv": 18, "underlying_price": 22500},
        ],
    }


def test_gamma_profile_recomputes_gamma_per_spot():
    # The profile must recompute each option's gamma via Black-Scholes at every spot level
    # (Perfiliev) — so the GEX series varies across levels rather than being a flat rescale.
    rows = _expiry_rows()
    grouped = group_rows_by_strike(rows)
    strike_analysis = {s: analyze_strike_across_expiries(s, be, 22500.0, date(2026, 6, 1)) for s, be in grouped.items()}
    profile = calculate_gamma_profile(
        strike_analysis, 22500.0, rate=0.06, tte_by_expiry={"2026-06-26": 0.07, "2026-07-31": 0.16}
    )
    assert "current_total_gex" in profile
    assert len(profile["spot_levels"]) == len(profile["gex_values"]) > 2
    assert len({round(v, 6) for v in profile["gex_values"]}) > 1  # not a flat constant series
    # the zero-gamma flip is read off this profile and is a spot price within the band
    flip = find_zero_gamma_level(profile)["zero_gamma_strike"]
    if flip is not None:
        assert min(profile["spot_levels"]) <= flip <= max(profile["spot_levels"])


def test_build_comprehensive_analysis_shape_and_json_safe():
    analysis = build_comprehensive_advanced_analysis(
        _expiry_rows(), 22500.0, today=date(2026, 6, 1),
        rate=0.06, tte_by_expiry={"2026-06-26": 0.07, "2026-07-31": 0.16},
    )
    assert "error" not in analysis
    for key in (
        "chart_data", "gamma_profile", "zero_gamma_analysis",
        "multi_expiry_summary", "market_interpretation", "time_analysis",
        "expiry_insights", "volatility_surface", "expiry_data",
    ):
        assert key in analysis
    assert "support_resistance" not in analysis  # dead S/R removed
    # intermediate float-keyed structure must NOT leak into the stored dict
    assert "strike_analysis" not in analysis
    # must be JSON-serializable (it lives in a dcc.Store)
    json.dumps(analysis)


def test_build_comprehensive_analysis_empty_inputs():
    assert build_comprehensive_advanced_analysis({}, 22500.0)["error"]
    assert build_comprehensive_advanced_analysis(_expiry_rows(), 0.0)["error"]
