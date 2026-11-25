"""Advanced tab rendering.

Builds the 5 charts + the GEX-regime summary from a stored, JSON-safe
analysis dict (produced by ``analytics.advanced_gex`` via ``AdvancedAnalysisService``).
Pure rendering — no fetching, no state. Dark-themed to match the rest of the app.
"""

from __future__ import annotations

from dash import dcc, html
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from scipy.interpolate import griddata

from analytics.report_builder import bracketing_atm_iv
from ui import theme
from ui.formatters import format_expiry_label, format_expiry_label_short, format_strike
from ui.notices import build_notice

_DARK_BG = theme.SURFACE

# Dark-native dmc.Select styling (same Styles API that fixed the builder dropdowns — legible menu).
_SELECT_STYLES = {
    "input": {"backgroundColor": "#101725", "color": "#e8edf7", "borderColor": "#2a3a5c"},
    "dropdown": {"backgroundColor": "#18233c", "borderColor": "#2a3a5c"},
    "option": {"color": "#e8edf7"},
    "label": {"color": "#8a97b5", "fontWeight": 600, "fontSize": "0.72rem", "marginBottom": "2px"},
}
_EXPIRY_COLORS = [theme.CALL, theme.PUT, theme.AMBER, theme.VIOLET, theme.INFO]


def _theme(figure: go.Figure, *, height: int) -> go.Figure:
    figure.update_layout(
        template="plotly_dark",
        paper_bgcolor=_DARK_BG,
        plot_bgcolor=_DARK_BG,
        font={"color": theme.INK, "family": "Geist Mono, monospace"},
        height=height,
        margin={"l": 55, "r": 40, "t": 60, "b": 45},
        dragmode="pan",
    )
    return figure


# Map the legacy dbc colour arg → an on-theme notice tone, so callers keep the `color=` signature.
_PLACEHOLDER_TONE = {"secondary": "muted", "info": "info", "warning": "warning"}


def build_advanced_placeholder(message: str, color: str = "secondary") -> html.Div:
    return html.Div(
        build_notice(message, tone=_PLACEHOLDER_TONE.get(color, "muted")),
        className="p-2",
    )


def build_advanced_controls() -> html.Div:
    """Compact, always-visible controls strip for the Advanced tab.

    Mounted once at the top of the persistent ``advanced-container`` so the count + Run controls show
    even before the first Run. The Run callback fills ``advanced-dashboard-content`` below this strip,
    never replacing it.
    """
    return dbc.Row(
        [
            dbc.Col(
                dmc.Select(
                    id="advanced-expiry-count",
                    label="Advanced Expiries",
                    data=[
                        {"label": "Off", "value": "off"},
                        {"label": "3 Expiries", "value": "3"},
                        {"label": "4 Expiries", "value": "4"},
                        {"label": "5 Expiries", "value": "5"},
                    ],
                    value="off",
                    allowDeselect=False,
                    size="sm",
                    w="160px",
                    comboboxProps={"zIndex": 3000},
                    styles=_SELECT_STYLES,
                ),
                width="auto",
            ),
            dbc.Col(
                [
                    html.Div(" ", className="small mb-0"),
                    dbc.Button("Run Advanced", id="advanced-run-button", color="info", size="sm", n_clicks=0),
                ],
                width="auto",
            ),
        ],
        className="g-2 align-items-end mb-2",
    )


# --------------------------------------------------------------------------- #
# Summary cards (Multi-Expiry Overview + GEX Regime)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Chart generators
# --------------------------------------------------------------------------- #

