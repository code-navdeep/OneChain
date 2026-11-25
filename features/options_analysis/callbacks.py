"""Options Analysis callbacks (relocated time-series surfaces).

Drives the four time-series charts that moved off the Charts tab onto two always-on surfaces:

- The two per-option **metric charts** in the left drawer — opened by clicking the chain's
  underlying-price cell (``manage_metric_drawer``), their contracts/figures refreshing **live**.
- The **Aggregate-GEX** + **25Δ Risk-Reversal** charts in the bottom analytics panel.

Unlike the old Charts tab (token-gated to tab-entry), every chart here keys off the live
``loaded-report-meta-store`` + ``active-view-context-store`` — a deliberate trade: always-updating,
pair-specific surfaces. Pair resolution (``_resolve_pair`` →
``get_active_loaded_pair``) is unchanged; the static selectors keep their state across ticks.
"""

from __future__ import annotations

from dash import Input, Output, State, ctx, no_update

from features.options_analysis.layout import metric_drawer_toggle_style
from features.options_analysis.rendering import (
    build_aggregate_gex_chart,
    build_metric_chart,
    build_risk_reversal_chart,
)
from services.runtime import RuntimeServices
from state.models import ActiveViewContext, LoadedReportState
from state.selectors import get_active_loaded_pair
from ui.formatters import format_strike


def _resolve_pair(loaded_data: dict | None, view_context_data: dict | None):
    if not loaded_data:
        return None
    loaded_state = LoadedReportState.from_dict(loaded_data)
    if loaded_state is None or not loaded_state.loaded_pairs:
        return None
    context = ActiveViewContext.from_dict(view_context_data) if view_context_data else None
    return get_active_loaded_pair(loaded_state, context.pair_key if context is not None else None)


def _is_underlying_price_cell(active_cell: dict | None, table_data: list[dict] | None) -> bool:
    """True only for the one cell the left drawer opens on: the spliced underlying-price row's
    strike cell (its value starts with ₹, the same marker `required_move_drawer.build_contract`
    uses to *reject* that row). Any other cell — option LTPs, normal strikes — returns False."""
    if not active_cell or not table_data:
        return False
    if str(active_cell.get("column_id")) != "strike":
        return False
    row_idx = active_cell.get("row")
    if row_idx is None or row_idx >= len(table_data):
        return False
    return str(table_data[row_idx].get("strike", "")).lstrip().startswith("₹")


def _filter_contracts(contracts: list[dict], underlying_price: float, strike_range) -> list[dict]:
    if not contracts or strike_range in (None, "All"):
        return contracts
    try:
        each_side = int(strike_range)
    except (TypeError, ValueError):
        return contracts
    strikes = sorted({float(c["strike"]) for c in contracts})
    if not strikes or underlying_price <= 0:
        return contracts
    # Same convention as state.selectors.filter_report_rows_by_strike_range: N strikes
    # strictly below the price + N strictly above (= 2N); on-strike edge → that strike is
    # the center, ±N around it (= 2N+1).
    on_strike = [s for s in strikes if abs(s - underlying_price) < 1e-6]
    if on_strike:
        center = on_strike[0]
        below = [s for s in strikes if s < center][-each_side:]
        above = [s for s in strikes if s > center][:each_side]
        keep = set(below) | {center} | set(above)
    else:
        below = [s for s in strikes if s < underlying_price][-each_side:]
        above = [s for s in strikes if s > underlying_price][:each_side]
        keep = set(below) | set(above)
    return [c for c in contracts if float(c["strike"]) in keep]


def _contract_options(contracts: list[dict]) -> list[dict]:
    options = []
    for c in contracts:
        strike = format_strike(c["strike"])
        option_type = str(c["option_type"])
        expiry = str(c["expiry"])
        options.append({"label": f"{strike} {option_type}", "value": f"{strike}_{option_type}_{expiry}"})
    return options


