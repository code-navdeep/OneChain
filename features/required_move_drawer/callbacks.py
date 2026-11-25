"""Required-move drawer callbacks — chain-cell selection → live per-option sparklines.

Clicking an option's **price cell** in the chain table pins that contract; the drawer holds
the last ≤2 pinned contracts and re-draws their required-move curves on every live refresh.
The feature is **live + per-pair only** — selections are ignored and the body clears in
aggregate / playback / idle, mirroring how the rest of the app scopes to the active pair.

Identity (not cell coordinates) is what is stored, so the chain table re-rendering on each
refresh never loses the drawer's memory. Pure helpers (``push_contract`` / ``build_contract``
/ ``is_live_pair``) carry the selection logic and are unit-tested directly.
"""

from __future__ import annotations

from typing import Any

from dash import Input, Output, State, callback_context, no_update

from analytics.required_move import build_required_move_curve
from features.chain.table_model import extract_strike_value
from features.required_move_drawer.rendering import build_drawer_body, drawer_toggle_style
from services.runtime import RuntimeServices
from state.models import ActiveViewContext, LoadedReportState
from state.selectors import (
    filter_report_rows_by_strike_range,
    get_active_loaded_pair,
    split_rows_by_option_type,
    windowed_max_oi_pain,
    windowed_max_oi_strike,
)
from ui.formatters import format_strike

# Chain price-cell column id -> option side.
_PRICE_COLUMNS = {"call_last_price": "CE", "put_last_price": "PE"}
MAX_PINNED = 2

# A 2nd-highest-OI wall reversal line is drawn only when the 2nd strike's OI is at least this fraction
# of the highest-OI strike's — a small local gate, deliberately its OWN constant (unrelated to the
# interpretor / alert thresholds), so this stays confined to the drawer.
_SECOND_WALL_QUAL_RATIO = 0.75


def _windowed_second_oi_wall(report: dict, strike_range: str, option_type: str) -> tuple[float, float] | None:
    """``(strike, pain_point)`` of the **2nd-highest-OI** strike on one side, *within the strike-range
    window*, or ``None``.

    Windowed to match the primary walls (``windowed_max_oi_pain``), so "2nd highest" means 2nd among
    the strikes currently shown and it jumps with the strike-range selector. Returns ``None`` when the
    side has fewer than two windowed strikes, the 2nd's OI is below ``_SECOND_WALL_QUAL_RATIO`` × the
    highest's, or its ``pain_point`` is ``0``/absent — in every such case the drawer simply omits the
    line (no fallback)."""
    windowed = filter_report_rows_by_strike_range(report, strike_range)
    call_rows, put_rows = split_rows_by_option_type(windowed)
    side_rows = call_rows if option_type == "CE" else put_rows
    if len(side_rows) < 2:
        return None
    ordered = sorted(side_rows, key=lambda r: float(r.get("oi_lots", 0) or 0), reverse=True)
    top_oi = float(ordered[0].get("oi_lots", 0) or 0)
    second = ordered[1]
    second_oi = float(second.get("oi_lots", 0) or 0)
    if top_oi <= 0 or second_oi < _SECOND_WALL_QUAL_RATIO * top_oi:
        return None
    pain = second.get("pain_point")
    if pain in (None, 0):
        return None
    return float(second.get("strike", 0)), float(pain)


def _contract_key(contract: dict) -> tuple:
    return (
        contract["asset_id"],
        contract["segment_code"],
        contract["expiry"],
        contract["strike"],
        contract["option_type"],
    )


def is_live_pair(context: ActiveViewContext | None) -> bool:
    """The drawer operates only on the live, per-pair view."""
    return context is not None and context.source == "live" and context.kind == "pair"


def push_contract(contracts: list[dict] | None, contract: dict, *, cap: int = MAX_PINNED) -> list[dict]:
    """Prepend ``contract`` (newest first), drop any duplicate, cap to ``cap`` entries."""
    key = _contract_key(contract)
    rest = [c for c in (contracts or []) if _contract_key(c) != key]
    return [contract, *rest][:cap]