def _build_iv_surface(analysis: dict) -> go.Figure | None:
    """3D IV surface — dual call/put surfaces, scipy cubic interp."""
    expiry_data = analysis.get("expiry_data", {})
    iv_by_expiry = analysis.get("volatility_surface", {}).get("iv_by_expiry", {})
    underlying = float(analysis.get("analysis_metadata", {}).get("underlying_price", 0) or 0)
    if not expiry_data or not iv_by_expiry:
        return None

    call_strikes, call_days, call_ivs = [], [], []
    put_strikes, put_days, put_ivs = [], [], []
    for expiry, rows in expiry_data.items():
        days = iv_by_expiry.get(expiry, {}).get("days_to_expiry")
        if days is None:
            continue
        for row in rows:
            iv = float(row.get("iv", 0) or 0)
            if iv <= 0:
                continue
            if row.get("option_type") == "CE":
                call_strikes.append(float(row["strike"])); call_days.append(days); call_ivs.append(iv)
            elif row.get("option_type") == "PE":
                put_strikes.append(float(row["strike"])); put_days.append(days); put_ivs.append(iv)

    if len(call_strikes) + len(put_strikes) < 10 or len(call_strikes) < 3 or len(put_strikes) < 3:
        return None

    all_strikes = sorted(set(call_strikes + put_strikes))
    all_days = sorted(set(call_days + put_days))
    if len(all_strikes) < 2 or len(all_days) < 2:
        return None

    strikes_dense = np.linspace(min(all_strikes), max(all_strikes), 50)
    days_dense = np.linspace(min(all_days), max(all_days), 25)
    sd_grid, dd_grid = np.meshgrid(strikes_dense, days_dense)

    def _interp(strikes, days, ivs):
        if len(strikes) < 6:
            return None
        points = np.column_stack([strikes, days])
        return griddata(points, ivs, (sd_grid, dd_grid), method="cubic", fill_value=np.nan)

    call_grid = _interp(call_strikes, call_days, call_ivs)
    put_grid = _interp(put_strikes, put_days, put_ivs)
    if call_grid is None or put_grid is None:
        return None

    call_valid = call_grid[~np.isnan(call_grid)]
    put_valid = put_grid[~np.isnan(put_grid)]
    iv_min = min(np.min(call_valid) if call_valid.size else 0, np.min(put_valid) if put_valid.size else 0)
    iv_max = max(np.max(call_valid) if call_valid.size else 50, np.max(put_valid) if put_valid.size else 50)

    figure = make_subplots(
        rows=1, cols=2,
        specs=[[{"type": "surface"}, {"type": "surface"}]],
        subplot_titles=("Call IV Surface", "Put IV Surface"),
        horizontal_spacing=0.08,
    )
    figure.add_trace(
        go.Surface(x=strikes_dense, y=days_dense, z=call_grid, colorscale="RdYlBu_r", cmin=iv_min, cmax=iv_max,
                   name="Call IV", colorbar={"title": "Call IV (%)", "x": 0.45, "len": 0.8}),
        row=1, col=1,
    )
    figure.add_trace(
        go.Surface(x=strikes_dense, y=days_dense, z=put_grid, colorscale="RdYlBu_r", cmin=iv_min, cmax=iv_max,
                   name="Put IV", colorbar={"title": "Put IV (%)", "x": 1.0, "len": 0.8}),
        row=1, col=2,
    )
    scene = {"xaxis_title": "Strike", "yaxis_title": "Days to Expiry", "zaxis_title": "IV (%)",
             "camera": {"eye": {"x": 1.5, "y": 1.5, "z": 1.2}}, "aspectratio": {"x": 1.5, "y": 1.0, "z": 1.0}}
    figure.update_layout(scene=scene, scene2=dict(scene), template="plotly_dark",
                         paper_bgcolor=_DARK_BG, font={"color": theme.INK}, height=600,
                         margin={"t": 50, "b": 30, "l": 20, "r": 20})
    return figure