def _parse_contract_values(values: list[str] | None) -> list[tuple[float, str]]:
    parsed: list[tuple[float, str]] = []
    for value in values or []:
        parts = str(value).split("_")
        if len(parts) >= 2:
            try:
                parsed.append((float(parts[0]), parts[1]))
            except (TypeError, ValueError):
                continue
    return parsed


# ── Per-pair metric-chart selection persistence (oa-selections-store) ──
# Pure helpers (unit-tested) mirroring the reversal drawer's bucket helpers. The store is
# `{pair_key: {"1": [contract values], "2": [...]}}`; each sub-tab (pair) keeps its own picks.
def read_pair_selection(store: dict | None, pair_key: str | None, index: int, valid: set | None = None) -> list:
    """The saved selection for ``(pair_key, chart index)``. When ``valid`` is given, drop any value
    not in it (contracts that aged out of the current strike window), preserving order."""
    if not pair_key:
        return []
    saved = list(((store or {}).get(pair_key, {}) or {}).get(str(index), []) or [])
    if valid is None:
        return saved
    return [v for v in saved if v in valid]


def write_pair_selection(store: dict | None, pair_key: str, index: int, values: list | None) -> dict:
    """Return a new store with ``(pair_key, chart index)``'s selection set to ``values`` — other
    pairs/charts untouched."""
    out = dict(store or {})
    pair = dict(out.get(pair_key, {}) or {})
    pair[str(index)] = list(values or [])
    out[pair_key] = pair
    return out


def read_restore_stash(data_store: dict | None) -> tuple[list, list] | None:
    """The one-shot ``restore`` stash from an ``oa-data-store`` write — ``(chart-1 values,
    chart-2 values)`` — or ``None`` when the write carried no stash (a same-pair refresh:
    leave the pickers untouched)."""
    stash = (data_store or {}).get("restore")
    if stash is None:
        return None
    return list(stash.get("1", []) or []), list(stash.get("2", []) or [])


