"""Options Analysis static building blocks (per-option time-series surfaces).

The four time-series charts are mounted **once** in the shell — never rebuilt by a tick —
so their selectors keep state across data refreshes. They now live on two always-on surfaces
instead of the Charts tab:

- The two per-option **metric charts** → the **left slide-in drawer** (``build_metric_drawer_shell``),
  opened by clicking the chain's underlying-price cell.
- The **Aggregate-GEX** + **25Δ Risk-Reversal** charts → the **bottom analytics panel**
  (``build_gex_rr_charts_row``), beside the Price | IV-vs-RV row.

The interactivity (live triggers, pair resolution) lives in
``features/options_analysis/callbacks.py``.
"""

from __future__ import annotations

from dash import dcc, html
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc

from features.options_analysis.rendering import (
    build_aggregate_gex_chart,
    build_metric_chart,
    build_risk_reversal_chart,
)


# Dark-native dmc.Select styling (same Styles API as the command bar / Advanced — legible menu).
_SELECT_STYLES = {
    "input": {"backgroundColor": "#101725", "color": "#e8edf7", "borderColor": "#2a3a5c"},
    "dropdown": {"backgroundColor": "#18233c", "borderColor": "#2a3a5c"},
    "option": {"color": "#e8edf7"},
    "label": {"color": "#8a97b5", "fontWeight": 600, "fontSize": "0.72rem", "marginBottom": "2px"},
    "pill": {"backgroundColor": "rgba(34,211,168,0.18)", "color": "#bff3e4"},
    "empty": {"color": "#8a97b5"},
}
_COMBO = {"zIndex": 3000}


# Metric short-label -> stored column. Counts are the lot-scaled `*_lots` columns so the axis
# label "(Lots)" is truthful. Order and short labels as shown (compact dropdown).
OA_METRICS: list[dict[str, str]] = [
    {"label": "LTP", "value": "last_price"},
    {"label": "BS Price", "value": "bs_price"},
    {"label": "Volume", "value": "volume_lots"},
    {"label": "OI", "value": "oi_lots"},
    {"label": "OI Chg", "value": "oi_change_lots"},
    {"label": "IV", "value": "iv"},
    {"label": "GEX", "value": "gamma_exposure"},
    {"label": "VEX", "value": "volatility_exposure"},
    {"label": "Delta", "value": "delta"},
    {"label": "Gamma", "value": "gamma"},
    {"label": "Theta", "value": "theta"},
    {"label": "Vega", "value": "vega"},
]


_DEFAULT_METRICS = ["last_price", "iv", "delta", "gamma"]