def _build_gex_profile(analysis: dict) -> go.Figure | None:
    """GEX profile chart — total GEX line + per-expiry + zero-gamma."""
    chart_data = analysis.get("chart_data", {})
    strikes = chart_data.get("strikes", [])
    total_gex = chart_data.get("main_data", {}).get("total_gex", [])
    if not strikes or not total_gex:
        return None

    spot = float(analysis.get("multi_expiry_summary", {}).get("current_spot", 0) or 0)
    zero_gamma = analysis.get("zero_gamma_analysis", {}).get("zero_gamma_strike")

    figure = go.Figure()
    figure.add_scatter(x=strikes, y=total_gex, mode="lines+markers", name="Total GEX",
                       line={"color": theme.INFO, "width": 3}, marker={"size": 6})
    for i, (expiry, gex_values) in enumerate(chart_data.get("expiry_lines", {}).items()):
        figure.add_scatter(x=strikes, y=gex_values, mode="lines", name=format_expiry_label(expiry),
                           line={"color": _EXPIRY_COLORS[i % len(_EXPIRY_COLORS)], "width": 1, "dash": "dot"})

    arr = np.array(total_gex)
    pos = arr >= 0
    neg = arr < 0
    if pos.any():
        figure.add_scatter(x=np.array(strikes)[pos], y=arr[pos], fill="tozeroy", mode="none",
                           fillcolor="rgba(45,212,160,0.14)", name="Positive GEX (Stabilizing)", hoverinfo="skip")
    if neg.any():
        figure.add_scatter(x=np.array(strikes)[neg], y=arr[neg], fill="tozeroy", mode="none",
                           fillcolor="rgba(255,107,107,0.14)", name="Negative GEX (Destabilizing)", hoverinfo="skip")

    figure.add_hline(y=0, line_dash="dash", line_color="gray")
    if spot > 0:
        figure.add_vline(x=spot, line_dash="solid", line_color=theme.INK, line_width=2,
                         annotation_text=f"Spot: ₹{spot:,.0f}", annotation_position="top")
    if zero_gamma and zero_gamma > 0:
        figure.add_vline(x=zero_gamma, line_dash="dash", line_color=theme.AMBER, line_width=2,
                         annotation_text=f"Zero Gamma: ₹{zero_gamma:,.0f}", annotation_position="top left")

    figure.update_layout(title="Strike-Based Multi-Expiry GEX Analysis", xaxis_title="Strike Price (₹)",
                         yaxis_title="Gamma Exposure", hovermode="x unified",
                         legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1})
    return _theme(figure, height=500)


def _build_gex_term_structure(analysis: dict) -> go.Figure | None:
    """GEX term-structure chart — GEX + GEX/day + rollover."""
    from analytics.advanced_gex import calculate_rollover_pressure

    decay = analysis.get("time_analysis", {}).get("decay_impact", {})
    if not decay:
        return None
    triples = sorted(
        ((d["days_to_expiry"], d["total_gex"], d["gex_per_day"], e) for e, d in decay.items()),
        key=lambda t: t[0],
    )
    days = [t[0] for t in triples]
    gex = [t[1] for t in triples]
    gex_day = [t[2] for t in triples]
    expiries = [t[3] for t in triples]

    figure = go.Figure()
    figure.add_scatter(x=days, y=gex, mode="lines+markers", name="Total GEX",
                       line={"color": theme.INFO, "width": 3}, marker={"size": 8})
    figure.add_scatter(x=days, y=gex_day, mode="lines+markers", name="GEX per Day", yaxis="y2",
                       line={"color": theme.DOWN, "width": 2, "dash": "dash"}, marker={"size": 6})

    rollover = calculate_rollover_pressure(analysis.get("time_analysis", {}))
    pressures = [rollover.get("rollover_pressure_by_expiry", {}).get(e, {}).get("pressure", 0) for e in expiries]
    if any(pressures):
        figure.add_scatter(x=days, y=pressures, mode="lines+markers", name="Rollover Pressure", yaxis="y3",
                           line={"color": theme.AMBER, "width": 2, "dash": "dot"}, marker={"size": 5})
        figure.update_layout(yaxis3={"title": {"text": "Rollover", "font": {"color": theme.AMBER}},
                                     "overlaying": "y", "side": "right", "position": 0.97, "showgrid": False,
                                     "tickfont": {"color": theme.AMBER}})

    figure.update_layout(title="GEX Term Structure", xaxis_title="Days to Expiry", yaxis_title="Total GEX",
                         yaxis2={"title": {"text": "GEX/Day", "font": {"color": theme.DOWN}}, "overlaying": "y",
                                 "side": "right", "showgrid": False, "tickfont": {"color": theme.DOWN}},
                         hovermode="x unified",
                         legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1})
    return _theme(figure, height=450)


