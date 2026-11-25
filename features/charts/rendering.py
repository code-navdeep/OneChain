"""Charts tab rendering."""

from __future__ import annotations

from collections import defaultdict

from dash import dcc
import dash_bootstrap_components as dbc
import plotly.graph_objects as go

from analytics.aggregate import build_aggregate_report, is_aggregate_eligible
from state.models import ActiveViewContext, LoadedReportState
from state.selectors import filter_report_rows_by_strike_range, get_active_loaded_pair, split_rows_by_option_type
from ui import theme
from ui.notices import build_notice


_CALL_COLOR = theme.CALL       # coral — Calls (Charts-tab + bottom-panel convention)
_PUT_COLOR = theme.PUT         # teal-green — Puts
_PRICE_LINE_COLOR = theme.AMBER  # amber — underlying spot marker
_DARK_BG = theme.SURFACE


def _apply_chart_theme(figure: go.Figure) -> go.Figure:
    """Consistent dark theme for the Charts-tab figures.

    Title is anchored top-left and the legend top-right so the two never overlap.
    Default drag tool is pan (app-wide rule).
    """
    figure.update_layout(
        template="plotly_dark",
        font={"color": theme.INK, "family": "Geist Mono, monospace"},
        paper_bgcolor=_DARK_BG,
        plot_bgcolor=_DARK_BG,
        margin={"l": 50, "r": 30, "t": 50, "b": 40},
        hovermode="x unified",
        dragmode="pan",
        title={"x": 0, "xanchor": "left"},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},
    )
    figure.update_xaxes(gridcolor=theme.GRID, linecolor=theme.GRID, zeroline=False)
    figure.update_yaxes(gridcolor=theme.GRID, linecolor=theme.GRID, zeroline=False)
    return figure


def _add_price_line(figure: go.Figure, underlying_price) -> None:
    """Draw a vertical marker at the underlying spot on a strike-axis chart."""
    if underlying_price in (None, 0, ""):
        return
    figure.add_vline(
        x=float(underlying_price),
        line_width=1.5,
        line_dash="dash",
        line_color=_PRICE_LINE_COLOR,
        annotation_text=f"Spot {float(underlying_price):,.2f}",
        annotation_position="top",
        annotation_font_color=_PRICE_LINE_COLOR,
    )


def _group_metric(rows: list[dict], metric: str) -> tuple[list[float], list[float]]:
    grouped = defaultdict(float)
    for row in rows:
        grouped[float(row["strike"])] += float(row.get(metric, 0) or 0)
    strikes = sorted(grouped)
    values = [grouped[strike] for strike in strikes]
    return strikes, values


def _build_bar_figure(
    call_rows: list[dict],
    put_rows: list[dict],
    metric: str,
    title: str,
    yaxis_title: str | None = None,
    *,
    underlying_price=None,
) -> go.Figure:
    call_x, call_y = _group_metric(call_rows, metric)
    put_x, put_y = _group_metric(put_rows, metric)
    figure = go.Figure()
    figure.add_bar(name="Calls", x=call_x, y=call_y, marker_color=_CALL_COLOR)
    figure.add_bar(name="Puts", x=put_x, y=put_y, marker_color=_PUT_COLOR)
    figure.update_layout(
        barmode="group",
        title=title,
        xaxis_title="Strike",
        yaxis_title=yaxis_title or metric.replace("_", " ").title(),
    )
    _add_price_line(figure, underlying_price)
    return _apply_chart_theme(figure)


def _build_two_line_figure(
    call_rows: list[dict],
    put_rows: list[dict],
    metric: str,
    title: str,
    yaxis_title: str | None = None,
    *,
    underlying_price=None,
) -> go.Figure:
    """Call vs Put lines for a per-strike metric (greeks)."""
    call_x, call_y = _group_metric(call_rows, metric)
    put_x, put_y = _group_metric(put_rows, metric)
    figure = go.Figure()
    figure.add_scatter(name="Calls", x=call_x, y=call_y, mode="lines+markers", line={"color": _CALL_COLOR})
    figure.add_scatter(name="Puts", x=put_x, y=put_y, mode="lines+markers", line={"color": _PUT_COLOR})
    figure.update_layout(
        title=title,
        xaxis_title="Strike",
        yaxis_title=yaxis_title or metric.replace("_", " ").title(),
    )
    _add_price_line(figure, underlying_price)
    return _apply_chart_theme(figure)


def _build_iv_smile_figure(call_rows: list[dict], put_rows: list[dict], underlying_price=None) -> go.Figure:
    """Separate Call-IV and Put-IV lines per strike (never summed across sides)."""
    call_x, call_y = _group_metric(call_rows, "iv")
    put_x, put_y = _group_metric(put_rows, "iv")
    figure = go.Figure()
    figure.add_scatter(name="Call IV", x=call_x, y=call_y, mode="lines+markers", line={"color": _CALL_COLOR})
    figure.add_scatter(name="Put IV", x=put_x, y=put_y, mode="lines+markers", line={"color": _PUT_COLOR})
    figure.update_layout(title="IV Smile", xaxis_title="Strike", yaxis_title="Implied Volatility")
    _add_price_line(figure, underlying_price)
    return _apply_chart_theme(figure)


