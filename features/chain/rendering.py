"""Chain tab rendering."""

from __future__ import annotations

import dash_bootstrap_components as dbc

from analytics.aggregate import build_aggregate_report, is_aggregate_eligible
from features.chain.table_model import build_option_chain_table
from services.view_data_service import ViewDataService
from state.models import (
    ActiveViewContext,
    LoadedReportState,
)
from state.selectors import filter_report_rows_by_strike_range, get_active_loaded_pair
from ui.notices import build_notice


def resolve_chain_report(
    loaded_report_state: LoadedReportState | None,
    active_pair_key: str | None,
    strike_range: str,
    active_view_context: ActiveViewContext | None = None,
) -> tuple[dict, list[dict]] | None:
    """Resolve the active pair/aggregate into ``(report_with_request, display_rows)``, or ``None``
    when nothing is loaded. Shared by ``build_chain_tab`` (initial render) and the greeks-toggle
    callback (`ui/shell.swap_greeks_columns`) so both see the exact same report + windowed rows."""
    if (
        loaded_report_state is not None
        and active_view_context is not None
        and active_view_context.kind == "aggregate"
        and is_aggregate_eligible(loaded_report_state)
    ):
        aggregate_report = build_aggregate_report(loaded_report_state.loaded_pairs)
        if aggregate_report is not None:
            report = dict(aggregate_report)
            report["request"] = loaded_report_state.loaded_pairs[0].request.to_dict()
            display_rows = filter_report_rows_by_strike_range(aggregate_report, strike_range)
            return report, display_rows

    active_pair = get_active_loaded_pair(loaded_report_state, active_pair_key)
    if active_pair is None:
        return None
    report = dict(active_pair.report)
    report["request"] = active_pair.request.to_dict()
    display_rows = filter_report_rows_by_strike_range(active_pair.report, strike_range)
    return report, display_rows


def build_chain_tab(
    loaded_report_state: LoadedReportState | None,
    active_pair_key: str | None,
    strike_range: str,
    view_data_service: ViewDataService,
    active_view_context: ActiveViewContext | None = None,
    show_greeks: bool = False,
    rate_markers: dict | None = None,
    interpretor: dict | None = None,
    hover: dict | None = None,
) -> dbc.Card:
    """Render the shipping Chain tab from loaded state only.

    The strike range is a **display** filter — it selects which strike rows the grid
    paints. The full-chain summary (totals, max pain, support/resistance) is taken from
    the loaded pair's report unchanged, so those market values never shift with the view.

    ``rate_markers`` (the Tier-2 Layer-1 states, ``{(strike, side, rule): "up"|"down"}``) is passed
    straight through to the table style, which paints the thin marker columns natively (white=up /
    sky-blue=down). Only shown in the non-greeks view.
    """
    resolved = resolve_chain_report(loaded_report_state, active_pair_key, strike_range, active_view_context)
    if resolved is None:
        return build_notice("No live data loaded yet.")
    report, display_rows = resolved
    return dbc.Card(
        dbc.CardBody(
            build_option_chain_table(
                report,
                view_data_service=view_data_service,
                show_greeks=show_greeks,
                display_rows=display_rows,
                rate_markers=rate_markers,
                interpretor=interpretor,
                hover=hover,
            ),
        ),
        className="onechain-chain-card",
    )
