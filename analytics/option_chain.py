"""Option-chain parsing and enrichment."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Iterable, Mapping

IST = ZoneInfo("Asia/Kolkata")

from analytics.report_builder import (
    apply_pain_points,
    apply_risk_reward_ratios,
    black_scholes_price,
    time_to_expiry_years,
)
from domain.requests import RequestSpec


def _as_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_option_chain_response(
    option_chain_data: Mapping[str, Any] | None,
    asset_id: int,
    segment_code: str,
    expiry: str,
    underlying_price_override: float | None = None,
) -> list[dict[str, Any]]:
    """Parse the Dhan option-chain payload into normalized row dicts."""
    if not option_chain_data or not isinstance(option_chain_data, Mapping):
        return []

    option_chain = option_chain_data.get("oc", {})
    if not isinstance(option_chain, Mapping):
        return []

    raw_price = option_chain_data.get("last_price")
    underlying_price = (
        _as_number(underlying_price_override)
        if underlying_price_override is not None
        else _as_number(raw_price)
    )

    parsed_rows: list[dict[str, Any]] = []
    for strike_raw, strike_payload in option_chain.items():
        if not isinstance(strike_payload, Mapping):
            continue

        try:
            strike = float(strike_raw)
        except (TypeError, ValueError):
            continue

        for option_type, option_key in (("CE", "ce"), ("PE", "pe")):
            option_payload = strike_payload.get(option_key)
            if not isinstance(option_payload, Mapping) or not option_payload:
                continue

            greeks = option_payload.get("greeks", {}) or {}
            oi = int(_as_number(option_payload.get("oi")))
            previous_oi = int(_as_number(option_payload.get("previous_oi")))
            parsed_rows.append(
                {
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "last_price": _as_number(option_payload.get("last_price")),
                    "oi": oi,
                    "previous_oi": previous_oi,
                    "oi_change": oi - previous_oi,
                    "volume": int(_as_number(option_payload.get("volume"))),
                    "iv": _as_number(option_payload.get("implied_volatility")),
                    "delta": _as_number(greeks.get("delta")),
                    "gamma": _as_number(greeks.get("gamma")),
                    "theta": _as_number(greeks.get("theta")),
                    "vega": _as_number(greeks.get("vega")),
                    "bid_price": _as_number(option_payload.get("top_bid_price")),
                    "ask_price": _as_number(option_payload.get("top_ask_price")),
                    "underlying_price": underlying_price,
                    "underlying_asset_id": asset_id,
                    "segment_code": segment_code,
                }
            )

    return parsed_rows


def stamp_option_rows(
    rows: Iterable[Mapping[str, Any]],
    fetch_group_id: str,
    timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Add common persistence fields to normalized option rows."""
    resolved_timestamp = timestamp or datetime.now(IST).replace(tzinfo=None).isoformat()
    stamped_rows: list[dict[str, Any]] = []
    for row in rows:
        stamped_row = dict(row)
        stamped_row["timestamp"] = resolved_timestamp
        stamped_row["fetch_group_id"] = fetch_group_id
        stamped_rows.append(stamped_row)
    return stamped_rows


