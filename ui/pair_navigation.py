"""Secondary pair-row rendering."""

from __future__ import annotations

from dash import html
import dash_bootstrap_components as dbc

from state.models import LoadedReportState, RequestPlanState
from ui.formatters import format_expiry_label_short


def build_pair_tab_label(display_name: str, expiry: str) -> str:
    """Both the asset name and the short (yearless) expiry, separated by ``|``."""
    return f"{display_name} | {format_expiry_label_short(expiry)}"


def build_pair_navigation(
    *,
    request_plan_state: RequestPlanState | None,
    loaded_report_state: LoadedReportState | None,
    active_pair_key: str | None,
) -> html.Div | dbc.Card:
    """Render the secondary pair row from effective pair state."""
    if loaded_report_state is None or len(loaded_report_state.loaded_pairs) <= 1:
        return html.Div(
            dbc.Tabs(
                [],
                id="loaded-pair-tabs",
                active_tab=active_pair_key,
                style={"display": "none"},
            ),
            id="secondary-row-card",
            className="d-none",
        )

    pairs = loaded_report_state.loaded_pairs

    pair_key_values = {
        (pair.resolved_view_key if hasattr(pair, "resolved_view_key") else pair.request.pair_key.value)
        for pair in pairs
    }
    default_pair_key = pairs[0].resolved_view_key if hasattr(pairs[0], "resolved_view_key") else pairs[0].request.pair_key.value
    resolved_active = active_pair_key if active_pair_key in pair_key_values else default_pair_key
    tabs = []
    for pair in pairs:
        tab_id = pair.resolved_view_key if hasattr(pair, "resolved_view_key") else pair.request.pair_key.value
        label = build_pair_tab_label(pair.request.display_name, pair.request.expiry)
        tabs.append(
            dbc.Tab(
                label=label,
                tab_id=tab_id,
            )
        )
    return dbc.Card(
        dbc.CardBody(
            dbc.Tabs(
                tabs,
                id="loaded-pair-tabs",
                active_tab=resolved_active,
            ),
        ),
        id="secondary-row-card",
        className="onechain-secondary-row-card",
    )