def _build_vol_term_structure(analysis: dict) -> go.Figure | None:
    """Volatility term-structure chart — per-side bracketing ATM IV (Call + Put) by DTE."""
    vol = analysis.get("volatility_surface", {})
    term = vol.get("term_structure", [])
    if not term:
        return None
    days = [item["days"] for item in term]
    call_iv = [item.get("atm_call_iv") for item in term]
    put_iv = [item.get("atm_put_iv") for item in term]

    figure = go.Figure()
    figure.add_scatter(x=days, y=call_iv, mode="lines+markers", name="Call ATM IV",
                       line={"color": theme.CALL, "width": 3}, marker={"size": 8})
    figure.add_scatter(x=days, y=put_iv, mode="lines+markers", name="Put ATM IV",
                       line={"color": theme.PUT, "width": 3}, marker={"size": 8})

    figure.update_layout(title="Volatility Term Structure", xaxis_title="Days to Expiry",
                         yaxis_title="Implied Volatility (%)", hovermode="x unified",
                         legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1})
    return _theme(figure, height=450)


def _heatmap_matrices(expiry_data: dict) -> tuple[list[float], list[str], list[str]]:
    all_strikes = sorted({float(r["strike"]) for rows in expiry_data.values() for r in rows})
    sorted_expiries = sorted(expiry_data.keys())
    strike_labels = [format_strike(s) for s in all_strikes]
    return all_strikes, sorted_expiries, strike_labels


def build_migration_heatmap(analysis: dict, mode: str = "combined") -> go.Figure | None:
    """Expiry migration heatmap (combined / split) on oi_display."""
    expiry_data = analysis.get("expiry_data", {})
    if len(expiry_data) < 2:
        return None
    all_strikes, sorted_expiries, strike_labels = _heatmap_matrices(expiry_data)
    expiry_labels = [f"{format_expiry_label(e)}" for e in sorted_expiries]

    def _oi(rows: list[dict], strike: float, option_type: str | None) -> float:
        total = 0.0
        for r in rows:
            if float(r["strike"]) == strike and (option_type is None or r.get("option_type") == option_type):
                total += float(r.get("oi_display", 0) or 0)
        return total

    if mode == "split":
        call_matrix, put_matrix = [], []
        for strike in all_strikes:
            call_matrix.append([_oi(expiry_data[e], strike, "CE") for e in sorted_expiries])
            put_matrix.append([_oi(expiry_data[e], strike, "PE") for e in sorted_expiries])
        figure = make_subplots(rows=1, cols=2, subplot_titles=("Call OI Migration", "Put OI Migration"),
                               shared_yaxes=True, horizontal_spacing=0.1)
        figure.add_trace(go.Heatmap(z=call_matrix, x=expiry_labels, y=strike_labels, colorscale="Reds",
                                    colorbar={"title": "Call OI", "x": 0.46}), row=1, col=1)
        figure.add_trace(go.Heatmap(z=put_matrix, x=expiry_labels, y=strike_labels, colorscale="Blues",
                                    colorbar={"title": "Put OI", "x": 1.0}), row=1, col=2)
        figure.update_layout(title="Call/Put OI Migration Heatmaps")
    else:
        matrix = [[_oi(expiry_data[e], strike, None) for e in sorted_expiries] for strike in all_strikes]
        figure = go.Figure(go.Heatmap(z=matrix, x=expiry_labels, y=strike_labels, colorscale="Viridis",
                                      colorbar={"title": "Total OI (Lots)"}))
        figure.update_layout(title="Combined OI Migration Heatmap", xaxis_title="Expiry", yaxis_title="Strike")
    return _theme(figure, height=500)


# --------------------------------------------------------------------------- #
# Dashboard assembly + bottom-panel summary
# --------------------------------------------------------------------------- #

def _chart_or_alert(figure: go.Figure | None, missing: str, height: int) -> object:
    if figure is None:
        return build_notice(missing, tone="info")
    return dcc.Graph(figure=figure, config={"displayModeBar": False}, style={"height": f"{height}px"})


