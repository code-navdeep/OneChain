"""Bottom-panel chart callbacks — static Price + IV-vs-RV figures, refreshed live.

The two charts are mounted once in the shell (``ui/shell._build_bottom_panel_shell``); these
callbacks swap only their ``.figure`` on each live tick / window change, so the charts keep
refreshing without the DOM (or any controls) being torn down. Pair scoping mirrors
``build_bottom_panels`` via the shared ``resolve_panel_context``. The Aggregate-GEX + Risk-Reversal
charts beside them are driven by ``features/options_analysis/callbacks.py``.
"""

from __future__ import annotations

from dash import Input, Output, State, no_update

from features.bottom_panels.rendering import (
    build_iv_rv_figure,
    build_price_figure,
    resolve_panel_context,
)
from services.runtime import RuntimeServices
from state.models import ActiveViewContext, LoadedReportState
from state.selectors import windowed_max_oi_pain


def _context_from_stores(meta_data: dict | None, rows_data: dict | None, view_context_data: dict | None):
    # Hydrate meta + rows (resolve_panel_context needs rows for the price-chart walls and for
    # build_aggregate_report in aggregate mode).
    loaded_state = LoadedReportState.from_meta_and_rows(meta_data, rows_data)
    context_obj = ActiveViewContext.from_dict(view_context_data) if view_context_data else None
    active_pair_key = context_obj.pair_key if context_obj is not None else None
    return resolve_panel_context(loaded_state, active_pair_key, context_obj)


def register_bottom_panel_callbacks(app, services: RuntimeServices) -> None:
    view = services.view_data_service

    @app.callback(
        Output("bottom-price-chart-graph", "figure"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        Input("bottom-panel-collapse", "is_open"),
        Input("bottom-charts-mounted", "data"),
        State("loaded-report-rows-store", "data"),
        State("strike-range-selector", "value"),
        prevent_initial_call=True,
    )
    def render_price_chart(loaded_meta, view_context_data, panel_open, _mounted, loaded_rows, strike_range):
        # Gate: don't rebuild the figure while the panel is collapsed. `is_open` is an Input, so
        # opening the panel re-fires this and paints fresh.
        if not panel_open:
            return no_update
        context = _context_from_stores(loaded_meta, loaded_rows, view_context_data)
        if context is None:
            return build_price_figure([], "")
        request = context["request"]
        # Single-session per-fetch spot series (from pair_reports). The service owns the session
        # floor + the live incremental cache; live (context_end None) is cached today-only, playback
        # (context_end set) is a one-off bounded read.
        price_series = view.get_underlying_price_series(
            asset_id=request.asset_id,
            segment_code=request.segment_code,
            expiry=None if context["aggregate"] else request.expiry,
            end_timestamp=context["context_end_timestamp"],
        )
        report = context["report"]
        # Walls = the highest-OI strike's pain point **within the current strike-range window**, not
        # the full-chain levels — so they match what the chain table shows and jump with the selector.
        call_pain = windowed_max_oi_pain(report, strike_range, "CE")
        put_pain = windowed_max_oi_pain(report, strike_range, "PE")
        return build_price_figure(
            price_series, request.display_name,
            call_pain_level=call_pain, put_pain_level=put_pain,
        )

    @app.callback(
        Output("bottom-iv-rv-chart-graph", "figure"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        Input("bottom-panel-window-selector", "value"),
        Input("bottom-panel-collapse", "is_open"),
        Input("bottom-charts-mounted", "data"),
        State("loaded-report-rows-store", "data"),
        prevent_initial_call=True,
    )
    def render_iv_rv_chart(loaded_meta, view_context_data, window_value, panel_open, _mounted, loaded_rows):
        if not panel_open:  # gate while collapsed; re-fires on open (is_open is an Input)
            return no_update
        window = int(window_value or 22)  # 22 = the shortest real window (22/90/180/252)
        context = _context_from_stores(loaded_meta, loaded_rows, view_context_data)
        if context is None:
            return build_iv_rv_figure([], window, "", aggregate=False)
        request = context["request"]
        # Full history up to the loaded context's date — the window selects the column-family read,
        # not the row count.
        instrument_history = view.get_instrument_historical_metrics(
            asset_id=request.asset_id,
            segment_code=request.segment_code,
            end_date=context["context_end_date"],
        )
        return build_iv_rv_figure(instrument_history, window, request.display_name, aggregate=context["aggregate"])
