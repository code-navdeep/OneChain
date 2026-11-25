"""Advanced multi-expiry GEX analytics — pure compute, no I/O.

Operates on plain row dicts (one per contract, the enriched rows from
``LiveFetchService``).

GEX SCALE: this module computes dealer-signed GEX as ``oi_lots * gamma * spot * 0.01``,
reading the lot-scaled ``oi_lots`` off the enriched rows. That is identical to the
app-wide ``gamma_exposure`` scale (``±·gamma·oi·spot·0.01 ÷ lot``), so Advanced's GEX
magnitudes are now comparable to the GEX shown elsewhere in the app.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping

import numpy as np

from analytics.report_builder import black_scholes_gamma, bracketing_atm_iv

DEFAULT_PRICE_RANGE = 0.15
DEFAULT_NUM_LEVELS = 50


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def moneyness(strike: float, spot: float) -> str:
    """Moneyness bucket for a strike vs spot."""
    if spot == 0:
        return "Unknown"
    ratio = strike / spot
    if ratio < 0.98:
        return "Deep OTM" if strike < spot else "Deep ITM"
    if ratio < 0.995:
        return "OTM" if strike < spot else "ITM"
    if ratio < 1.005:
        return "ATM"
    if ratio < 1.02:
        return "ITM" if strike < spot else "OTM"
    return "Deep ITM" if strike < spot else "Deep OTM"


def days_to_expiry(expiry_str: str, today: date | None = None) -> int:
    """Calendar days to expiry, floored at 0."""
    today = today or date.today()
    try:
        expiry_date = datetime.strptime(str(expiry_str), "%Y-%m-%d").date()
        return max(0, (expiry_date - today).days)
    except (TypeError, ValueError):
        return 0


def option_gex(spot: float, gamma: float, oi_lots: float, is_call: bool) -> float:
    """Dealer-signed per-option GEX on the app-wide ``oi_lots·gamma·spot·0.01`` scale.

    Mainstream/Perfiliev sign: dealers long calls (+CE), short puts (−PE)."""
    if gamma == 0 or spot <= 0:
        return 0.0
    base_gex = oi_lots * gamma * spot * 0.01
    return base_gex if is_call else -base_gex


def _dominant_expiry(expiry_analysis: Mapping[str, dict]) -> str:
    if not expiry_analysis:
        return "None"
    max_gex = 0.0
    dominant = "None"
    for expiry, data in expiry_analysis.items():
        abs_gex = abs(data["total_gex"])
        if abs_gex > max_gex:
            max_gex = abs_gex
            dominant = expiry
    return dominant


def _gex_concentration(expiry_analysis: Mapping[str, dict]) -> float:
    if not expiry_analysis:
        return 0.0
    gex_values = [abs(data["total_gex"]) for data in expiry_analysis.values()]
    total = sum(gex_values)
    return (max(gex_values) / total) if total > 0 else 0.0


# --------------------------------------------------------------------------- #
# Strike-centric grouping + per-strike analysis
# --------------------------------------------------------------------------- #

def group_rows_by_strike(expiry_rows: Mapping[str, list[dict]]) -> dict[float, dict[str, list[dict]]]:
    """Group rows into {strike: {expiry: [rows]}}."""
    grouped: dict[float, dict[str, list[dict]]] = {}
    for expiry, rows in expiry_rows.items():
        for row in rows:
            strike = _num(row.get("strike"))
            grouped.setdefault(strike, {}).setdefault(expiry, []).append(row)
    return grouped


def analyze_strike_across_expiries(
    strike: float,
    by_expiry: Mapping[str, list[dict]],
    spot: float,
    today: date | None = None,
) -> dict:
    """Aggregate one strike's GEX/OI across all expiries."""
    result: dict[str, Any] = {
        "strike": strike,
        "moneyness": moneyness(strike, spot),
        "expiry_analysis": {},
        "aggregated_gex": {"total_call_gex": 0.0, "total_put_gex": 0.0, "total_gex": 0.0},
        "expiry_count": len(by_expiry),
        "has_liquid_options": False,
    }
    total_call_oi = 0.0
    total_put_oi = 0.0

    for expiry, rows in by_expiry.items():
        calls = [r for r in rows if r.get("option_type") == "CE"]
        puts = [r for r in rows if r.get("option_type") == "PE"]

        call_gex = put_gex = 0.0
        call_oi = put_oi = 0.0
        call_volume = put_volume = 0.0
        call_gamma = put_gamma = 0.0
        call_iv = put_iv = 0.0

        if calls:
            call_row = calls[0]
            call_oi = _num(call_row.get("oi_lots"))
            call_volume = _num(call_row.get("volume_lots"))
            call_gamma = _num(call_row.get("gamma"))
            call_iv = _num(call_row.get("iv"))
            call_gex = option_gex(spot, call_gamma, call_oi, True)
            total_call_oi += call_oi
        if puts:
            put_row = puts[0]
            put_oi = _num(put_row.get("oi_lots"))
            put_volume = _num(put_row.get("volume_lots"))
            put_gamma = _num(put_row.get("gamma"))
            put_iv = _num(put_row.get("iv"))
            put_gex = option_gex(spot, put_gamma, put_oi, False)
            total_put_oi += put_oi

        is_liquid = (call_oi + put_oi) > 0
        if is_liquid:
            result["has_liquid_options"] = True

        expiry_total_gex = call_gex + put_gex
        result["expiry_analysis"][expiry] = {
            "call_gex": call_gex,
            "put_gex": put_gex,
            "total_gex": expiry_total_gex,
            "call_oi": call_oi,
            "put_oi": put_oi,
            "call_gamma": call_gamma,
            "put_gamma": put_gamma,
            "call_iv": call_iv,
            "put_iv": put_iv,
            "call_volume": call_volume,
            "put_volume": put_volume,
            "is_liquid": is_liquid,
            "days_to_expiry": days_to_expiry(expiry, today),
        }
        result["aggregated_gex"]["total_call_gex"] += call_gex
        result["aggregated_gex"]["total_put_gex"] += put_gex
        result["aggregated_gex"]["total_gex"] += expiry_total_gex

    result["summary"] = {
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "total_oi": total_call_oi + total_put_oi,
        "put_call_oi_ratio": total_put_oi / max(total_call_oi, 1),
        "dominant_expiry": _dominant_expiry(result["expiry_analysis"]),
        "gex_concentration": _gex_concentration(result["expiry_analysis"]),
    }
    return result