def build_advanced_dashboard(analysis: dict) -> html.Div:
    """Full advanced dashboard from a stored analysis dict."""
    if not analysis or analysis.get("error"):
        return build_advanced_placeholder(analysis.get("error", "Advanced analysis unavailable.") if analysis else "Advanced analysis unavailable.", color="warning")

    return html.Div(
        [
            dbc.Card(
                [dbc.CardHeader("3D Implied Volatility Surface"),
                 dbc.CardBody(_chart_or_alert(_build_iv_surface(analysis), "3D IV surface needs more strikes/expiries.", 600))],
                className="mb-3",
            ),
            dbc.Card(
                [dbc.CardHeader("Gamma Exposure Profile"),
                 dbc.CardBody(_chart_or_alert(_build_gex_profile(analysis), "GEX profile unavailable.", 500))],
                className="mb-3",
            ),
            dbc.Row(
                [
                    dbc.Col(dbc.Card([dbc.CardHeader("Volatility Term Structure"),
                                      dbc.CardBody(_chart_or_alert(_build_vol_term_structure(analysis), "Term structure unavailable.", 450))]), md=6),
                    dbc.Col(dbc.Card([dbc.CardHeader("GEX Term Structure"),
                                      dbc.CardBody(_chart_or_alert(_build_gex_term_structure(analysis), "Term structure unavailable.", 450))]), md=6),
                ],
                className="g-3 mb-3",
            ),
            dbc.Card(
                [
                    dbc.CardHeader(
                        dbc.Row(
                            [
                                dbc.Col("Expiry Migration Heatmap", md="auto", className="fw-bold"),
                                dbc.Col(
                                    dbc.RadioItems(
                                        id="advanced-heatmap-mode",
                                        options=[{"label": "Combined", "value": "combined"}, {"label": "Split", "value": "split"}],
                                        value="combined",
                                        inline=True,
                                    ),
                                    md="auto",
                                ),
                            ],
                            className="g-2 align-items-center justify-content-between",
                        )
                    ),
                    dbc.CardBody(
                        dcc.Graph(
                            id="advanced-heatmap-graph",
                            figure=build_migration_heatmap(analysis, "combined") or go.Figure(),
                            config={"displayModeBar": False},
                            style={"height": "500px"},
                        )
                    ),
                ],
                className="mb-3",
            ),
        ],
        className="p-2",
    )


def build_advanced_summary_for_store(analysis: dict, *, captured_at: str | None = None) -> dict:
    """Attach the bottom-panel summary keys onto the analysis dict (one store).

    Keys consumed by ``features/bottom_panels/rendering._build_advanced_row``: instrument_key,
    expiry_metrics (per-expiry bracketing ATM call/put IV), gex_regime, zero_gamma_strike,
    distance_to_zero_gamma_pts/_pct, captured_at.
    """
    spot = float(analysis.get("multi_expiry_summary", {}).get("current_spot", 0) or 0)
    expiry_data = analysis.get("expiry_data", {})
    expiry_metrics = []
    for expiry, rows in expiry_data.items():
        # Per-side bracketing ATM IV (the skew-row primitive), not a wing-polluted full-range mean.
        call_rows = [r for r in rows if r.get("option_type") == "CE"]
        put_rows = [r for r in rows if r.get("option_type") == "PE"]
        atm_call = bracketing_atm_iv(call_rows, spot=spot, option_type="CE") if spot > 0 else None
        atm_put = bracketing_atm_iv(put_rows, spot=spot, option_type="PE") if spot > 0 else None
        expiry_metrics.append({
            "expiry_label": format_expiry_label_short(expiry),
            "atm_call_iv": atm_call,
            "atm_put_iv": atm_put,
        })

    regime = analysis.get("market_interpretation", {})
    zero_gamma = analysis.get("zero_gamma_analysis", {})

    enriched = dict(analysis)
    enriched["instrument_key"] = analysis.get("request", {}).get("instrument_key")
    enriched["expiry_metrics"] = expiry_metrics
    # GEX-regime read relocated here from the (removed) Advanced-tab top card.
    enriched["gex_regime"] = regime.get("regime_name")
    enriched["zero_gamma_strike"] = zero_gamma.get("zero_gamma_strike")
    enriched["distance_to_zero_gamma_pts"] = regime.get("distance_to_zero_gamma_pts")
    enriched["distance_to_zero_gamma_pct"] = regime.get("distance_to_zero_gamma_pct")
    enriched["captured_at"] = captured_at
    return enriched