# The store is bucketed **per pair** (key = ``"{asset_id}|{segment_code}|{expiry}"`` — the same
# ``pair_key`` the pills / ActiveViewContext use): each pair keeps its own ≤MAX_PINNED list, and only
# the active pair's bucket is rendered. The pair — instrument AND expiry — is the app's unit of
# view everywhere else; an earlier per-*instrument* key pooled all expiries of one instrument into a
# single bucket, so in single-instrument multi-expiry mode pins from one expiry pill showed on (and
# evicted from) every other expiry pill. The cap is per-pair, not global.
def bucket_for(store: dict | None, pair_key: str | None) -> list[dict]:
    """The pinned-contract list for ``pair_key`` (empty when missing/None)."""
    if not pair_key:
        return []
    return (store or {}).get(pair_key, []) or []


def push_to_bucket(
    store: dict | None, pair_key: str, contract: dict, *, cap: int = MAX_PINNED
) -> dict:
    """Prepend ``contract`` into ``pair_key``'s ≤cap list, leaving other pairs untouched."""
    buckets = dict(store or {})
    buckets[pair_key] = push_contract(buckets.get(pair_key), contract, cap=cap)
    return buckets


def clear_bucket(store: dict | None, pair_key: str | None) -> dict:
    """Drop ``pair_key``'s bucket (Clear empties only the pair on screen)."""
    buckets = dict(store or {})
    if pair_key:
        buckets.pop(pair_key, None)
    return buckets


def build_contract(
    active_cell: dict | None,
    table_data: list[dict] | None,
    *,
    asset_id: int,
    segment_code: str,
    expiry: str,
    rows: list[dict],
) -> dict | None:
    """Resolve a clicked price cell to a contract identity, or ``None`` if it isn't one.

    Only the call/put LTP columns count; the strike comes from the row's strike cell and is
    matched against the active pair's rows so non-option cells (the underlying row, blanks)
    are rejected.
    """
    if not active_cell or not table_data:
        return None
    side = _PRICE_COLUMNS.get(str(active_cell.get("column_id")))
    if side is None:
        return None
    row_idx = active_cell.get("row")
    if row_idx is None or row_idx >= len(table_data):
        return None
    raw_strike = str(table_data[row_idx].get("strike", ""))
    # The spliced underlying-price row's strike cell *starts* with ₹ (e.g. "₹23456.78"); normal
    # strike rows start with the strike digits — and their straddle price on line 2 also carries ₹
    # ("23300\n(₹349)"), so a bare "₹ in string" test wrongly rejected every option row.
    if raw_strike.lstrip().startswith("₹"):
        return None
    strike = extract_strike_value(raw_strike)
    if not strike:
        return None
    match = next(
        (r for r in rows if abs(float(r.get("strike", 0) or 0) - strike) < 1e-6 and str(r.get("option_type")) == side),
        None,
    )
    if match is None:
        return None
    return {
        "asset_id": asset_id,
        "segment_code": segment_code,
        "expiry": expiry,
        "strike": float(match["strike"]),
        "option_type": side,
    }


def _rate_for(services: RuntimeServices, segment_code: str) -> float | None:
    lfs = services.live_fetch_service
    if lfs is None:
        return None
    return lfs.risk_free_rate_mcx if segment_code == "MCX_COMM" else lfs.risk_free_rate_nse


def _find_pair(loaded_state: LoadedReportState, contract: dict):
    for pair in loaded_state.loaded_pairs:
        request = pair.request
        if (request.asset_id, request.segment_code, request.expiry) == (
            contract["asset_id"],
            contract["segment_code"],
            contract["expiry"],
        ):
            return pair
    return None