# --------------------------------------------------------------------------- #
# Zero-gamma, chart data, gamma profile, support/resistance, regime
# --------------------------------------------------------------------------- #

def find_zero_gamma_level(gamma_profile: Mapping[str, Any]) -> dict:
    """Zero-gamma flip level — interpolated spot PRICE where the gamma profile crosses zero.

    Perfiliev-correct: ``gamma_profile`` is net GEX vs hypothetical spot with each option's
    gamma recomputed at that spot (see ``calculate_gamma_profile``), so the first sign change
    is a true flip price (not a strike-axis artefact)."""
    spot_levels = list(gamma_profile.get("spot_levels", []))
    gex_values = list(gamma_profile.get("gex_values", []))
    if len(spot_levels) < 2 or len(gex_values) != len(spot_levels):
        return {
            "zero_gamma_strike": None,
            "analysis": "No gamma profile available",
            "regime": "Unknown",
            "recommendation": "Expand strike range to find zero gamma level",
        }

    zero_crossings = [i for i in range(len(gex_values) - 1) if gex_values[i] * gex_values[i + 1] < 0]
    if not zero_crossings:
        return {
            "zero_gamma_strike": None,
            "analysis": "No zero gamma level found in current price range",
            "regime": "Monotonic GEX (no flip point)",
            "recommendation": "Expand strike range to find zero gamma level",
        }

    idx = zero_crossings[0]
    spot_below, spot_above = spot_levels[idx], spot_levels[idx + 1]
    gex_below, gex_above = gex_values[idx], gex_values[idx + 1]
    if gex_above != gex_below:
        zero_gamma_strike = spot_above - (spot_above - spot_below) * gex_above / (gex_above - gex_below)
    else:
        zero_gamma_strike = (spot_below + spot_above) / 2

    current_gex = gamma_profile.get("current_total_gex")
    if current_gex is None:
        regime = "Unknown"
    else:
        regime = "Positive GEX (Stabilizing)" if current_gex > 0 else "Negative GEX (Destabilizing)"

    return {
        "zero_gamma_strike": zero_gamma_strike,
        "spot_below": spot_below,
        "spot_above": spot_above,
        "gex_below": gex_below,
        "gex_above": gex_above,
        "current_regime": regime,
        "zero_crossings_count": len(zero_crossings),
        "interpretation": {
            "above_zero_gamma": "Stabilizing regime - dealers provide liquidity",
            "below_zero_gamma": "Destabilizing regime - dealers amplify moves",
            "at_zero_gamma": "Neutral - minimal dealer hedging impact",
        },
    }


