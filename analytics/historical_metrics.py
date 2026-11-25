"""Pure-compute historical IV / RV metrics for the snapshot engine.

This module holds the *math* for the snapshot ``historical_metrics`` row: realized
volatility, the continuous bracketing-strike ATM IV series (per side; *not* a
straddle), IV percentile / rank / skew, and the IV-RV spread + signal. It takes plain
row dicts (fetched by the snapshot repository) and returns plain values — no DB access,
no fallbacks. When a value cannot be computed honestly it is ``None``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Sequence

from analytics.report_builder import bracketing_atm_iv

WINDOWS: tuple[int, ...] = (22, 90, 180, 252)
# IV percentile is only meaningful with enough history behind it, so it is computed/stored
# at the long windows only (a 22- or 90-day percentile is noise). IV rank was removed entirely
# (it keys off a single window min/max, so one spike day warps it).
PERCENTILE_WINDOWS: tuple[int, ...] = (180, 252)


def realized_volatility_percent(
    daily_closes: Sequence[dict],
    *,
    window_days: int,
) -> float | None:
    """Annualized realized volatility (%) from underlying daily closes.

    ``daily_closes`` is ascending by date. Strict: needs exactly ``window_days + 1``
    usable closes to form ``window_days`` log-returns, else ``None``. Uses sample
    standard deviation (ddof=1), annualized with sqrt(252)."""
    close_values = [
        float(row.get("underlying_price", 0) or 0)
        for row in daily_closes
        if float(row.get("underlying_price", 0) or 0) > 0
    ]
    required_closes = window_days + 1
    if len(close_values) < required_closes:
        return None

    closes = close_values[-required_closes:]
    returns = [
        math.log(current / previous)
        for previous, current in zip(closes, closes[1:])
        if previous > 0 and current > 0
    ]
    if len(returns) < window_days:
        return None

    mean_return = sum(returns) / len(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / max(1, len(returns) - 1)
    std_daily = math.sqrt(max(0.0, variance))
    return std_daily * math.sqrt(252) * 100


def _calendar_dte(date_str: str, expiry_str: str) -> int | None:
    """Calendar days from a trade date to an expiry (both ``YYYY-MM-DD``); ``None`` on parse failure."""
    try:
        trade_date = datetime.strptime(str(date_str), "%Y-%m-%d").date()
        expiry_date = datetime.strptime(str(expiry_str), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (expiry_date - trade_date).days


def _variance_interpolate(points: Sequence[tuple[int, float]], *, target_days: int) -> float | None:
    """Constant-maturity IV at ``target_days`` via linear interpolation in total variance (VIX-style).

    ``points`` is ``(dte, iv%)`` for one date / one side. Total variance ``var = (iv/100)² · dte/365``
    is interpolated between the two expiries bracketing ``target_days``, then re-annualized:
    ``cm_iv = sqrt(var* · 365 / target_days) · 100``. Requires a true bracket (one ``dte ≤ target ≤``
    another) — **no extrapolation**; returns ``None`` otherwise. An exact ``dte == target_days`` hit
    short-circuits."""
    usable = sorted(((dte, iv) for dte, iv in points if dte and dte > 0 and iv and iv > 0), key=lambda p: p[0])
    for dte, iv in usable:
        if dte == target_days:
            return iv
    lower: tuple[int, float] | None = None
    upper: tuple[int, float] | None = None
    for dte, iv in usable:
        if dte < target_days:
            lower = (dte, iv)            # largest dte below target
        elif dte > target_days and upper is None:
            upper = (dte, iv)            # smallest dte above target
    if lower is None or upper is None:
        return None
    t1, iv1 = lower
    t2, iv2 = upper
    var1 = (iv1 / 100.0) ** 2 * (t1 / 365.0)
    var2 = (iv2 / 100.0) ** 2 * (t2 / 365.0)
    var_star = var1 + (var2 - var1) * (target_days - t1) / (t2 - t1)
    if var_star <= 0:
        return None
    return math.sqrt(var_star * 365.0 / target_days) * 100.0


def build_constant_maturity_atm_iv_series(
    context_rows: Sequence[dict],
    *,
    target_days: int = 30,
) -> dict[str, list[tuple[str, float]]]:
    """Build the constant-maturity (default 30-day) bracketing-strike ATM IV series per side.

    ``context_rows`` are the per-date ATM-context option rows across **all** collected expiries
    (columns ``date, expiry, option_type, strike, iv, underlying_price``). For each date and side,
    the per-expiry bracketing-strike ATM IV (per side; *not* a straddle) is variance-interpolated
    to ``target_days`` (VIX-style). Returns ``{"CE": [(date, cm_iv), ...], "PE": [...]}`` ascending
    by date; dates without a bracket around the target are dropped (no extrapolation)."""
    rows_by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    spot_by_date_expiry: dict[tuple[str, str], float] = {}
    expiries_by_date: dict[str, set[str]] = defaultdict(set)
    for row in context_rows:
        date = str(row["date"])
        expiry = str(row["expiry"])
        option_type = str(row["option_type"])
        rows_by_key[(date, expiry, option_type)].append(row)
        spot_by_date_expiry[(date, expiry)] = float(row.get("underlying_price", 0) or 0)
        expiries_by_date[date].add(expiry)

    series: dict[str, list[tuple[str, float]]] = {"CE": [], "PE": []}
    for date in sorted(expiries_by_date):
        for option_type in ("CE", "PE"):
            points: list[tuple[int, float]] = []
            for expiry in expiries_by_date[date]:
                spot = spot_by_date_expiry.get((date, expiry), 0.0)
                if spot <= 0:
                    continue
                rows = rows_by_key.get((date, expiry, option_type), [])
                if not rows:
                    continue
                atm_iv = bracketing_atm_iv(rows, spot=spot, option_type=option_type)
                if atm_iv is None:
                    continue
                dte = _calendar_dte(date, expiry)
                if dte is None or dte <= 0:
                    continue
                points.append((dte, atm_iv))
            cm_iv = _variance_interpolate(points, target_days=target_days)
            if cm_iv is not None:
                series[option_type].append((date, cm_iv))
    return series


def iv_window_metrics(
    series: dict[str, list[tuple[str, float]]],
    *,
    window_days: int,
) -> dict[str, float]:
    """IV percentile / skew / current for one window (no rank — removed).

    Strict: a side is only computed when it has at least ``window_days`` dated points;
    metrics are taken over the most recent ``window_days`` points (the window tail),
    with ``current`` = the most recent point. Percentile is computed here for any window;
    the payload builder only *stores* it at the long ``PERCENTILE_WINDOWS``. Missing
    metrics are simply absent."""
    out: dict[str, float] = {}
    for option_type, key in (("CE", "call"), ("PE", "put")):
        points = series.get(option_type, [])
        if len(points) < window_days:
            continue
        window_points = points[-window_days:]
        ivs = [iv for _date, iv in window_points]
        current = ivs[-1]
        out[f"{key}_iv_current"] = current
        if len(ivs) > 1:
            out[f"{key}_iv_percentile"] = sum(1 for value in ivs if value < current) / len(ivs) * 100

    if "call_iv_current" in out and "put_iv_current" in out:
        out["iv_skew"] = out["put_iv_current"] - out["call_iv_current"]
    return out


def iv_rv_spread(iv_current: float | None, rv: float | None) -> float | None:
    """IV-RV spread (``iv − rv``) for one side, one window — the raw variance-risk-premium
    datum. ``None`` when not computable. (No over/under/fair signal — a zero-baseline signal
    is structurally biased noise and was removed.)"""
    if iv_current is None or rv is None or rv == 0:
        return None
    return iv_current - rv


def build_historical_metric_payload(
    *,
    date: str,
    asset_id: int,
    segment_code: str,
    daily_closes: Sequence[dict],
    context_rows: Sequence[dict],
    windows: Sequence[int] = WINDOWS,
    target_days: int = 30,
) -> dict:
    """Build the flat wide ``historical_metrics`` row from raw repo series.

    The IV series is **constant-maturity** (``target_days``, default 30 — VIX-style), so the
    metric row is one per ``(date, asset, segment)`` — there is no per-expiry granularity.
    Single-fetch-then-slice: ``daily_closes`` and ``context_rows`` are fetched once at the max
    window; every window slices the relevant tail. Every metric column is present in the payload,
    valued ``None`` when not honestly computable (no fallbacks)."""
    series = build_constant_maturity_atm_iv_series(context_rows, target_days=target_days)
    payload: dict = {
        "date": date,
        "underlying_asset_id": asset_id,
        "segment_code": segment_code,
    }

    for window in windows:
        rv = realized_volatility_percent(daily_closes, window_days=window)
        metrics = iv_window_metrics(series, window_days=window)

        payload[f"rv_{window}d"] = rv
        payload[f"iv_skew_{window}d"] = metrics.get("iv_skew")
        payload[f"call_iv_rv_spread_{window}d"] = iv_rv_spread(metrics.get("call_iv_current"), rv)
        payload[f"put_iv_rv_spread_{window}d"] = iv_rv_spread(metrics.get("put_iv_current"), rv)
        # Percentile only at the long windows (where it's meaningful). No rank, no signal.
        if window in PERCENTILE_WINDOWS:
            payload[f"call_iv_percentile_{window}d"] = metrics.get("call_iv_percentile")
            payload[f"put_iv_percentile_{window}d"] = metrics.get("put_iv_percentile")

    return payload


def historical_metric_columns(windows: Sequence[int] = WINDOWS) -> dict[str, str]:
    """Column-name -> SQLite type for every metric column (the snapshot schema CREATE).

    Single source of truth for the `historical_metrics` columns. Not a flat grid: the
    percentile families exist only at ``PERCENTILE_WINDOWS`` (long windows); there is no
    rank family and no signal family (all columns are REAL)."""
    all_window_real = ("rv", "iv_skew", "call_iv_rv_spread", "put_iv_rv_spread")
    long_window_real = ("call_iv_percentile", "put_iv_percentile")
    columns: dict[str, str] = {}
    for prefix in all_window_real:
        for window in windows:
            columns[f"{prefix}_{window}d"] = "REAL"
    for prefix in long_window_real:
        for window in windows:
            if window in PERCENTILE_WINDOWS:
                columns[f"{prefix}_{window}d"] = "REAL"
    return columns
