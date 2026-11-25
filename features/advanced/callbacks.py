"""Advanced tab callbacks — independent multi-expiry GEX, ephemeral.

The Run button fetches its own N expiries (no DB writes), computes the analysis,
renders it into the persistent ``advanced-container``, and stores the analysis (+
bottom-panel summary keys) in ``advanced-report-store``. Nothing here listens to
auto-refresh or loaded state, so the result is never disturbed by refresh ticks.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dash import Input, Output, State, no_update

from domain.keys import InstrumentKey
from features.advanced.rendering import (
    build_advanced_dashboard,
    build_advanced_placeholder,
    build_advanced_summary_for_store,
    build_migration_heatmap,
)
from services.runtime import RuntimeServices


def register_advanced_callbacks(app, services: RuntimeServices) -> None:
    @app.callback(
        Output("advanced-dashboard-content", "children"),
        Output("advanced-report-store", "data"),
        Input("advanced-run-button", "n_clicks"),
        State("request-mode-selector", "value"),
        State("asset-selector", "value"),
        State("advanced-expiry-count", "value"),
        prevent_initial_call=True,
    )
    def run_advanced(n_clicks, request_mode, asset_value, expiry_count):
        if not n_clicks:
            return no_update, no_update
        if request_mode == "multi_instrument":
            return build_advanced_placeholder("Advanced analytics runs in single-instrument mode only."), None
        if not expiry_count or str(expiry_count) == "off":
            return build_advanced_placeholder("Select an Advanced Expiries count, then Run."), None
        if services.advanced_analysis_service is None:
            return build_advanced_placeholder("Missing CLIENT_ID or ACCESS_TOKEN.", color="warning"), None
        if not asset_value:
            return build_advanced_placeholder("Select an asset, then Run."), None

        instrument = InstrumentKey.from_value(asset_value)
        # Advanced is hard-pinned to ±16 strikes around spot, independent of the
        # global strike-range selector.
        analysis = services.advanced_analysis_service.run(
            instrument.asset_id, instrument.segment_code, int(expiry_count), "16"
        )
        if analysis is None:
            return build_advanced_placeholder("Could not resolve the selected asset.", color="warning"), None
        if analysis.get("error"):
            return build_advanced_placeholder(analysis["error"], color="warning"), None

        captured_at = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None).isoformat()
        stored = build_advanced_summary_for_store(analysis, captured_at=captured_at)
        return build_advanced_dashboard(analysis), stored

    @app.callback(
        Output("advanced-container", "style"),
        Input("main-tabs", "active_tab"),
    )
    def toggle_advanced_container(active_tab: str):
        return {"display": "block"} if active_tab == "advanced-tab" else {"display": "none"}

    @app.callback(
        Output("advanced-expiry-count", "disabled"),
        Output("advanced-run-button", "disabled"),
        Output("advanced-dashboard-content", "children", allow_duplicate=True),
        Input("request-mode-selector", "value"),
        prevent_initial_call=True,
    )
    def gate_advanced_controls(request_mode: str):
        # Advanced runs on a single instrument (one or many of its expiries) only — fully deactivate
        # the controls in multi-instrument mode and say why. Switching back to single re-enables them
        # and resets to the run prompt (a mode change invalidates any prior run anyway).
        if request_mode == "multi_instrument":
            notice = build_advanced_placeholder(
                "Advanced runs on a single instrument only — switch to Single Instrument mode.",
                color="warning",
            )
            return True, True, notice
        return False, False, build_advanced_placeholder("Run advanced analytics to view the dashboard.")

    @app.callback(
        Output("advanced-heatmap-graph", "figure"),
        Input("advanced-heatmap-mode", "value"),
        State("advanced-report-store", "data"),
        prevent_initial_call=True,
    )
    def switch_heatmap_mode(mode, analysis):
        if not analysis:
            return no_update
        figure = build_migration_heatmap(analysis, mode or "combined")
        return figure if figure is not None else no_update