def create_comprehensive_chart_data(strike_analysis: Mapping[float, dict], expiry_list: list[str]) -> dict:
    """Build the per-strike chart-data block (GEX/OI series + per-expiry lines)."""
    strikes = sorted(strike_analysis.keys())
    chart_data: dict[str, Any] = {
        "strikes": strikes,
        "main_data": {"total_gex": [], "call_gex": [], "put_gex": []},
        "expiry_lines": {},
        "oi_data": {"total_oi": [], "call_oi": [], "put_oi": []},
        "liquidity_filter": [],
    }
    for strike in strikes:
        analysis = strike_analysis[strike]
        agg = analysis["aggregated_gex"]
        summary = analysis["summary"]
        chart_data["main_data"]["total_gex"].append(agg["total_gex"])
        chart_data["main_data"]["call_gex"].append(agg["total_call_gex"])
        chart_data["main_data"]["put_gex"].append(agg["total_put_gex"])
        chart_data["oi_data"]["total_oi"].append(summary["total_oi"])
        chart_data["oi_data"]["call_oi"].append(summary["total_call_oi"])
        chart_data["oi_data"]["put_oi"].append(summary["total_put_oi"])
        chart_data["liquidity_filter"].append(analysis["has_liquid_options"])

    for expiry in expiry_list:
        chart_data["expiry_lines"][expiry] = [
            strike_analysis[strike]["expiry_analysis"].get(expiry, {}).get("total_gex", 0.0)
            for strike in strikes
        ]

    chart_data["metadata"] = {
        "total_strikes": len(strikes),
        "liquid_strikes": sum(chart_data["liquidity_filter"]),
        "strike_range": [min(strikes), max(strikes)] if strikes else [0, 0],
        "expiry_count": len(expiry_list),
    }
    return chart_data


def calculate_gamma_profile(
    strike_analysis: Mapping[float, dict],
    spot: float,
    *,
    rate: float = 0.0,
    tte_by_expiry: Mapping[str, float] | None = None,
    price_range: float = DEFAULT_PRICE_RANGE,
    num_levels: int = DEFAULT_NUM_LEVELS,
) -> dict:
    """Gamma profile — net GEX across a band of hypothetical spot levels (Perfiliev).

    Each option's gamma is RECOMPUTED via Black-Scholes at every spot level (the broker
    gamma is only valid at the current spot). IV is held fixed (sticky-strike); only spot
    varies. ``rate`` defaults to 0.0 when the risk-free rate env is unset — gamma is nearly
    rate-insensitive, so this is a benign degradation, not a fabricated price."""
    tte_by_expiry = tte_by_expiry or {}
    price_levels = np.linspace(spot * (1 - price_range), spot * (1 + price_range), num_levels)
    profile_data = []
    for test_spot in price_levels:
        ts = float(test_spot)
        level_call_gex = level_put_gex = 0.0
        for strike, analysis in strike_analysis.items():
            for expiry, expiry_data in analysis["expiry_analysis"].items():
                tte = float(tte_by_expiry.get(expiry, 0.0))
                call_oi = expiry_data["call_oi"]
                put_oi = expiry_data["put_oi"]
                call_iv = expiry_data.get("call_iv", 0.0)
                put_iv = expiry_data.get("put_iv", 0.0)
                if call_oi > 0 and call_iv > 0:
                    g = black_scholes_gamma(spot=ts, strike=float(strike), time_to_expiry=tte, rate=rate, volatility=call_iv / 100.0)
                    level_call_gex += option_gex(ts, g, call_oi, True)
                if put_oi > 0 and put_iv > 0:
                    g = black_scholes_gamma(spot=ts, strike=float(strike), time_to_expiry=tte, rate=rate, volatility=put_iv / 100.0)
                    level_put_gex += option_gex(ts, g, put_oi, False)
        level_gex = level_call_gex + level_put_gex
        profile_data.append({
            "spot_level": ts,
            "total_gex": level_gex,
            "call_gex": level_call_gex,
            "put_gex": level_put_gex,
            "distance_pct": ((ts - spot) / spot) * 100 if spot else 0.0,
        })
    current_total_gex = 0.0
    if profile_data:
        current_total_gex = min(profile_data, key=lambda p: abs(p["spot_level"] - spot))["total_gex"]
    return {
        "profile_data": profile_data,
        "spot_levels": [float(p) for p in price_levels],
        "gex_values": [p["total_gex"] for p in profile_data],
        "current_total_gex": current_total_gex,
        "call_gex_values": [p["call_gex"] for p in profile_data],
        "put_gex_values": [p["put_gex"] for p in profile_data],
        "current_spot": spot,
        "price_range_pct": price_range * 100,
    }