def _entries_for(
    services: RuntimeServices,
    loaded_state: LoadedReportState,
    contracts: list[dict] | None,
    strike_range: str,
):
    entries: list[tuple[str, dict[str, Any]]] = []
    for contract in contracts or []:
        pair = _find_pair(loaded_state, contract)
        if pair is None:
            continue
        rows = pair.report.get("rows", []) or []
        row = next(
            (
                r
                for r in rows
                if abs(float(r.get("strike", 0) or 0) - contract["strike"]) < 1e-6
                and str(r.get("option_type")) == contract["option_type"]
            ),
            None,
        )
        if row is None:
            continue
        # The "current price" line is the option's actual stored LTP over time — no math.
        ltp_series = services.view_data_service.get_oa_option_timeseries(
            contract["asset_id"],
            contract["segment_code"],
            contract["expiry"],
            contracts=[(contract["strike"], contract["option_type"])],
            metric="last_price",
        )
        # The two extra reversal lines reuse this contract with the spot held at the wall pain points.
        # Walls = the highest-OI strike's pain point **within the current strike-range window** (what
        # the chain shows), not the full-chain levels — so they jump with the strike-range selector.
        call_pain = windowed_max_oi_pain(pair.report, strike_range, "CE")
        put_pain = windowed_max_oi_pain(pair.report, strike_range, "PE")
        # The 2nd-highest-OI walls (windowed, 75%-gated locally) — None when there's no qualifying 2nd,
        # so the line is simply not drawn.
        call_2nd = _windowed_second_oi_wall(pair.report, strike_range, "CE")
        put_2nd = _windowed_second_oi_wall(pair.report, strike_range, "PE")
        curve = build_required_move_curve(
            strike=contract["strike"],
            option_type=contract["option_type"],
            iv=float(row.get("iv", 0) or 0),
            rate=_rate_for(services, contract["segment_code"]),
            expiry=contract["expiry"],
            segment=contract["segment_code"],
            pain_point=float(row.get("pain_point", 0) or 0),
            call_pain_point=call_pain,
            put_pain_point=put_pain,
            call_2nd_pain_point=call_2nd[1] if call_2nd else None,
            put_2nd_pain_point=put_2nd[1] if put_2nd else None,
            ltp_series=ltp_series,
            holidays=getattr(services, "holidays", None),
        )
        # Legend labels carry the actual strikes: the pinned contract (self) + each wall's highest-OI
        # strike within the window. Falls back to the generic name if a wall strike can't resolve.
        curve["self_label"] = f"{format_strike(contract['strike'])} {contract['option_type']} (Self)"
        call_wall = windowed_max_oi_strike(pair.report, strike_range, "CE")
        put_wall = windowed_max_oi_strike(pair.report, strike_range, "PE")
        curve["call_label"] = f"{format_strike(call_wall)} CE" if call_wall else "Call OI"
        curve["put_label"] = f"{format_strike(put_wall)} PE" if put_wall else "Put OI"
        curve["call_2nd_label"] = f"{format_strike(call_2nd[0])} CE (2nd)" if call_2nd else "Call OI 2nd"
        curve["put_2nd_label"] = f"{format_strike(put_2nd[0])} PE (2nd)" if put_2nd else "Put OI 2nd"
        title = f"{format_strike(contract['strike'])} {contract['option_type']} · {pair.report.get('display_name', '')}"
        entries.append((title, curve))
    return entries


