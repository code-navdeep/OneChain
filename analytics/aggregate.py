"""Aggregate-mode helpers for single-instrument multi-expiry views."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from state.models import LoadedPairState, LoadedReportState


def is_aggregate_eligible(loaded_report_state: LoadedReportState | None) -> bool:
    if loaded_report_state is None or len(loaded_report_state.loaded_pairs) <= 1:
        return False
    instrument_keys = {item.request.instrument_key.value for item in loaded_report_state.loaded_pairs}
    return len(instrument_keys) == 1


def get_aggregate_view_key(loaded_report_state: LoadedReportState) -> str:
    instrument_key = loaded_report_state.loaded_pairs[0].request.instrument_key.value
    return f"aggregate|{instrument_key}"


def build_aggregate_report(loaded_pairs: list[LoadedPairState]) -> dict[str, Any] | None:
    if len(loaded_pairs) <= 1:
        return None
    instrument_keys = {item.request.instrument_key.value for item in loaded_pairs}
    if len(instrument_keys) != 1:
        return None

    aggregate_rows: dict[tuple[float, str], dict[str, Any]] = {}
    call_oi = 0.0
    put_oi = 0.0
    call_volume = 0.0
    put_volume = 0.0
    call_oi_change = 0.0
    put_oi_change = 0.0
    call_vex = 0.0
    put_vex = 0.0
    call_gex = 0.0
    put_gex = 0.0
    total_gamma_exposure = 0.0
    total_volatility_exposure = 0.0
    underlying_price = float(loaded_pairs[0].report.get("underlying_price", 0) or 0)

    for loaded_pair in loaded_pairs:
        for row in loaded_pair.report.get("rows", []):
            strike = float(row.get("strike", 0) or 0)
            option_type = str(row.get("option_type", ""))
            key = (strike, option_type)
            target = aggregate_rows.setdefault(
                key,
                {
                    "strike": strike,
                    "option_type": option_type,
                    "last_price": None,
                    "oi_lots": 0.0,
                    "previous_oi_lots": 0.0,
                    "oi_change_lots": 0.0,
                    "volume_lots": 0.0,
                    "straddle_oi_lots": 0.0,
                    "iv": None,
                    "delta": None,
                    "gamma": None,
                    "theta": None,
                    "vega": None,
                    "gamma_exposure": 0.0,
                    "volatility_exposure": 0.0,
                    "bs_price": None,
                    "moneyness_category": None,
                    "risk_reward_ratio": None,
                    "pain_point": None,
                    "underlying_price": underlying_price,
                },
            )
            target["oi_lots"] += float(row.get("oi_lots", 0) or 0)
            target["previous_oi_lots"] += float(row.get("previous_oi_lots", 0) or 0)
            target["oi_change_lots"] += float(row.get("oi_change_lots", 0) or 0)
            target["volume_lots"] += float(row.get("volume_lots", 0) or 0)
            target["gamma_exposure"] += float(row.get("gamma_exposure", 0) or 0)
            target["volatility_exposure"] += float(row.get("volatility_exposure", 0) or 0)

    for row in aggregate_rows.values():
        if row["option_type"] == "CE":
            call_oi += float(row["oi_lots"])
            call_volume += float(row["volume_lots"])
            call_oi_change += float(row["oi_change_lots"])
            call_vex += float(row["volatility_exposure"])
            call_gex += float(row["gamma_exposure"])
        elif row["option_type"] == "PE":
            put_oi += float(row["oi_lots"])
            put_volume += float(row["volume_lots"])
            put_oi_change += float(row["oi_change_lots"])
            put_vex += float(row["volatility_exposure"])
            put_gex += float(row["gamma_exposure"])
        total_gamma_exposure += float(row["gamma_exposure"])
        total_volatility_exposure += float(row["volatility_exposure"])

    call_rows = [row for row in aggregate_rows.values() if row["option_type"] == "CE"]
    put_rows = [row for row in aggregate_rows.values() if row["option_type"] == "PE"]
    max_call_oi_strike = max(call_rows, key=lambda row: row["oi_lots"])["strike"] if call_rows else None
    max_put_oi_strike = max(put_rows, key=lambda row: row["oi_lots"])["strike"] if put_rows else None

    first_pair = loaded_pairs[0]
    aggregate_view_key = f"aggregate|{first_pair.request.instrument_key.value}"
    pair_count = len(loaded_pairs)
    return {
        "pair_key": aggregate_view_key,
        "instrument_key": first_pair.request.instrument_key.value,
        "display_name": first_pair.request.display_name,
        "expiry": "Aggregate",
        "lot_size": first_pair.request.lot_size,
        "strike_size": first_pair.request.strike_size,
        "underlying_price": underlying_price,
        "row_count": len(aggregate_rows),
        "aggregate_pair_count": pair_count,
        "aggregate_member_pairs": [item.request.pair_key.value for item in loaded_pairs],
        "totals": {
            "call_oi": call_oi,
            "put_oi": put_oi,
            "total_oi": call_oi + put_oi,
            "call_volume": call_volume,
            "put_volume": put_volume,
            "total_volume": call_volume + put_volume,
            "call_oi_change": call_oi_change,
            "put_oi_change": put_oi_change,
            "total_oi_change": call_oi_change + put_oi_change,
            "put_call_ratio": (put_oi / call_oi) if call_oi else None,
            "total_gamma_exposure": total_gamma_exposure,
            "total_volatility_exposure": total_volatility_exposure,
            "call_vex": call_vex,
            "put_vex": put_vex,
        },
        "levels": {
            "max_call_oi_strike": max_call_oi_strike,
            "max_put_oi_strike": max_put_oi_strike,
        },
        # GEX values are additive across expiries; regime/VEX/greeks need raw greeks
        # (None on aggregate rows) and are intentionally not synthesized here.
        "gex_analysis": {
            "total_gex": total_gamma_exposure,
            "call_gex": call_gex,
            "put_gex": put_gex,
            "market_regime": None,
        },
        "rows": sorted(aggregate_rows.values(), key=lambda row: (float(row["strike"]), str(row["option_type"]))),
    }


def build_aggregate_iv_overlay(loaded_pairs: list[LoadedPairState]) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    for loaded_pair in loaded_pairs:
        strike_points: dict[float, list[float]] = defaultdict(list)
        for row in loaded_pair.report.get("rows", []):
            iv_value = row.get("iv")
            if iv_value in (None, ""):
                continue
            strike_points[float(row["strike"])].append(float(iv_value))
        if not strike_points:
            continue
        strikes = sorted(strike_points)
        overlays.append(
            {
                "label": loaded_pair.request.expiry,
                "x": strikes,
                "y": [
                    sum(strike_points[strike]) / len(strike_points[strike])
                    for strike in strikes
                ],
            }
        )
    return overlays