def determine_market_regime(total_gex: float, zero_gamma_analysis: Mapping[str, Any], spot: float) -> dict:
    """Market regime + distance to the zero-gamma flip from total GEX.

    Sign-only regime — positive net GEX stabilizes, non-positive destabilizes (matches the
    app-wide 2-bucket pattern; no absolute magnitude cutoffs). The signed distance from spot
    to the zero-gamma flip is the honest, actionable read (how close the regime is to switching,
    and on which side); ``None`` when there is no flip in range — never fabricated. GEX sign is
    dealer-hedging *structure*, not a bullish/bearish call, so there is no directional "bias"
    (and GEX says nothing about the *level* of volatility, so there is no "volatility outlook").
    """
    zero_gamma_strike = zero_gamma_analysis.get("zero_gamma_strike")
    base_regime = "Stabilizing" if total_gex > 0 else "Destabilizing"

    if zero_gamma_strike and spot:
        distance_pts = zero_gamma_strike - spot
        distance_pct = distance_pts / spot * 100
        distance_pts = round(distance_pts, 2)
        distance_pct = round(distance_pct, 2)
    else:
        distance_pts = None
        distance_pct = None

    return {
        "regime_name": base_regime,
        "current_gex_regime": base_regime,
        "distance_to_zero_gamma_pts": distance_pts,
        "distance_to_zero_gamma_pct": distance_pct,
    }


def _time_decay_analysis(strike_analysis: Mapping[float, dict], expiry_list: list[str], today: date | None = None) -> dict:
    """Per-expiry time-decay analysis (weights, decay impact, concentration)."""
    time_analysis: dict[str, Any] = {"decay_impact": {}, "concentration_by_expiry": {}}
    total_gex_by_expiry: dict[str, float] = {}

    for expiry in expiry_list:
        total_gex = 0.0
        strike_count = 0
        for strike_data in strike_analysis.values():
            if expiry in strike_data["expiry_analysis"]:
                total_gex += strike_data["expiry_analysis"][expiry]["total_gex"]
                strike_count += 1
        total_gex_by_expiry[expiry] = total_gex
        dte = days_to_expiry(expiry, today)
        time_analysis["decay_impact"][expiry] = {
            "days_to_expiry": dte,
            "total_gex": total_gex,
            "gex_per_day": total_gex / max(dte, 1),
            "active_strikes": strike_count,
        }

    total_abs = sum(abs(g) for g in total_gex_by_expiry.values())
    for expiry, gex in total_gex_by_expiry.items():
        time_analysis["concentration_by_expiry"][expiry] = (abs(gex) / total_abs) if total_abs > 0 else 0.0
    return time_analysis


def _assess_data_quality(strike_analysis: Mapping[float, dict]) -> dict:
    """Assess data completeness + liquidity coverage."""
    total_strikes = len(strike_analysis)
    liquid_strikes = sum(1 for s in strike_analysis.values() if s["has_liquid_options"])

    total_combinations = 0
    complete_combinations = 0
    for strike_data in strike_analysis.values():
        total_combinations += len(strike_data["expiry_analysis"])
        for expiry_data in strike_data["expiry_analysis"].values():
            if expiry_data["call_oi"] > 0 and expiry_data["put_oi"] > 0:
                complete_combinations += 1

    completeness = complete_combinations / total_combinations if total_combinations > 0 else 0
    liquidity_ratio = liquid_strikes / total_strikes if total_strikes > 0 else 0

    if completeness > 0.8 and liquidity_ratio > 0.6:
        rating, notes = "High", "Comprehensive data with good liquidity coverage"
    elif completeness > 0.6 and liquidity_ratio > 0.4:
        rating, notes = "Good", "Adequate data quality for reliable analysis"
    elif completeness > 0.4:
        rating, notes = "Fair", "Limited data - results should be interpreted cautiously"
    else:
        rating, notes = "Poor", "Insufficient data for reliable analysis"

    return {
        "quality_rating": rating,
        "quality_notes": notes,
        "data_completeness": completeness,
        "liquidity_ratio": liquidity_ratio,
        "total_strikes": total_strikes,
        "liquid_strikes": liquid_strikes,
        "total_combinations": total_combinations,
        "complete_combinations": complete_combinations,
    }


