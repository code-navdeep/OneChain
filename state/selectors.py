"""State selectors and filtering helpers."""

from __future__ import annotations

from typing import Any

from state.models import LoadedPairState, LoadedReportState


def get_active_loaded_pair(
    loaded_report_state: LoadedReportState | None,
    active_pair_key: str | None,
) -> LoadedPairState | None:
    if loaded_report_state is None or not loaded_report_state.loaded_pairs:
        return None

    for pair in loaded_report_state.loaded_pairs:
        if pair.resolved_view_key == active_pair_key:
            return pair
    return loaded_report_state.loaded_pairs[0]


def filter_report_rows_by_strike_range(report: dict[str, Any], strike_range: str) -> list[dict[str, Any]]:
    rows = [dict(row) for row in report.get("rows", [])]
    if not rows or strike_range == "All":
        return rows

    try:
        strikes_each_side = int(strike_range)
    except (TypeError, ValueError):
        return rows

    underlying_price = float(report.get("underlying_price", 0) or 0)
    unique_strikes = sorted({float(row["strike"]) for row in rows})
    if not unique_strikes or underlying_price <= 0:
        return rows

    # ATM is the underlying PRICE — which normally sits *between* two strikes, not on
    # one. ±N therefore means the N strikes strictly below the price plus the N strictly
    # above → 2N strikes, with no extra "center" strike. Edge case (price exactly on a
    # strike, very rare): that strike is the center and we take ±N around it → 2N+1.
    on_strike = [s for s in unique_strikes if abs(s - underlying_price) < 1e-6]
    if on_strike:
        center = on_strike[0]
        below = [s for s in unique_strikes if s < center][-strikes_each_side:]
        above = [s for s in unique_strikes if s > center][:strikes_each_side]
        keep = set(below) | {center} | set(above)
    else:
        below = [s for s in unique_strikes if s < underlying_price][-strikes_each_side:]
        above = [s for s in unique_strikes if s > underlying_price][:strikes_each_side]
        keep = set(below) | set(above)

    return [row for row in rows if float(row["strike"]) in keep]


def split_rows_by_option_type(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    call_rows = [row for row in rows if row.get("option_type") == "CE"]
    put_rows = [row for row in rows if row.get("option_type") == "PE"]
    return call_rows, put_rows


def windowed_max_oi_pain(
    report: dict[str, Any], strike_range: str, option_type: str
) -> float | None:
    """The ``pain_point`` of the highest-OI strike on one side, **within the strike-range window**.

    Both the price-chart pain lines and the reversal-curve wall lines call this so they pick their
    call/put wall from the strikes currently in view (the ``strike-range-selector``) rather than the
    full chain — matching what the chain table shows. The "highest-OI strike → its pain_point"
    selection reproduces ``report_builder._max_oi_strike`` (``max`` by ``oi_lots``) over the windowed
    subset, so at ``strike_range == "All"`` it equals the full-chain wall. Returns ``None`` when the
    side has no windowed rows or no usable pain_point (e.g. aggregate rows carry none).
    """
    windowed = filter_report_rows_by_strike_range(report, strike_range)
    call_rows, put_rows = split_rows_by_option_type(windowed)
    side_rows = call_rows if option_type == "CE" else put_rows
    if not side_rows:
        return None
    top_row = max(side_rows, key=lambda row: float(row.get("oi_lots", 0) or 0))
    pain_point = top_row.get("pain_point")
    return float(pain_point) if pain_point not in (None, 0) else None


def windowed_max_oi_strike(
    report: dict[str, Any], strike_range: str, option_type: str
) -> float | None:
    """The **strike** of the highest-OI strike on one side, within the strike-range window.

    Sibling of ``windowed_max_oi_pain`` (same windowed ``max``-by-``oi_lots`` selection) but returns
    the strike itself rather than its pain_point — used to label the reversal-curve wall lines with
    the actual strike (e.g. "22800 CE"). ``None`` when the side has no windowed rows.
    """
    windowed = filter_report_rows_by_strike_range(report, strike_range)
    call_rows, put_rows = split_rows_by_option_type(windowed)
    side_rows = call_rows if option_type == "CE" else put_rows
    if not side_rows:
        return None
    top_row = max(side_rows, key=lambda row: float(row.get("oi_lots", 0) or 0))
    strike = top_row.get("strike")
    return float(strike) if strike not in (None, "") else None