def _metric_chart_card(index: int, default_metric: str) -> html.Div:
    """Bare dark metric chart (no card/header chrome) — selector row + graph, matching the
    Price/IV-RV charts. The metric dropdown above labels it; the figure carries the dark theme
    (`_apply_oa_theme` → `theme.SURFACE`) and pans by default. Initial figure is the dark
    placeholder so the very first paint is on-theme, never a white default plot.

    The card is a flex column (`flex: 1 1 0`): the selector row stays fixed-height and the
    responsive graph fills the rest, so two cards stacked in `build_metric_drawer_body` split
    the drawer's height evenly (no fixed 500px overflow). Mirrors the right required-move drawer."""
    return html.Div(
        [
            dbc.Row(
                [
                    dbc.Col(
                        dmc.Select(
                            id=f"oa-metric-{index}",
                            data=OA_METRICS,
                            value=default_metric,
                            allowDeselect=False,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        width=3,
                    ),
                    dbc.Col(
                        dmc.MultiSelect(
                            id=f"oa-contracts-{index}",
                            data=[],
                            placeholder="Choose contracts to compare…",
                            searchable=True,
                            clearable=True,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        width=9,
                    ),
                ],
                className="mb-1 g-2",
            ),
            dcc.Graph(
                id=f"oa-chart-{index}",
                figure=build_metric_chart([], default_metric, "tight10"),
                config={"displayModeBar": False, "responsive": True},
                style={"flex": "1 1 0", "minHeight": 0, "width": "100%"},
            ),
        ],
        style={"display": "flex", "flexDirection": "column", "flex": "1 1 0", "minHeight": 0},
    )


def _aggregate_gex_card() -> html.Div:
    """Bare dark Aggregate-GEX chart (no card/header chrome, no selectors) — today-only, a rolling
    30-min window. The figure's own top-left title names it; dark theme + pan default. Initial figure
    is the dark placeholder (no white pre-data flash)."""
    return html.Div(
        dcc.Graph(
            id="oa-aggregate-gex-chart",
            figure=build_aggregate_gex_chart([], "", ""),
            config={"displayModeBar": True},
            style={"height": "400px"},
        ),
        className="h-100",
    )


def _risk_reversal_card() -> html.Div:
    """Bare dark 25Δ Risk-Reversal chart (no card/header chrome, no selectors) — today-only, a
    rolling 30-min window. The figure's own top-left title names it; dark theme + pan default.
    Initial figure is the dark placeholder."""
    return html.Div(
        dcc.Graph(
            id="oa-rr-chart",
            figure=build_risk_reversal_chart([], "", ""),
            config={"displayModeBar": True},
            style={"height": "400px"},
        ),
        className="h-100",
    )


# Left metric drawer — 40% wide (consistent with the right required-move drawer), mirrors its
# shell pattern (features/required_move_drawer/rendering.build_drawer_shell) and the #playback-drawer styling.
_METRIC_DRAWER_WIDTH = "40%"


def build_metric_charts() -> list:
    """The two stacked per-option metric chart cards — the drawer body's content. Injected lazily
    into the body's mount point on the drawer's **first open** (the `ui/shell` mount callback)
    instead of at cold load, so Plotly isn't initialized for these charts until the drawer is first
    used (lazy mount)."""
    return [_metric_chart_card(i, _DEFAULT_METRICS[i - 1]) for i in range(1, 3)]


def build_metric_drawer_body() -> html.Div:
    """Placeholder mount point for the two metric charts — filled by `build_metric_charts()` on the
    drawer's first open (lazy mount). The flex-column styling stays here so the injected cards split
    the drawer height evenly (`flex: 1` each). Mirrors `required_move_drawer.build_drawer_body`."""
    return html.Div(
        id="metric-charts-mount",
        style={"display": "flex", "flexDirection": "column", "height": "100%", "gap": "6px"},
    )


def metric_drawer_toggle_style(is_open: bool) -> dict:
    """Pin the arrow to the drawer's right edge when open, to the screen's left edge when closed.

    Left-side mirror of ``required_move_drawer.rendering.drawer_toggle_style``.
    """
    return {
        "position": "fixed",
        "left": _METRIC_DRAWER_WIDTH if is_open else "0",
        "top": "45%",
        # zIndex 1900: below the bottom analytics panel (2000) so an open panel covers the arrow,
        # above the sticky tab bars. Mirror of the required-move arrow.
        "zIndex": 1900,
        "borderRadius": "0 6px 6px 0",
        "padding": "10px 6px",
        "transition": "left 0.3s ease",
    }


def build_metric_drawer_shell() -> html.Div:
    """Static left Offcanvas (the two metric charts) + its fixed arrow toggle, mounted once in the shell."""
    toggle = dbc.Button(
        "▸",
        id="metric-drawer-toggle",
        n_clicks=0,
        color="dark",
        size="sm",
        style=metric_drawer_toggle_style(False),
    )
    drawer = dbc.Offcanvas(
        build_metric_drawer_body(),
        id="metric-drawer",
        title=html.Span("Option Time-Series", style={"fontSize": "0.9rem", "fontWeight": 600, "whiteSpace": "nowrap"}),
        placement="start",
        is_open=False,
        backdrop=False,
        scrollable=True,
        # No header X — the fixed arrow toggle (metric-drawer-toggle) is the sole open/close control,
        # so the X is redundant (mirrors the reversal drawer).
        close_button=False,
        # zIndex 1500 lifts the drawer above the sticky sub-tabs (1190); below the analytics panel (2000).
        style={"width": _METRIC_DRAWER_WIDTH, "zIndex": 1500},
    )
    return html.Div([toggle, drawer])


def build_gex_rr_charts_row() -> dbc.Row:
    """Aggregate-GEX | Risk-Reversal side-by-side row for the bottom analytics panel —
    mirrors the Price | IV-vs-RV row (two equal `md=6` columns), within the panel's 75% column."""
    return dbc.Row(
        [
            dbc.Col(_aggregate_gex_card(), md=6),
            dbc.Col(_risk_reversal_card(), md=6),
        ],
        className="g-2 mt-1",
    )
