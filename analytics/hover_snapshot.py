"""Pure 09:20 reference snapshot for the A2 chain-table hovers.

No I/O. Captures, from the already-enriched option rows, a per-strike ±32 band (each strike's OI/IV per
side + PCR + straddle-OI) **and** a full-chain totals block (per-side total OI, OI-PCR, bracketed-ATM IV
per side) for the Totals-row hovers. Consumed by the collector's ``_update_hover`` (stored in
``hover.json``) and read back by the display overlay ``_apply_hover``. Display-reference only — it never
feeds any live compute.
"""

from __future__ import annotations

from math import isnan
from typing import Any

from analytics.report_builder import compute_atm_iv_gap


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if isnan(result) else result


def strike_key(strike: float) -> str:
    """Stable string key for a strike (whole → ``'22600'``, decimal grids → ``'2.5'``), used for **both**
    writing and reading the snapshot so the display looks a strike up by the same key (JSON-safe)."""
    value = float(strike)
    return str(int(value)) if value == int(value) else f"{value:g}"


def build_hover_snapshot(rows, spot, *, band: int = 32) -> dict:
    """The 09:20 reference snapshot for one pair: ``{"strikes": {…}, "totals": {…}}``, or ``{}`` when spot
    is missing/≤0 or there are no rows. Pure.

    ``strikes`` — the ±``band`` strikes around ``spot``: ``{strike_key: {call_oi, put_oi, call_iv, put_iv,
    pcr, straddle_oi}}`` (OI lot-scaled; ``pcr`` = put_oi/call_oi, ``None`` without call OI; ``straddle_oi``
    = call_oi + put_oi; per-side OI/IV ``None`` when a side is absent).
    ``totals`` — full-chain aggregates for the Totals row (``_build_totals``): ``{call_oi, put_oi, pcr,
    call_atm_iv, put_atm_iv}`` (per-side total OI, OI-PCR, bracketed-ATM IV per side)."""
    spot_value = _as_float(spot)
    if spot_value is None or spot_value <= 0 or not rows:
        return {}

    by_strike: dict[float, dict[str, float | None]] = {}
    for row in rows:
        strike = _as_float(row.get("strike"))
        if strike is None:
            continue
        entry = by_strike.setdefault(strike, {})
        if row.get("option_type") == "CE":
            entry["call_oi"] = _as_float(row.get("oi_lots"))
            entry["call_iv"] = _as_float(row.get("iv"))
        elif row.get("option_type") == "PE":
            entry["put_oi"] = _as_float(row.get("oi_lots"))
            entry["put_iv"] = _as_float(row.get("iv"))

    strikes = sorted(by_strike)
    selected = [s for s in strikes if s < spot_value][-band:] + [s for s in strikes if s >= spot_value][:band]

    strikes_block: dict[str, dict] = {}
    for strike in selected:
        entry = by_strike[strike]
        call_oi_value = entry.get("call_oi") or 0.0
        put_oi_value = entry.get("put_oi") or 0.0
        strikes_block[strike_key(strike)] = {
            "call_oi": entry.get("call_oi"),
            "put_oi": entry.get("put_oi"),
            "call_iv": entry.get("call_iv"),
            "put_iv": entry.get("put_iv"),
            "pcr": (put_oi_value / call_oi_value) if call_oi_value > 0 else None,
            "straddle_oi": call_oi_value + put_oi_value,
        }
    return {"strikes": strikes_block, "totals": _build_totals(rows, spot_value)}


def _build_totals(rows, spot: float) -> dict:
    """Full-chain Totals-row aggregates at 09:20: per-side total OI (``oi_lots`` summed), OI-PCR, and the
    bracketed-ATM IV per side (reusing ``report_builder.compute_atm_iv_gap``). Per-side, never combined."""
    call_oi_total = 0.0
    put_oi_total = 0.0
    for row in rows:
        oi = _as_float(row.get("oi_lots")) or 0.0
        if row.get("option_type") == "CE":
            call_oi_total += oi
        elif row.get("option_type") == "PE":
            put_oi_total += oi
    atm = compute_atm_iv_gap(rows, underlying_price=spot)
    return {
        "call_oi": call_oi_total,
        "put_oi": put_oi_total,
        "pcr": (put_oi_total / call_oi_total) if call_oi_total > 0 else None,
        "call_atm_iv": atm.get("atm_call_iv"),
        "put_atm_iv": atm.get("atm_put_iv"),
    }
