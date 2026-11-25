"""Live reporting helpers."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from math import erf, exp, log, pi, sqrt
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from domain.requests import RequestSpec


IST = ZoneInfo("Asia/Kolkata")

# Reference strike size the raw pain-point signal is calibrated against. The pain-point offset is
# `(signal / _PAIN_REFERENCE_STRIKE_SIZE) * strike_size` — i.e. the signal is read as a fraction of
# this reference step, then re-scaled to the asset's own grid. 50 reproduces the trusted output on
# NSE-class 50-wide grids while staying correct on tiny (5–10) and huge (500–1000) strike sizes.
_PAIN_REFERENCE_STRIKE_SIZE = 50.0


def _as_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _split_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    call_rows: list[dict[str, Any]] = []
    put_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        if payload.get("option_type") == "CE":
            call_rows.append(payload)
        elif payload.get("option_type") == "PE":
            put_rows.append(payload)
    return call_rows, put_rows


def _sum_metric(rows: Iterable[Mapping[str, Any]], key: str) -> float:
    return float(sum(_as_float(row.get(key, 0)) for row in rows))


def _max_oi_strike(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    top_row = max(rows, key=lambda row: _as_float(row.get("oi_lots", 0)))
    return float(top_row.get("strike", 0))


def _max_volume_strike(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    top_row = max(rows, key=lambda row: _as_float(row.get("volume_lots", 0)))
    return float(top_row.get("strike", 0))


def _pain_of_max_oi(rows: list[dict[str, Any]]) -> float | None:
    """The ``pain_point`` of the highest-``oi_lots`` strike on one side (full chain), or ``None``.

    Backs the Tier-2 pain-point walls: ``resistance_strong`` (call side) / ``support_strong`` (put).
    A report scalar so the spot-cross alert stays meta-only; recomputed on inflate (rows carry
    ``pain_point``), like ``pinning``."""
    if not rows:
        return None
    top_row = max(rows, key=lambda row: _as_float(row.get("oi_lots", 0)))
    pain = top_row.get("pain_point")
    return float(pain) if pain not in (None, 0) else None


def _nth_strike_by(rows: list[dict[str, Any]], *, column: str, rank: int) -> float | None:
    """Strike of the row at 0-based ``rank`` when one side's rows are ordered by ``column`` desc
    (current value). ``None`` when there are fewer than ``rank + 1`` rows. Strikes are unique per
    side, so ranking the rows ranks the strikes."""
    if len(rows) <= rank:
        return None
    ordered = sorted(rows, key=lambda row: _as_float(row.get(column, 0)), reverse=True)
    return float(ordered[rank].get("strike", 0))


def _second_max_oi_strike(rows: list[dict[str, Any]]) -> float | None:
    return _nth_strike_by(rows, column="oi_lots", rank=1)


def _second_max_volume_strike(rows: list[dict[str, Any]]) -> float | None:
    return _nth_strike_by(rows, column="volume_lots", rank=1)


def _count_trading_days(start_date, end_date, holidays=None) -> int:
    if start_date >= end_date:
        return 0
    holiday_set = holidays or frozenset()
    current = start_date + timedelta(days=1)
    trading_days = 0
    while current <= end_date:
        if current.weekday() < 5 and current not in holiday_set:
            trading_days += 1
        current += timedelta(days=1)
    return trading_days


def time_to_expiry_years(
    expiry: str, market: str = "NSE", *, as_of: datetime | None = None, holidays=None
) -> float:
    try:
        expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        return 0.0

    reference = as_of.astimezone(IST) if as_of is not None else datetime.now(IST)
    now = reference.replace(tzinfo=None)
    if expiry_dt.date() == now.date():
        market_hours = {
            "NSE": {"start": 9.25, "end": 15.5},
            "MCX": {"start": 9.0, "end": 23.5},
        }
        hours = market_hours.get(market, market_hours["NSE"])
        current_hour = now.hour + now.minute / 60.0
        market_end = hours["end"]
        if current_hour >= market_end:
            return 0.0
        remaining_hours = market_end - max(current_hour, hours["start"])
        annual_trading_hours = 252 * (hours["end"] - hours["start"])
        return max(0.0, remaining_hours / annual_trading_hours)

    trading_days = _count_trading_days(now.date(), expiry_dt.date(), holidays)
    return trading_days / 252.0


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def black_scholes_price(
    *,
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    volatility: float,
    option_type: str,
) -> float:
    if time_to_expiry <= 0:
        if option_type == "CE":
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)

    if volatility <= 0:
        discount_factor = exp(-rate * time_to_expiry)
        if option_type == "CE":
            return max(0.0, spot - strike * discount_factor)
        return max(0.0, strike * discount_factor - spot)

    d1 = (log(spot / strike) + (rate + 0.5 * volatility**2) * time_to_expiry) / (volatility * sqrt(time_to_expiry))
    d2 = d1 - volatility * sqrt(time_to_expiry)
    if option_type == "CE":
        return max(0.0, (spot * _normal_cdf(d1)) - (strike * exp(-rate * time_to_expiry) * _normal_cdf(d2)))
    return max(0.0, (strike * exp(-rate * time_to_expiry) * _normal_cdf(-d2)) - (spot * _normal_cdf(-d1)))


def black_scholes_gamma(
    *,
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    volatility: float,
) -> float:
    """Black-Scholes gamma φ(d1) / (spot·σ·√T).

    Needed to recompute an option's gamma at a *hypothetical* spot for the
    Perfiliev zero-gamma profile (the broker only gives gamma at the current spot).
    IV is held fixed (sticky-strike). Returns ``0.0`` for any degenerate input
    (``T ≤ 0``, ``σ ≤ 0``, ``spot ≤ 0``, ``strike ≤ 0``)."""
    if time_to_expiry <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (log(spot / strike) + (rate + 0.5 * volatility**2) * time_to_expiry) / (volatility * sqrt(time_to_expiry))
    pdf = exp(-0.5 * d1 * d1) / sqrt(2.0 * pi)
    return pdf / (spot * volatility * sqrt(time_to_expiry))


def apply_risk_reward_ratios(rows: list[dict[str, Any]]) -> None:
    for option_type in ("CE", "PE"):
        option_rows = sorted(
            [row for row in rows if row.get("option_type") == option_type],
            key=lambda row: _as_float(row.get("strike", 0)),
        )
        strikes = [float(row.get("strike", 0)) for row in option_rows]
        for index, row in enumerate(option_rows):
            current_price = _as_float(row.get("last_price", 0))
            if current_price <= 0:
                row["risk_reward_ratio"] = None
                continue

            risk_price = 0.0
            reward_price = 0.0
            if option_type == "CE":
                if index > 0:
                    reward_price = _as_float(option_rows[index - 1].get("last_price", 0)) - current_price
                if index < len(strikes) - 1:
                    risk_price = current_price - _as_float(option_rows[index + 1].get("last_price", 0))
            else:
                if index > 0:
                    risk_price = current_price - _as_float(option_rows[index - 1].get("last_price", 0))
                if index < len(strikes) - 1:
                    reward_price = _as_float(option_rows[index + 1].get("last_price", 0)) - current_price

            row["risk_reward_ratio"] = round(risk_price / reward_price, 2) if reward_price > 0 and risk_price > 0 else None


def apply_pain_points(rows: list[dict[str, Any]], strike_size: float) -> None:
    grouped: dict[float, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[float(row.get("strike", 0))][str(row.get("option_type", ""))] = row

    for strike, pair in grouped.items():
        call_row = pair.get("CE")
        put_row = pair.get("PE")
        call_iv = _as_float((call_row or {}).get("iv"))
        put_iv = _as_float((put_row or {}).get("iv"))
        iv_ratio = (call_iv / put_iv) if put_iv else 0.0

        for row in pair.values():
            scaled_volume = _as_float(row.get("volume", 0)) / 1000.0
            scaled_oi = _as_float(row.get("oi", 0)) / 1000.0
            ratio = (scaled_volume / scaled_oi) if scaled_oi else 0.0
            row["pain_point_ratio"] = ratio
            row["pain_point_iv_ratio"] = iv_ratio
            # Offset = (signal / reference step) * this asset's strike_size — a fixed fraction of one
            # strike step, so it scales with the grid. CE pulls the pain above the strike, PE below
            # (the `±`); symmetric — no extra half-step floor. Uncapped: a large signal (heavy
            # turnover) is a legitimate read and produces a proportionally farther pain point.
            if row.get("option_type") == "CE":
                signal = ratio * iv_ratio
                row["pain_point"] = round(strike + (signal / _PAIN_REFERENCE_STRIKE_SIZE) * strike_size, 2)
            else:
                signal = ratio * (1 / iv_ratio) if iv_ratio else 0.0
                row["pain_point"] = round(strike - (signal / _PAIN_REFERENCE_STRIKE_SIZE) * strike_size, 2)


def _calculate_max_pain(rows: list[dict[str, Any]]) -> float:
    grouped: dict[float, dict[str, float]] = defaultdict(lambda: {"CE": 0.0, "PE": 0.0})
    for row in rows:
        strike = float(row.get("strike", 0) or 0)
        grouped[strike][str(row.get("option_type", ""))] += _as_float(row.get("oi_lots", 0))
    strikes = sorted(grouped)
    if not strikes:
        return 0.0

    min_pain = None
    min_strike = strikes[0]
    for target in strikes:
        payout = 0.0
        for strike in strikes:
            payout += max(0.0, target - strike) * grouped[strike]["CE"]
            payout += max(0.0, strike - target) * grouped[strike]["PE"]
        if min_pain is None or payout < min_pain:
            min_pain = payout
            min_strike = target
    return float(min_strike)


def _interpolate_iv_at_delta(points: list[tuple[float, float]], target: float) -> float | None:
    """Linear-in-delta IV interpolation.

    ``points`` is a list of ``(delta, iv)`` with ``delta > 0`` (one side of the smile;
    puts use ``abs(delta)``). Returns the IV at ``target`` delta by linearly interpolating
    between the two bracketing points, or ``None`` when ``target`` falls outside the
    available delta range (chain too narrow / illiquid wings — never fabricate a wing).
    """
    if len(points) < 2:
        return None
    pts = sorted(points, key=lambda item: item[0])
    if target < pts[0][0] or target > pts[-1][0]:
        return None
    for delta, iv in pts:
        if delta == target:
            return iv
    for (d0, iv0), (d1, iv1) in zip(pts, pts[1:]):
        if d0 <= target <= d1 and d1 > d0:
            return iv0 + (target - d0) / (d1 - d0) * (iv1 - iv0)
    return None


def compute_risk_reversal_25(rows: list[dict[str, Any]], *, target_delta: float = 0.25) -> dict[str, float | None]:
    """25-delta risk reversal: ``IV(25Δ call) − IV(25Δ put)``.

    Interpolates each side's IV at ``target_delta`` in delta-space (linear-in-delta).
    Puts use ``abs(delta)`` so the result is correct regardless of whether the broker
    reports put deltas as negative or as magnitude. Only rows with ``iv > 0`` and a
    positive (abs) delta count. Any leg that cannot reach ``target_delta`` is ``None``,
    and ``rr_25`` is ``None`` when either leg is missing.
    """
    call_points = [
        (_as_float(row.get("delta")), _as_float(row.get("iv")))
        for row in rows
        if row.get("option_type") == "CE" and _as_float(row.get("iv")) > 0 and _as_float(row.get("delta")) > 0
    ]
    put_points = [
        (abs(_as_float(row.get("delta"))), _as_float(row.get("iv")))
        for row in rows
        if row.get("option_type") == "PE" and _as_float(row.get("iv")) > 0 and abs(_as_float(row.get("delta"))) > 0
    ]
    call_iv = _interpolate_iv_at_delta(call_points, target_delta)
    put_iv = _interpolate_iv_at_delta(put_points, target_delta)
    rr_25 = round(call_iv - put_iv, 2) if (call_iv is not None and put_iv is not None) else None
    return {
        "call_iv_25d": round(call_iv, 2) if call_iv is not None else None,
        "put_iv_25d": round(put_iv, 2) if put_iv is not None else None,
        "rr_25": rr_25,
    }


def risk_reversal_label(rr_25: float | None) -> str | None:
    """Factual skew tag from the RR sign — no directional (bullish/bearish) call."""
    if rr_25 is None:
        return None
    if rr_25 > 0:
        return "Call skew"
    if rr_25 < 0:
        return "Put skew"
    return "Flat"


def bracketing_atm_iv(
    rows_for_type: Iterable[Mapping[str, Any]],
    *,
    spot: float,
    option_type: str,
) -> float | None:
    """Bracketing-strike ATM IV for one option type (the shared parity-skew primitive).

    NB: *not* a straddle (a straddle is a call + put at the SAME strike). It averages **one
    side's** two strikes bracketing spot — e.g. for calls, ``avg(call_IV@below, call_IV@above)``
    — and never mixes call with put. Pass one side's rows at a time (keyed by strike internally).

    Rules:
    - Spot strictly between two strikes: average the IVs of the nearest strike below and above.
    - Spot exactly on a strike K: calls average K with the immediate higher strike; puts with
      the immediate lower.
    - One-sided (the required neighbour does not exist): ``None``.

    Used by both the live ATM parity gap (``compute_atm_iv_gap``) and the snapshot CM-30 IV
    series (``analytics/historical_metrics``), so the two never diverge.
    """
    by_strike: dict[float, float] = {}
    for row in rows_for_type:
        iv = _as_float(row.get("iv", 0))
        if iv > 0:
            by_strike[float(row["strike"])] = iv
    if not by_strike:
        return None

    strikes = sorted(by_strike)
    below = [strike for strike in strikes if strike < spot]
    above = [strike for strike in strikes if strike > spot]
    on_spot = [strike for strike in strikes if strike == spot]

    if on_spot:
        anchor = on_spot[0]
        if option_type == "CE":
            if not above:
                return None
            return (by_strike[anchor] + by_strike[above[0]]) / 2
        if not below:
            return None
        return (by_strike[anchor] + by_strike[below[-1]]) / 2

    if not below or not above:
        return None
    return (by_strike[below[-1]] + by_strike[above[0]]) / 2


def compute_atm_iv_gap(rows: list[dict[str, Any]], *, underlying_price: float) -> dict[str, float | None]:
    """Live ATM forward/parity skew: per-side bracketing-strike ATM IV and their gap.

    Averages each side's two strikes bracketing spot (``bracketing_atm_iv``) → an ATM call IV
    and an ATM put IV; ``atm_gap = atm_put_iv − atm_call_iv`` is the forward/put-call-parity skew
    (positive = puts bid up at ATM). Any leg unreachable (one-sided chain / ``spot ≤ 0``) →
    ``None``, never fabricated. Computed on the full chain — unaffected by the strike-range window.
    """
    spot = _as_float(underlying_price)
    call_rows, put_rows = _split_rows(rows)
    atm_call = bracketing_atm_iv(call_rows, spot=spot, option_type="CE") if spot > 0 else None
    atm_put = bracketing_atm_iv(put_rows, spot=spot, option_type="PE") if spot > 0 else None
    atm_gap = round(atm_put - atm_call, 2) if (atm_call is not None and atm_put is not None) else None
    return {
        "atm_call_iv": round(atm_call, 2) if atm_call is not None else None,
        "atm_put_iv": round(atm_put, 2) if atm_put is not None else None,
        "atm_gap": atm_gap,
    }


# Pinning-pressure tunables. PIN_PROXIMITY_SCALE_MULT widens the proximity kernel to
# ~1.5 strikes; PIN_TTE_FLOOR caps the 1/sqrt(TTE) time-urgency factor at expiry (a
# few market hours, in years) so the score stays finite on expiry day.
PIN_PROXIMITY_SCALE_MULT = 1.5
PIN_TTE_FLOOR = 6e-4


def compute_pinning_pressure(
    rows: list[dict[str, Any]],
    *,
    underlying_price: float,
    strike_size: float,
    time_to_expiry: float,
) -> dict[str, float | None]:
    """Gamma-magnet ("pinning") read for one pair.

    Per strike, ``gamma_mass = Σ(gamma · oi_lots)`` over CE+PE (magnitude of gamma
    concentration; whether it stabilizes vs amplifies is the GEX-regime read's job, not
    pinning's). The **pin strike** is the argmax of the time-free ``gamma_mass · proximity``
    (``proximity = exp(-((K-spot)/scale)**2)``, ``scale = max(1, 1.5·strike_size)``) — the
    most concentrated gamma near spot. The **strength** is that strike's concentration
    share (0–1) **times a time-urgency factor** ``1/sqrt(max(TTE, floor))`` so the reported
    number rises as expiry nears (the time factor is global and would cancel out of a pure
    share, so it is applied to the headline strength on purpose).

    Returns ``{pin_strike, pin_strength, distance_pts, distance_pct}``. All ``None`` when
    there is no positive gamma mass or ``spot ≤ 0`` (never fabricated).
    """
    spot = _as_float(underlying_price)
    if spot <= 0:
        return {"pin_strike": None, "pin_strength": None, "distance_pts": None, "distance_pct": None}

    gamma_mass: dict[float, float] = defaultdict(float)
    for row in rows:
        strike = _as_float(row.get("strike"))
        gamma_mass[strike] += _as_float(row.get("gamma")) * _as_float(row.get("oi_lots"))

    scale = max(1.0, PIN_PROXIMITY_SCALE_MULT * _as_float(strike_size))
    time_urgency = 1.0 / sqrt(max(_as_float(time_to_expiry), PIN_TTE_FLOOR))

    raw: dict[float, float] = {}
    for strike, mass in gamma_mass.items():
        if mass <= 0:
            continue
        proximity = exp(-(((strike - spot) / scale) ** 2))
        raw[strike] = mass * proximity

    total_raw = sum(raw.values())
    if total_raw <= 0:
        return {"pin_strike": None, "pin_strength": None, "distance_pts": None, "distance_pct": None}

    pin_strike = max(raw, key=raw.get)
    concentration_share = raw[pin_strike] / total_raw
    distance_pts = pin_strike - spot
    return {
        "pin_strike": float(pin_strike),
        "pin_strength": round(concentration_share * time_urgency, 3),
        "distance_pts": round(distance_pts, 2),
        "distance_pct": round(distance_pts / spot * 100, 2),
    }


def _gex_market_regime(rows: list[dict[str, Any]]) -> str | None:
    """GEX regime label — sign only, no thresholds.

    Sums the stored per-contract ``gamma_exposure`` (already on the unified
    dealer-signed scale, mainstream sign: +CE / −PE): **positive net = long dealer
    gamma → ``"Stabilizing"``, negative → ``"Destabilizing"``**. ``None`` when there
    is no exposure to compute from.
    """
    regime_gex = 0.0
    seen = False
    for row in rows:
        gex = _as_float(row.get("gamma_exposure", 0))
        if gex == 0:
            continue
        seen = True
        regime_gex += gex
    if not seen:
        return None
    return "Stabilizing" if regime_gex > 0 else "Destabilizing"


def _vex_vol_regime(call_vex: float, put_vex: float) -> str | None:
    """VEX regime.

    Keyed off the scale-free ratio ``call_vex / put_vex``. ``None`` when there is
    no VEX at all. A one-sided chain (all the VEX on one side) is named directly —
    the ratio only runs when both sides are positive, so a zero side can never
    invert the label.
    """
    if call_vex == 0 and put_vex == 0:
        return None
    if call_vex > 0 and put_vex == 0:
        return "Call Vol Heavy"
    if put_vex > 0 and call_vex == 0:
        return "Put Vol Heavy"
    vex_ratio = call_vex / put_vex
    if vex_ratio > 1.5:
        return "Call Vol Heavy"
    if vex_ratio > 1.1:
        return "Call Vol Biased"
    if vex_ratio < 0.7:
        return "Put Vol Heavy"
    if vex_ratio < 0.9:
        return "Put Vol Biased"
    return "Vol Balanced"


def _calculate_greeks_summary(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Greeks summary across the chain."""
    valid = [
        row
        for row in rows
        if all(row.get(key) not in (None, "") for key in ("delta", "gamma", "theta", "vega"))
    ]
    if not valid:
        return {}
    calls = [row for row in valid if row.get("option_type") == "CE"]
    puts = [row for row in valid if row.get("option_type") == "PE"]
    avg_call_delta = (sum(_as_float(row.get("delta")) for row in calls) / len(calls)) if calls else 0.0
    avg_put_delta = (sum(_as_float(row.get("delta")) for row in puts) / len(puts)) if puts else 0.0
    avg_theta = sum(_as_float(row.get("theta")) for row in valid) / len(valid)
    max_vega_row = max(valid, key=lambda row: _as_float(row.get("vega")))
    call_vega_avg = (sum(_as_float(row.get("vega")) for row in calls) / len(calls)) if calls else 0.0
    put_vega_avg = (sum(_as_float(row.get("vega")) for row in puts) / len(puts)) if puts else 0.0
    return {
        "avg_call_delta": round(avg_call_delta, 3),
        "avg_put_delta": round(avg_put_delta, 3),
        "delta_skew": round(avg_call_delta - avg_put_delta, 3),
        "avg_theta": round(avg_theta, 3),
        "max_vega_strike": float(_as_float(max_vega_row.get("strike"))),
        "vega_skew": round(call_vega_avg - put_vega_avg, 3),
    }


def _annotate_rows(request_spec: RequestSpec, rows: list[dict[str, Any]], holidays=None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    annotated = [dict(row) for row in rows]
    if not annotated:
        return annotated, {}

    # Per-contract enrichment (moneyness/bs_price/risk_reward/pain_point/straddle) is
    # computed once at fetch in analytics.option_chain.build_enhanced_option_rows and is
    # already present on the incoming rows. build_pair_report only aggregates the FULL
    # chain into the summary below.
    # Strike size is the CSV spec (request_spec.strike_size), never inferred from the strike grid.
    strike_size = request_spec.strike_size
    underlying_price = _as_float(annotated[0].get("underlying_price", 0))
    time_to_expiry = time_to_expiry_years(
        request_spec.expiry,
        "MCX" if request_spec.segment_code == "MCX_COMM" else "NSE",
        holidays=holidays,
    )

    grouped: dict[float, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in annotated:
        grouped[float(row.get("strike", 0))][str(row.get("option_type", ""))] = row

    max_pain_strike = _calculate_max_pain(annotated)
    gex_by_strike = {
        strike: round(sum(_as_float(row.get("gamma_exposure", 0)) for row in pair.values()), 2)
        for strike, pair in grouped.items()
    }
    return annotated, {
        "strike_size": strike_size,
        "max_pain_strike": max_pain_strike,
        "gex_by_strike": gex_by_strike,
        "time_to_expiry": time_to_expiry,
    }


def build_pair_report(request_spec: RequestSpec, rows: list[dict[str, Any]], holidays=None) -> dict[str, Any]:
    """Build a richer analytics report for one loaded pair."""
    annotated_rows, derived = _annotate_rows(request_spec, rows, holidays)
    call_rows, put_rows = _split_rows(annotated_rows)
    call_oi = _sum_metric(call_rows, "oi_lots")
    put_oi = _sum_metric(put_rows, "oi_lots")
    call_volume = _sum_metric(call_rows, "volume_lots")
    put_volume = _sum_metric(put_rows, "volume_lots")
    call_oi_change = _sum_metric(call_rows, "oi_change_lots")
    put_oi_change = _sum_metric(put_rows, "oi_change_lots")
    call_vex = _sum_metric(call_rows, "volatility_exposure")
    put_vex = _sum_metric(put_rows, "volatility_exposure")
    call_gex = _sum_metric(call_rows, "gamma_exposure")
    put_gex = _sum_metric(put_rows, "gamma_exposure")
    total_gex = _sum_metric(annotated_rows, "gamma_exposure")
    total_vex = call_vex + put_vex
    underlying_price = _as_float(annotated_rows[0]["underlying_price"]) if annotated_rows else 0.0
    pcr = (put_oi / call_oi) if call_oi else None
    greeks_summary = _calculate_greeks_summary(annotated_rows)
    skew = {
        **compute_risk_reversal_25(annotated_rows),
        **compute_atm_iv_gap(annotated_rows, underlying_price=underlying_price),
    }
    pinning = compute_pinning_pressure(
        annotated_rows,
        underlying_price=underlying_price,
        strike_size=derived.get("strike_size", request_spec.strike_size),
        time_to_expiry=derived.get("time_to_expiry", 0.0),
    )

    return {
        "pair_key": request_spec.pair_key.value,
        "instrument_key": request_spec.instrument_key.value,
        "display_name": request_spec.display_name,
        "expiry": request_spec.expiry,
        "lot_size": request_spec.lot_size,
        "strike_size": derived.get("strike_size", request_spec.strike_size),
        "underlying_price": underlying_price,
        "row_count": len(annotated_rows),
        "totals": {
            "call_oi": call_oi,
            "put_oi": put_oi,
            "total_oi": call_oi + put_oi,
            "call_volume": call_volume,
            "put_volume": put_volume,
            "total_volume": call_volume + put_volume,
            "call_oi_change": call_oi_change,
            "put_oi_change": put_oi_change,
            "put_call_ratio": pcr,
            "total_gamma_exposure": total_gex,
            "total_volatility_exposure": total_vex,
            "call_vex": call_vex,
            "put_vex": put_vex,
        },
        "levels": {
            "max_call_oi_strike": _max_oi_strike(call_rows),
            "max_put_oi_strike": _max_oi_strike(put_rows),
            "max_call_volume_strike": _max_volume_strike(call_rows),
            "max_put_volume_strike": _max_volume_strike(put_rows),
            "second_max_call_oi_strike": _second_max_oi_strike(call_rows),
            "second_max_put_oi_strike": _second_max_oi_strike(put_rows),
            "second_max_call_volume_strike": _second_max_volume_strike(call_rows),
            "second_max_put_volume_strike": _second_max_volume_strike(put_rows),
            # Tier-2 pain-point walls (spot-cross alert): pain point of the highest-OI strike per side.
            "resistance_strong": _pain_of_max_oi(call_rows),
            "support_strong": _pain_of_max_oi(put_rows),
        },
        "gex_analysis": {
            "gex_by_strike": derived.get("gex_by_strike", {}),
            "total_gex": round(total_gex, 2),
            "call_gex": round(call_gex, 2),
            "put_gex": round(put_gex, 2),
            "market_regime": _gex_market_regime(annotated_rows),
        },
        "vex_analysis": {
            "total_vex": round(total_vex, 2),
            "call_vex": round(call_vex, 2),
            "put_vex": round(put_vex, 2),
            "vol_regime": _vex_vol_regime(call_vex, put_vex),
        },
        "greeks_summary": greeks_summary,
        "skew": skew,
        "pinning": pinning,
        "max_pain_strike": derived.get("max_pain_strike", 0.0),
        "time_to_expiry": derived.get("time_to_expiry", 0.0),
        "rows": sorted(annotated_rows, key=lambda row: (float(row.get("strike", 0)), str(row.get("option_type", "")))),
    }


def _pair_key_parts(report: Mapping[str, Any]) -> tuple[int, str]:
    parts = str(report.get("pair_key", "")).split("|")
    if len(parts) >= 2:
        try:
            return int(parts[0]), parts[1]
        except (TypeError, ValueError):
            return 0, parts[1]
    return 0, ""


def flatten_pair_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a ``build_pair_report`` dict to the scalar ``pair_reports`` columns.

    The per-strike series and support/resistance lists are NOT included — they are
    derived from the stored rows on read. Identity (timestamp / fetch_group_id) is
    added by the writer. Used at ingest to persist the per-pair summary once.
    """
    asset_id, segment_code = _pair_key_parts(report)
    totals = report.get("totals", {})
    gex = report.get("gex_analysis", {})
    vex = report.get("vex_analysis", {})
    greeks = report.get("greeks_summary", {})
    levels = report.get("levels", {})
    skew = report.get("skew", {})
    return {
        "underlying_asset_id": asset_id,
        "segment_code": segment_code,
        "expiry": report.get("expiry"),
        "underlying_price": report.get("underlying_price"),
        "lot_size": report.get("lot_size"),
        "strike_size": report.get("strike_size"),
        "row_count": report.get("row_count"),
        "total_call_oi_lots": totals.get("call_oi"),
        "total_put_oi_lots": totals.get("put_oi"),
        "total_oi_lots": totals.get("total_oi"),
        "total_call_volume_lots": totals.get("call_volume"),
        "total_put_volume_lots": totals.get("put_volume"),
        "total_volume_lots": totals.get("total_volume"),
        "total_call_oi_change_lots": totals.get("call_oi_change"),
        "total_put_oi_change_lots": totals.get("put_oi_change"),
        "put_call_ratio": totals.get("put_call_ratio"),
        "total_gamma_exposure": gex.get("total_gex"),
        "call_gamma_exposure": gex.get("call_gex"),
        "put_gamma_exposure": gex.get("put_gex"),
        "total_volatility_exposure": vex.get("total_vex"),
        "call_volatility_exposure": vex.get("call_vex"),
        "put_volatility_exposure": vex.get("put_vex"),
        "gex_market_regime": gex.get("market_regime"),
        "vol_regime": vex.get("vol_regime"),
        "avg_call_delta": greeks.get("avg_call_delta"),
        "avg_put_delta": greeks.get("avg_put_delta"),
        "delta_skew": greeks.get("delta_skew"),
        "avg_theta": greeks.get("avg_theta"),
        "max_vega_strike": greeks.get("max_vega_strike"),
        "vega_skew": greeks.get("vega_skew"),
        "max_call_oi_strike": levels.get("max_call_oi_strike"),
        "max_put_oi_strike": levels.get("max_put_oi_strike"),
        "max_pain_strike": report.get("max_pain_strike"),
        "time_to_expiry": report.get("time_to_expiry"),
        "risk_reversal_25": skew.get("rr_25"),
        "call_iv_25d": skew.get("call_iv_25d"),
        "put_iv_25d": skew.get("put_iv_25d"),
    }


def inflate_pair_report(
    columns: Mapping[str, Any],
    rows: list[dict[str, Any]],
    request_spec: RequestSpec,
) -> dict[str, Any]:
    """Rebuild a full report dict from the stored ``pair_reports`` columns + stored rows.

    Scalars come straight from the columns (no recompute); the cheap per-strike GEX map
    and the support/resistance highlight strikes are derived from the rows. Produces the
    exact shape ``build_pair_report`` returns, so every renderer reads it unchanged.
    """
    annotated = [dict(row) for row in rows]
    grouped: dict[float, float] = defaultdict(float)
    for row in annotated:
        grouped[float(row.get("strike", 0) or 0)] += _as_float(row.get("gamma_exposure", 0))
    gex_by_strike = {strike: round(value, 2) for strike, value in grouped.items()}
    call_rows, put_rows = _split_rows(annotated)

    c = columns
    pinning = compute_pinning_pressure(
        annotated,
        underlying_price=_as_float(c.get("underlying_price")),
        strike_size=_as_float(c.get("strike_size")),
        time_to_expiry=_as_float(c.get("time_to_expiry")),
    )
    # ATM parity gap is recomputed from the stored rows (like pinning / gex_by_strike) — not a column.
    atm_gap = compute_atm_iv_gap(annotated, underlying_price=_as_float(c.get("underlying_price")))
    return {
        "pair_key": request_spec.pair_key.value,
        "instrument_key": request_spec.instrument_key.value,
        "display_name": request_spec.display_name,
        "expiry": request_spec.expiry,
        "lot_size": c.get("lot_size"),
        "strike_size": c.get("strike_size"),
        "underlying_price": c.get("underlying_price"),
        "row_count": c.get("row_count"),
        "totals": {
            "call_oi": c.get("total_call_oi_lots"),
            "put_oi": c.get("total_put_oi_lots"),
            "total_oi": c.get("total_oi_lots"),
            "call_volume": c.get("total_call_volume_lots"),
            "put_volume": c.get("total_put_volume_lots"),
            "total_volume": c.get("total_volume_lots"),
            "call_oi_change": c.get("total_call_oi_change_lots"),
            "put_oi_change": c.get("total_put_oi_change_lots"),
            "put_call_ratio": c.get("put_call_ratio"),
            "total_gamma_exposure": c.get("total_gamma_exposure"),
            "total_volatility_exposure": c.get("total_volatility_exposure"),
            "call_vex": c.get("call_volatility_exposure"),
            "put_vex": c.get("put_volatility_exposure"),
        },
        "levels": {
            "max_call_oi_strike": c.get("max_call_oi_strike"),
            "max_put_oi_strike": c.get("max_put_oi_strike"),
            "max_call_volume_strike": _max_volume_strike(call_rows),
            "max_put_volume_strike": _max_volume_strike(put_rows),
            # 2nd-highest strikes aren't persisted as columns — recompute from rows (rows are stored),
            # mirroring the volume-strike handling above. Keeps the report shape identical to live.
            "second_max_call_oi_strike": _second_max_oi_strike(call_rows),
            "second_max_put_oi_strike": _second_max_oi_strike(put_rows),
            "second_max_call_volume_strike": _second_max_volume_strike(call_rows),
            "second_max_put_volume_strike": _second_max_volume_strike(put_rows),
            # Tier-2 pain-point walls — recomputed from rows (rows carry pain_point), like pinning.
            "resistance_strong": _pain_of_max_oi(call_rows),
            "support_strong": _pain_of_max_oi(put_rows),
        },
        "gex_analysis": {
            "gex_by_strike": gex_by_strike,
            "total_gex": c.get("total_gamma_exposure"),
            "call_gex": c.get("call_gamma_exposure"),
            "put_gex": c.get("put_gamma_exposure"),
            "market_regime": c.get("gex_market_regime"),
        },
        "vex_analysis": {
            "total_vex": c.get("total_volatility_exposure"),
            "call_vex": c.get("call_volatility_exposure"),
            "put_vex": c.get("put_volatility_exposure"),
            "vol_regime": c.get("vol_regime"),
        },
        "greeks_summary": {
            "avg_call_delta": c.get("avg_call_delta"),
            "avg_put_delta": c.get("avg_put_delta"),
            "delta_skew": c.get("delta_skew"),
            "avg_theta": c.get("avg_theta"),
            "max_vega_strike": c.get("max_vega_strike"),
            "vega_skew": c.get("vega_skew"),
        },
        "skew": {
            "rr_25": c.get("risk_reversal_25"),
            "call_iv_25d": c.get("call_iv_25d"),
            "put_iv_25d": c.get("put_iv_25d"),
            **atm_gap,
        },
        "pinning": pinning,
        "max_pain_strike": c.get("max_pain_strike"),
        "time_to_expiry": c.get("time_to_expiry"),
        "rows": sorted(annotated, key=lambda row: (float(row.get("strike", 0)), str(row.get("option_type", "")))),
    }