def _build_gex_figure(rows: list[dict], underlying_price=None) -> go.Figure:
    """Net (Call+Put) dealer-signed GEX per strike as bars, with a zero line.

    `gamma_exposure` is already dealer-signed (CE +, PE −), so summing both sides
    per strike gives the net — the bars below the zero line show where dealer gamma
    flips negative. Bars are sign-colored (positive green / negative red).
    """
    strikes, values = _group_metric(rows, "gamma_exposure")
    colors = [theme.UP if value >= 0 else theme.DOWN for value in values]
    figure = go.Figure()
    figure.add_bar(name="Net GEX", x=strikes, y=values, marker_color=colors)
    figure.update_layout(
        title="Gamma Exposure by Strike",
        xaxis_title="Strike",
        yaxis_title="Gamma Exposure",
        showlegend=False,
    )
    figure.add_hline(y=0, line_width=1, line_color=theme.BORDER_STRONG)
    _add_price_line(figure, underlying_price)
    return _apply_chart_theme(figure)


def _build_overlay_figure(overlays: list[dict], title: str) -> go.Figure:
    figure = go.Figure()
    for overlay in overlays:
        figure.add_scatter(
            x=overlay["x"],
            y=overlay["y"],
            mode="lines+markers",
            name=str(overlay["label"]),
        )
    figure.update_layout(title=title, xaxis_title="Strike", yaxis_title="IV")
    return _apply_chart_theme(figure)


def _build_chart_grid(
    *,
    report: dict,
    rows: list[dict],
    call_rows: list[dict],
    put_rows: list[dict],
    aggregate_mode: bool = False,
) -> list[dbc.Row]:
    up = report.get("underlying_price")
    grid_rows = [
        dbc.Row(
            [
                dbc.Col(
                    dcc.Graph(
                        figure=_build_bar_figure(
                            call_rows, put_rows, "oi_lots",
                            "Open Interest by Strike", "Open Interest (Lots)",
                            underlying_price=up,
                        )
                    ),
                    md=6,
                ),
                dbc.Col(
                    dcc.Graph(
                        figure=_build_bar_figure(
                            call_rows, put_rows, "oi_change_lots",
                            "OI Change by Strike", "OI Change (Lots)",
                            underlying_price=up,
                        )
                    ),
                    md=6,
                ),
            ],
            className="g-3",
        ),
        dbc.Row(
            [
                dbc.Col(
                    dcc.Graph(
                        figure=_build_bar_figure(
                            call_rows, put_rows, "volume_lots",
                            "Volume by Strike", "Volume (Lots)",
                            underlying_price=up,
                        )
                    ),
                    md=6,
                ),
                dbc.Col(
                    build_notice(
                        "IV Smile is not available in aggregate mode.",
                        tone="info",
                        className="h-100",
                    )
                    if aggregate_mode
                    else dcc.Graph(figure=_build_iv_smile_figure(call_rows, put_rows, up)),
                    md=6,
                ),
            ],
            className="g-3 mt-1",
        ),
        dbc.Row(
            [
                dbc.Col(
                    dcc.Graph(figure=_build_gex_figure(rows, underlying_price=up)),
                    md=12,
                ),
            ],
            className="g-3 mt-1",
        ),
    ]
    if aggregate_mode:
        grid_rows.append(
            dbc.Row(
                [
                    dbc.Col(
                        build_notice(
                            "Aggregate mode hides Delta/Gamma/Theta/Vega distributions because those strike-wise values are non-additive across expiries.",
                        ),
                        md=12,
                    )
                ],
                className="g-3 mt-1",
            )
        )
        return grid_rows

    grid_rows.extend(
        [
            dbc.Row(
                [
                    dbc.Col(dcc.Graph(figure=_build_two_line_figure(call_rows, put_rows, "delta", "Delta Distribution", "Delta", underlying_price=up)), md=6),
                    dbc.Col(dcc.Graph(figure=_build_two_line_figure(call_rows, put_rows, "gamma", "Gamma Distribution", "Gamma", underlying_price=up)), md=6),
                ],
                className="g-3 mt-1",
            ),
            dbc.Row(
                [
                    dbc.Col(dcc.Graph(figure=_build_two_line_figure(call_rows, put_rows, "vega", "Vega Distribution", "Vega", underlying_price=up)), md=6),
                    dbc.Col(dcc.Graph(figure=_build_two_line_figure(call_rows, put_rows, "theta", "Theta Distribution", "Theta", underlying_price=up)), md=6),
                ],
                className="g-3 mt-1",
            ),
        ]
    )
    return grid_rows


def build_charts_tab(
    loaded_report_state: LoadedReportState | None,
    active_pair_key: str | None,
    strike_range: str,
    active_view_context: ActiveViewContext | None = None,
) -> dbc.Card:
    """Render the Charts tab from loaded state only."""
    if (
        loaded_report_state is not None
        and active_view_context is not None
        and active_view_context.kind == "aggregate"
        and is_aggregate_eligible(loaded_report_state)
    ):
        aggregate_report = build_aggregate_report(loaded_report_state.loaded_pairs)
        if aggregate_report is None:
            return build_notice("Aggregate charts are not available for this loaded set.")

        rows = filter_report_rows_by_strike_range(aggregate_report, strike_range)
        call_rows, put_rows = split_rows_by_option_type(rows)
        return dbc.Card(
            dbc.CardBody(
                _build_chart_grid(
                    report=aggregate_report,
                    rows=rows,
                    call_rows=call_rows,
                    put_rows=put_rows,
                    aggregate_mode=True,
                )
            )
        )

    active_pair = get_active_loaded_pair(loaded_report_state, active_pair_key)
    if active_pair is None:
        return build_notice("No live data loaded yet.")

    report = dict(active_pair.report)
    rows = filter_report_rows_by_strike_range(report, strike_range)
    call_rows, put_rows = split_rows_by_option_type(rows)

    return dbc.Card(
        dbc.CardBody(
            _build_chart_grid(
                report=report,
                rows=rows,
                call_rows=call_rows,
                put_rows=put_rows,
                aggregate_mode=False,
            )
        )
    )