def build_enhanced_option_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    request_spec: RequestSpec,
    risk_free_rate: float | None,
    market_volatility: float | None,
    as_of: datetime | None = None,
    holidays=None,
) -> list[dict[str, Any]]:
    """Full per-contract enrichment, computed once at fetch on the FULL chain.

    Adds the display/exposure fields AND moneyness / bs_price / risk_reward / pain_point
    / straddle, so the stored enhanced rows carry every per-contract derived number
    (this is what the Options Analysis time-series and the Chain table read).

    Black-Scholes uses a single market volatility (``market_volatility``) for every
    contract — VIX for NSE, the configured commodity volatility for MCX. An option's own
    implied volatility is never used as the BS input (doing so would just reproduce the
    option's market price and make the theoretical price meaningless). ``bs_price`` is
    ``None`` when either ``risk_free_rate`` or ``market_volatility`` is ``None`` — no
    fabricated rate, no fabricated volatility, no fabricated price. ``as_of`` pins
    time-to-expiry to the capture moment (so playback reconstruction is historically
    correct, not recomputed against today).
    """
    enhanced: list[dict[str, Any]] = [dict(row) for row in rows]
    if not enhanced:
        return enhanced

    lot_size = max(1, int(request_spec.lot_size))
    underlying_price = float(enhanced[0].get("underlying_price", 0) or 0)
    # Strike size is the authoritative CSV spec (request_spec.strike_size), never inferred from the
    # live grid — a wide-spaced wing or a thin chain must not silently flip it and skew pain points.
    strike_size = float(request_spec.strike_size)
    market = "MCX" if request_spec.segment_code == "MCX_COMM" else "NSE"
    ttx = time_to_expiry_years(request_spec.expiry, market=market, as_of=as_of, holidays=holidays)
    can_price = risk_free_rate is not None and market_volatility is not None

    for row in enhanced:
        oi = int(row["oi"])
        volume = int(row["volume"])
        oi_change = int(row["oi_change"])
        previous_oi = int(row.get("previous_oi", 0) or 0)
        gamma = float(row["gamma"])
        vega = float(row["vega"])
        option_type = str(row["option_type"])
        strike = float(row["strike"])
        direction = 1.0 if option_type == "CE" else -1.0

        # Counts stored in lots; the dashboard reads these and just formats (no division).
        row["oi_lots"] = oi / lot_size
        row["previous_oi_lots"] = previous_oi / lot_size
        row["oi_change_lots"] = oi_change / lot_size
        row["volume_lots"] = volume / lot_size
        # VEX = vega · oi_lots (vega notional per vol point, lot-scaled like every other count).
        row["volatility_exposure"] = vega * oi / lot_size
        # GEX = dealer-signed gamma exposure on the unified ÷lot ×0.01 scale. Sign convention is
        # the mainstream/Perfiliev one: dealers long calls (+CE), short puts (−PE), so positive
        # net GEX = long gamma = Stabilizing. The unit is "delta-shares to hedge per 1% move"
        # (spot¹, Perfiliev "Contribution"-style) — NOT the spot² rupee dollar-gamma; the extra
        # spot factor is common to every strike at a given level, so it factors out and changes
        # neither the regime sign nor the zero-gamma flip location, only magnitude.
        row["gamma_exposure"] = direction * gamma * oi * underlying_price * 0.01 / lot_size
        row["moneyness_category"] = (
            "ITM"
            if (
                (option_type == "CE" and underlying_price > strike)
                or (option_type == "PE" and strike > underlying_price)
            )
            else "OTM"
        )
        if not can_price:
            row["bs_price"] = None
        else:
            row["bs_price"] = round(
                black_scholes_price(
                    spot=underlying_price,
                    strike=strike,
                    time_to_expiry=ttx,
                    rate=risk_free_rate,
                    volatility=market_volatility,
                    option_type=option_type,
                ),
                2,
            )

    # These read raw last_price / volume / oi / iv — still present on the working row.
    apply_risk_reward_ratios(enhanced)
    apply_pain_points(enhanced, strike_size)

    grouped: dict[float, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in enhanced:
        grouped[float(row["strike"])][str(row["option_type"])] = row
    for pair in grouped.values():
        straddle_raw = float((pair.get("CE") or {}).get("oi", 0) or 0) + float((pair.get("PE") or {}).get("oi", 0) or 0)
        for row in pair.values():
            row["straddle_oi_lots"] = straddle_raw / lot_size

    # Drop the raw counts and the pain-point intermediates — only lot-scaled + analytics
    # fields are stored. Everything in _OPTION_ROW_COLUMNS survives.
    for row in enhanced:
        for transient in ("oi", "volume", "oi_change", "previous_oi", "pain_point_ratio", "pain_point_iv_ratio"):
            row.pop(transient, None)

    return enhanced