def calculate_comprehensive_gex(
    expiry_rows: Mapping[str, list[dict]],
    spot: float,
    today: date | None = None,
    *,
    rate: float = 0.0,
    tte_by_expiry: Mapping[str, float] | None = None,
) -> dict:
    """Orchestrator — full comprehensive multi-expiry GEX analysis."""
    if not expiry_rows:
        return {"error": "No expiry data provided"}

    grouped = group_rows_by_strike(expiry_rows)
    if not grouped:
        return {"error": "No common strikes found across expiries"}

    strike_analysis: dict[float, dict] = {}
    total_call_gex = total_put_gex = total_gex = 0.0
    for strike, by_expiry in grouped.items():
        analysis = analyze_strike_across_expiries(strike, by_expiry, spot, today)
        strike_analysis[strike] = analysis
        agg = analysis["aggregated_gex"]
        total_call_gex += agg["total_call_gex"]
        total_put_gex += agg["total_put_gex"]
        total_gex += agg["total_gex"]

    expiry_list = list(expiry_rows.keys())
    chart_data = create_comprehensive_chart_data(strike_analysis, expiry_list)
    # Perfiliev zero-gamma: build the BS-recomputed spot-level profile first, then read the
    # flip price off it (not off a strike-axis sign change).
    gamma_profile = calculate_gamma_profile(strike_analysis, spot, rate=rate, tte_by_expiry=tte_by_expiry)
    zero_gamma_analysis = find_zero_gamma_level(gamma_profile)
    market_regime = determine_market_regime(total_gex, zero_gamma_analysis, spot)
    time_analysis = _time_decay_analysis(strike_analysis, expiry_list, today)

    return {
        "strike_analysis": strike_analysis,
        "chart_data": chart_data,
        "gamma_profile": gamma_profile,
        "zero_gamma_analysis": zero_gamma_analysis,
        "multi_expiry_summary": {
            "total_call_gex": total_call_gex,
            "total_put_gex": total_put_gex,
            "total_gex": total_gex,
            "current_spot": spot,
            "active_expiries": len(expiry_rows),
            "active_strikes": len(strike_analysis),
            "liquid_strikes": sum(1 for s in strike_analysis.values() if s["has_liquid_options"]),
            "analysis_framework": "Strike-based multi-expiry analysis",
            "zero_gamma_level": zero_gamma_analysis.get("zero_gamma_strike"),
            "current_regime": market_regime["regime_name"],
        },
        "market_interpretation": market_regime,
        "time_analysis": time_analysis,
        "analysis_metadata": {
            "analysis_type": "comprehensive_strike_based_multi_expiry",
            "underlying_price": spot,
            "total_records_processed": sum(len(rows) for rows in expiry_rows.values()),
            "methodology": "Each strike analyzed across all expiries for true multi-expiry insights",
            "data_quality": _assess_data_quality(strike_analysis),
        },
    }


# --------------------------------------------------------------------------- #
# Engine-level enhancements (volatility surface, time analysis, rollover)
# --------------------------------------------------------------------------- #

def analyze_volatility_surface(expiry_rows: Mapping[str, list[dict]], spot: float, today: date | None = None) -> dict:
    """Per-expiry per-side bracketing ATM IV + term structure.

    ATM IV is the bracketing-strike ATM IV per side (``bracketing_atm_iv`` — the same primitive
    the live skew row and the snapshot CM-30 series use), never the nearest single strike, and
    never the wing-polluted full-range mean. A side without a bracketing pair (one-sided chain)
    is ``None`` — never fabricated."""
    vol: dict[str, Any] = {"iv_by_expiry": {}, "term_structure": []}
    for expiry, rows in expiry_rows.items():
        call_rows = [r for r in rows if r.get("option_type") == "CE"]
        put_rows = [r for r in rows if r.get("option_type") == "PE"]
        atm_call = bracketing_atm_iv(call_rows, spot=spot, option_type="CE") if spot > 0 else None
        atm_put = bracketing_atm_iv(put_rows, spot=spot, option_type="PE") if spot > 0 else None
        if atm_call is None and atm_put is None:
            continue
        atm_call = round(atm_call, 2) if atm_call is not None else None
        atm_put = round(atm_put, 2) if atm_put is not None else None
        dte = days_to_expiry(expiry, today)
        vol["iv_by_expiry"][expiry] = {
            "atm_call_iv": atm_call,
            "atm_put_iv": atm_put,
            "days_to_expiry": dte,
        }
        vol["term_structure"].append({
            "expiry": expiry,
            "days": dte,
            "atm_call_iv": atm_call,
            "atm_put_iv": atm_put,
        })
    vol["term_structure"].sort(key=lambda x: x["days"])
    return vol