def register_required_move_drawer_callbacks(app, services: RuntimeServices) -> None:
    @app.callback(
        Output("rmd-selected-store", "data"),
        Input("chain-option-table", "active_cell"),
        State("chain-option-table", "data"),
        State("loaded-report-meta-store", "data"),
        State("loaded-report-rows-store", "data"),
        State("active-view-context-store", "data"),
        State("rmd-selected-store", "data"),
        prevent_initial_call=True,
    )
    def select_contract(active_cell, table_data, loaded_meta, loaded_rows, context_data, selected):
        context = ActiveViewContext.from_dict(context_data) if context_data else None
        if not is_live_pair(context) or not loaded_meta:
            return no_update
        loaded_state = LoadedReportState.from_meta_and_rows(loaded_meta, loaded_rows)
        if loaded_state is None or not loaded_state.loaded_pairs:
            return no_update
        pair = get_active_loaded_pair(loaded_state, context.pair_key)
        if pair is None:
            return no_update
        contract = build_contract(
            active_cell,
            table_data,
            asset_id=pair.request.asset_id,
            segment_code=pair.request.segment_code,
            expiry=pair.request.expiry,
            rows=pair.report.get("rows", []) or [],
        )
        if contract is None:
            return no_update
        # Bucket the pin under the active pair (instrument + expiry) so it never bleeds into another
        # pill's view — neither another instrument's nor another expiry of the same instrument.
        return push_to_bucket(selected, pair.request.pair_key.value, contract)

    @app.callback(
        Output("rmd-drawer-body", "children"),
        Input("rmd-selected-store", "data"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        Input("rmd-drawer", "is_open"),
        State("loaded-report-rows-store", "data"),
        State("strike-range-selector", "value"),
        prevent_initial_call=True,
    )
    def render_curves(selected, loaded_meta, context_data, drawer_open, loaded_rows, strike_range):
        # Gate: skip the curve builds while the drawer is closed. `is_open` is an Input, so the
        # drawer auto-opening on a fresh selection (manage_drawer) re-fires this and paints the
        # sparklines. `active-view-context-store` is also an Input so a pair/instrument switch
        # re-renders immediately to the newly-active pair's bucket — not just on the next tick.
        if not drawer_open:
            return no_update
        context = ActiveViewContext.from_dict(context_data) if context_data else None
        if not is_live_pair(context) or not loaded_meta:
            return build_drawer_body([])
        loaded_state = LoadedReportState.from_meta_and_rows(loaded_meta, loaded_rows)
        if loaded_state is None:
            return build_drawer_body([])
        # Render only the active pair's pins (per-pair bucket).
        entries = _entries_for(
            services, loaded_state, bucket_for(selected, context.pair_key), strike_range
        )
        return build_drawer_body(entries)

    @app.callback(
        Output("rmd-selected-store", "data", allow_duplicate=True),
        Input("rmd-clear-button", "n_clicks"),
        State("active-view-context-store", "data"),
        State("rmd-selected-store", "data"),
        prevent_initial_call=True,
    )
    def clear_selection(_n_clicks, context_data, selected):
        # Empty the on-screen pair's bucket (the panel only ever shows the active pair's pins). Other
        # pairs' buckets are left intact. Cascades through `render_curves` (clears the body) and
        # `manage_drawer` (the store change keeps the open drawer open). allow_duplicate because
        # `select_contract` also writes this store.
        context = ActiveViewContext.from_dict(context_data) if context_data else None
        pair_key = context.pair_key if context is not None else None
        return clear_bucket(selected, pair_key)

    @app.callback(
        Output("rmd-drawer", "is_open"),
        Input("rmd-drawer-toggle", "n_clicks"),
        Input("rmd-selected-store", "data"),
        State("rmd-drawer", "is_open"),
        prevent_initial_call=True,
    )
    def manage_drawer(_n_clicks, _selected, is_open):
        # A fresh selection auto-opens the drawer; the arrow button flips it. Owns only `is_open`;
        # the arrow's position + glyph follow it via `sync_drawer_arrow`.
        triggered = callback_context.triggered
        trigger_id = triggered[0]["prop_id"].split(".")[0] if triggered else None
        return True if trigger_id == "rmd-selected-store" else (not is_open)

    @app.callback(
        Output("rmd-drawer-toggle", "style"),
        Output("rmd-drawer-toggle", "children"),
        Input("rmd-drawer", "is_open"),
    )
    def sync_drawer_arrow(is_open):
        # Single owner of the arrow's style + glyph, bound to the drawer's open-state — so the arrow
        # tracks the drawer however it closes (toggle, Esc, header X, or Bootstrap auto-hide on a
        # browser-tab switch). Without this the arrow was orphaned mid-screen when the offcanvas
        # closed itself. Mirrors `ui/shell.sync_playback_arrow`. (▸ pushes the open drawer back, ◂ pulls it out.)
        return drawer_toggle_style(bool(is_open)), ("▸" if is_open else "◂")