def register_options_analysis_callbacks(app, services: RuntimeServices) -> None:
    view = services.view_data_service

    # ── Left metric drawer: open on the chain's underlying-price cell, or flip via the arrow ──
    @app.callback(
        Output("metric-drawer", "is_open"),
        Input("chain-option-table", "active_cell"),
        Input("metric-drawer-toggle", "n_clicks"),
        State("chain-option-table", "data"),
        State("metric-drawer", "is_open"),
        State("active-view-context-store", "data"),
        prevent_initial_call=True,
    )
    def manage_metric_drawer(active_cell, _n_clicks, table_data, is_open, view_context_data):
        # A click on the underlying-price cell auto-opens the drawer; the arrow button flips it.
        # Owns only `is_open`; the arrow's position + glyph follow it via `sync_metric_arrow`.
        trigger = ctx.triggered_id
        if trigger == "metric-drawer-toggle":
            return not is_open
        # Live + per-pair only (mirrors the reversal drawer's is_live_pair): a cell click in
        # aggregate / playback must NOT auto-open the drawer — the panel has no meaning there.
        context = ActiveViewContext.from_dict(view_context_data) if view_context_data else None
        is_live_pair = context is not None and context.source == "live" and context.kind == "pair"
        if trigger == "chain-option-table" and is_live_pair and _is_underlying_price_cell(active_cell, table_data):
            return True
        return no_update

    @app.callback(
        Output("metric-drawer-toggle", "style"),
        Output("metric-drawer-toggle", "children"),
        Input("metric-drawer", "is_open"),
    )
    def sync_metric_arrow(is_open):
        # Single owner of the arrow style + glyph, bound to the drawer's open-state — so the arrow
        # tracks the drawer however it closes (toggle, Esc, X, or Bootstrap auto-hide on a tab
        # switch), instead of being left orphaned mid-screen. Mirrors `ui/shell.sync_playback_arrow`.
        # (◂ pushes the open drawer back, ▸ pulls it out.)
        return metric_drawer_toggle_style(bool(is_open)), ("◂" if is_open else "▸")

    # ── Metric charts (left drawer): live contract population + per-chart figures ──
    @app.callback(
        [Output(f"oa-contracts-{i}", "data") for i in range(1, 3)]
        + [Output("oa-data-store", "data")],
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        Input("strike-range-selector", "value"),
        Input("metric-drawer", "is_open"),
        Input("metric-charts-mounted", "data"),
        State("oa-data-store", "data"),
        State("oa-selections-store", "data"),
        prevent_initial_call=True,
    )
    def populate_oa_contracts(loaded_data, view_context_data, strike_range, drawer_open, _mounted, prev_store, selections_store):
        # Gate: the left metric drawer's work is skipped while it's closed. `is_open` is an Input so
        # opening the drawer re-fires this and repopulates the contract pickers + oa-data-store.
        if not drawer_open:
            return no_update, no_update, no_update
        pair = _resolve_pair(loaded_data, view_context_data)
        if pair is None:
            return [], [], {"restore": {"1": [], "2": []}}
        asset_id = pair.request.asset_id
        segment_code = pair.request.segment_code
        expiry = pair.request.expiry
        underlying_price = float(pair.report.get("underlying_price", 0) or 0)
        contracts = view.get_oa_available_contracts(asset_id, segment_code, expiry)
        contracts = _filter_contracts(contracts, underlying_price, strike_range)
        options = _contract_options(contracts)
        data_store = {
            "asset_id": asset_id,
            "segment_code": segment_code,
            "expiry": expiry,
            "display_name": pair.request.display_name,
            "underlying_price": underlying_price,
        }
        # NOTE — this callback must NEVER write `oa-contracts-{i}.value` itself: the dmc MultiSelect
        # wrapper reacts to a `data` prop change by re-filtering its CURRENT (stale) selection against
        # the new options and setting THAT as the value, clobbering any `value` delivered in the same
        # response (dmc 0.14.7, module 76904's `useDidUpdate([data])` effect). So on a pair change the
        # restored picks travel as a one-shot `restore` stash inside oa-data-store instead;
        # `restore_oa_selections` applies them in a follow-up response, after the data-change effect
        # has already run. The stash is read from oa-selections-store HERE — before the wrapper's
        # transient empty emission can be saved over it — dropping any contract that aged out of the
        # new strike window. Same-pair refreshes (tick / strike-range change) carry NO stash, so the
        # follow-up no-ops and can never clobber an in-flight user pick.
        prev = prev_store or {}
        pair_changed = (prev.get("asset_id"), prev.get("segment_code"), prev.get("expiry")) != (asset_id, segment_code, expiry)
        if pair_changed:
            pair_key = pair.request.pair_key.value
            valid = {opt["value"] for opt in options}
            data_store["restore"] = {
                "1": read_pair_selection(selections_store, pair_key, 1, valid),
                "2": read_pair_selection(selections_store, pair_key, 2, valid),
            }
        return options, options, data_store

    @app.callback(
        [Output(f"oa-contracts-{i}", "value") for i in range(1, 3)],
        Input("oa-data-store", "data"),
        prevent_initial_call=True,
    )
    def restore_oa_selections(data_store):
        # Second half of the sequenced restore (see populate_oa_contracts): applies the one-shot
        # `restore` stash in its OWN response — `value` changes while `data` does not, so the dmc
        # wrapper's data-change effect can't clobber it. The wrapper then reports the restored value
        # back, and save_oa_selections re-persists it — overwriting the transient empty the wrapper
        # emitted (and save recorded) when the new `data` landed. Fires on every oa-data-store write,
        # but same-pair writes carry no stash → no_update, so live ticks can never undo a user pick.
        stash = read_restore_stash(data_store)
        if stash is None:
            return no_update, no_update
        return stash

    @app.callback(
        Output("oa-selections-store", "data"),
        Input("oa-contracts-1", "value"),
        Input("oa-contracts-2", "value"),
        State("active-view-context-store", "data"),
        State("oa-selections-store", "data"),
        prevent_initial_call=True,
    )
    def save_oa_selections(value_1, value_2, view_context_data, selections_store):
        # Persist the two pickers under the active pair_key (from active-view-context — already the
        # new pair by the time any pair-switch-driven value change lands here). Value changes come
        # from user picks, the dmc wrapper's own emissions, and restore_oa_selections. On a pair
        # switch the wrapper first emits a transient [] (its data-change effect), which briefly
        # persists here — harmless, because the restore's value write follows in the same wave and
        # re-persists the real picks over it. Live + per-pair only, so aggregate/playback never
        # write. populate reads this store as State (not Input) → no loop.
        context = ActiveViewContext.from_dict(view_context_data) if view_context_data else None
        if context is None or context.source != "live" or context.kind != "pair" or not context.pair_key:
            return no_update
        store = write_pair_selection(selections_store, context.pair_key, 1, value_1)
        store = write_pair_selection(store, context.pair_key, 2, value_2)
        return store

    def _metric_chart_callback(index: int):
        @app.callback(
            Output(f"oa-chart-{index}", "figure"),
            Input("loaded-report-meta-store", "data"),
            Input(f"oa-metric-{index}", "value"),
            Input(f"oa-contracts-{index}", "value"),
            Input("oa-data-store", "data"),
            Input("metric-drawer", "is_open"),
            Input("metric-charts-mounted", "data"),
            prevent_initial_call=True,
        )
        def _update(_loaded, metric, contract_values, data_store, drawer_open, _mounted):
            # Re-renders on each live tick (loaded store), on the user's metric/contracts change, or
            # when `oa-data-store` is repopulated (pair switch). Gated: no figure build while the left
            # drawer is closed (re-fires on open — is_open is an Input). The y-margin is fixed at
            # ±10% ("tight10") — the per-chart y-range selector was removed.
            if not drawer_open:
                return no_update
            data_store = data_store or {}
            contracts = _parse_contract_values(contract_values)
            if not metric or not contracts or not data_store.get("asset_id"):
                return build_metric_chart([], metric or "", "tight10")
            series = view.get_oa_option_timeseries(
                data_store["asset_id"], data_store["segment_code"], data_store["expiry"],
                contracts, metric,
            )
            return build_metric_chart(series, metric, "tight10")

        return _update

    for chart_index in range(1, 3):
        _metric_chart_callback(chart_index)

    # ── Aggregate-GEX chart (bottom panel) — today-only, no selectors ──
    @app.callback(
        Output("oa-aggregate-gex-chart", "figure"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        Input("bottom-panel-collapse", "is_open"),
        Input("bottom-charts-mounted", "data"),
        prevent_initial_call=True,
    )
    def update_oa_aggregate_gex(loaded_data, view_context_data, panel_open, _mounted):
        if not panel_open:  # gate while the panel is collapsed; re-fires on open
            return no_update
        pair = _resolve_pair(loaded_data, view_context_data)
        if pair is None:
            return build_aggregate_gex_chart([], "", "")
        gex_rows = view.get_oa_aggregate_gex(
            pair.request.asset_id, pair.request.segment_code, pair.request.expiry,
        )
        return build_aggregate_gex_chart(gex_rows, pair.request.display_name, pair.request.expiry)

    # ── 25Δ Risk-Reversal chart (bottom panel) — today-only, no selectors ──
    @app.callback(
        Output("oa-rr-chart", "figure"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        Input("bottom-panel-collapse", "is_open"),
        Input("bottom-charts-mounted", "data"),
        prevent_initial_call=True,
    )
    def update_oa_risk_reversal(loaded_data, view_context_data, panel_open, _mounted):
        if not panel_open:  # gate while the panel is collapsed; re-fires on open
            return no_update
        pair = _resolve_pair(loaded_data, view_context_data)
        if pair is None:
            return build_risk_reversal_chart([], "", "")
        series = view.get_risk_reversal_series(
            pair.request.asset_id, pair.request.segment_code, pair.request.expiry,
        )
        return build_risk_reversal_chart(series, pair.request.display_name, pair.request.expiry)