def calculate_rollover_pressure(time_analysis: Mapping[str, Any]) -> dict:
    """Rollover pressure per expiry from time-decay GEX."""
    decay_impact = time_analysis.get("decay_impact", {})
    if not decay_impact:
        return {"rollover_pressure_by_expiry": {}}
    pressures: dict[str, dict] = {}
    for expiry, gex_data in decay_impact.items():
        total_gex = gex_data.get("total_gex", 0)
        pressures[expiry] = {
            "pressure": round(abs(total_gex) / 1_000_000 if total_gex != 0 else 0, 2),
            "direction": "positive" if total_gex > 0 else "negative",
            "magnitude": abs(total_gex) / 1_000_000 if total_gex != 0 else 0,
        }
    return {
        "rollover_pressure_by_expiry": pressures,
        "average_pressure": float(np.mean([p["pressure"] for p in pressures.values()])) if pressures else 0,
    }


def term_structure_slope_dispersion(days: list[float], values: list[float]) -> dict:
    """Term-structure slope + dispersion."""
    if len(days) < 3:
        return {}
    return {"slope": float(np.polyfit(days, values, 1)[0]), "dispersion": float(np.std(values))}


def _compact_expiry_data(expiry_rows: Mapping[str, list[dict]]) -> dict[str, list[dict]]:
    """JSON-safe per-expiry rows the renderer needs (3D surface IVs + heatmap OI)."""
    compact: dict[str, list[dict]] = {}
    for expiry, rows in expiry_rows.items():
        compact[expiry] = [
            {
                "strike": _num(r.get("strike")),
                "option_type": str(r.get("option_type")),
                "iv": _num(r.get("iv")),
                "oi": _num(r.get("oi_lots")),
                "oi_display": _num(r.get("oi_lots")),
                "gamma": _num(r.get("gamma")),
            }
            for r in rows
        ]
    return compact


def build_comprehensive_advanced_analysis(
    expiry_rows: Mapping[str, list[dict]],
    spot: float,
    today: date | None = None,
    *,
    rate: float = 0.0,
    tte_by_expiry: Mapping[str, float] | None = None,
) -> dict:
    """Public entry point → JSON-safe analysis dict for the store + renderer.

    Runs the comprehensive GEX analysis (which includes ``time_analysis`` via
    ``_time_decay_analysis``), then the engine-level enhancements
    (expiry_insights, volatility_surface) and attaches a compact
    per-expiry data block. The intermediate float-keyed ``strike_analysis`` is
    dropped (not needed by the renderer and not JSON-key-safe). ``rate`` +
    ``tte_by_expiry`` feed the Perfiliev zero-gamma profile (Black-Scholes gamma
    recomputation); the caller (``AdvancedAnalysisService``) supplies both.
    """
    if not expiry_rows or spot <= 0:
        return {"error": "Insufficient data for advanced analysis"}

    gex = calculate_comprehensive_gex(expiry_rows, spot, today, rate=rate, tte_by_expiry=tte_by_expiry)
    if "error" in gex:
        return gex

    expiry_insights: dict[str, Any] = {}
    for expiry, rows in expiry_rows.items():
        if not rows:
            continue
        ivs = [_num(r.get("iv")) for r in rows]
        expiry_insights[expiry] = {
            "total_oi": int(sum(_num(r.get("oi_lots")) for r in rows)),
            "average_iv": round(float(np.mean(ivs)) if ivs else 0.0, 2),
            "strike_count": len({_num(r.get("strike")) for r in rows}),
            "days_to_expiry": days_to_expiry(expiry, today),
        }

    result = {k: v for k, v in gex.items() if k != "strike_analysis"}
    result["expiry_insights"] = expiry_insights
    result["volatility_surface"] = analyze_volatility_surface(expiry_rows, spot, today)
    # "time_analysis" carries over from calculate_comprehensive_gex (_time_decay_analysis) —
    # the single per-expiry decay source (the old create_time_analysis duplicate was removed).
    result["expiry_data"] = _compact_expiry_data(expiry_rows)
    return result
