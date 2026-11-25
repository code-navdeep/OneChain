"""Options Analysis figure builders (per-option metric + aggregate GEX).

Pure presentation: turns the repository's per-contract time-series rows into Plotly
figures. Units are display-correct (oi/volume/oi_change plot the lot-scaled columns
and are labelled "(Lots)" truthfully). `None` metric values (e.g. `bs_price` when the
risk-free rate is unset) render as gaps, never as 0.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import plotly.graph_objects as go

from analytics.chart_windows import rolling_window_range
from ui import theme
from ui.formatters import format_strike

IST = ZoneInfo("Asia/Kolkata")


def _apply_oa_theme(figure: go.Figure) -> go.Figure:
    """Dark "Midnight Quant" theme for the Time-Series (OA) figures."""
    figure.update_layout(
        template="plotly_dark",
        paper_bgcolor=theme.SURFACE,
        plot_bgcolor=theme.SURFACE,
        font={"color": theme.INK, "family": "Geist Mono, monospace"},
    )
    figure.update_xaxes(gridcolor=theme.GRID, zeroline=False)
    figure.update_yaxes(gridcolor=theme.GRID, zeroline=False)
    return figure


_OPTION_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]

# metric column -> (axis title, unit suffix)
_METRIC_DISPLAY = {
    "last_price": ("Last Price", "(₹)"),
    "bs_price": ("Black-Scholes Price", "(₹)"),
    "volume_lots": ("Volume", "(Lots)"),
    "oi_lots": ("Open Interest", "(Lots)"),
    "oi_change_lots": ("OI Change", "(Lots)"),
    "iv": ("Implied Volatility", "(%)"),
    "delta": ("Delta", ""),
    "gamma": ("Gamma", ""),
    "theta": ("Theta", ""),
    "vega": ("Vega", ""),
    "volatility_exposure": ("Volatility Exposure", ""),
    "gamma_exposure": ("Gamma Exposure", ""),
}

_YRANGE_MARGIN = {"tight10": 0.10, "tight25": 0.25, "tight": 0.05, "wide": 0.50}


def _parse_ts(value) -> datetime:
    raw = str(value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def _placeholder(message: str, height: int | None = None, color: str = theme.MUTED) -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(text=message, xref="paper", yref="paper", x=0.5, y=0.5,
                          showarrow=False, font=dict(size=14, color=color))
    # height=None → autosize, so the figure fills its flex slot (the left metric drawer); a
    # fixed height is still honored for the bottom-panel charts that pass one.
    sizing = {"height": height} if height is not None else {"autosize": True}
    figure.update_layout(dragmode="pan", xaxis=dict(visible=False),
                         yaxis=dict(visible=False), margin=dict(l=20, r=20, t=20, b=20), **sizing)
    return _apply_oa_theme(figure)


def _yrange(values: list, selection: str) -> tuple[float | None, float | None]:
    numeric = [float(v) for v in values if v is not None]
    if not numeric or selection == "auto":
        return None, None
    low, high = min(numeric), max(numeric)
    margin = _YRANGE_MARGIN.get(selection, 0.20) * (high - low)
    return low - margin, high + margin


def build_metric_chart(series_rows: list[dict], metric: str, yrange: str) -> go.Figure:
    """One line per selected contract for a single metric. Default view is a rolling 60-min window:
    first point ~3 min from the left edge early-session, then it rolls; the full series stays in the
    trace (pannable). Legend is horizontal at the top (auto-wraps). Y-margin fixed at ±10% (``tight10``)."""
    if not series_rows:
        return _placeholder("Select contracts to compare")

    by_option: dict[str, list[dict]] = defaultdict(list)
    for row in series_rows:
        option_id = f"{format_strike(row['strike'])}_{row['option_type']}"
        by_option[option_id].append(row)

    # Default view: a rolling 60-min window — the first point sits ~3 min from the left edge early in
    # the session, then the newest is kept 6 min off the right edge and the window rolls. The whole
    # series stays in the trace (a default view, not a cap → pannable).
    timestamps = [_parse_ts(row["timestamp"]) for row in series_rows]
    left_edge, right_edge = rolling_window_range(
        first_t=min(timestamps), right_anchor=max(timestamps),
        window_minutes=60, left_pad_minutes=3, right_pad_minutes=6,
    )
    figure = go.Figure()
    all_values: list = []
    for index, (option_id, rows) in enumerate(by_option.items()):
        xs = [_parse_ts(row["timestamp"]) for row in rows]
        ys = [row["value"] for row in rows]
        all_values.extend(ys)
        figure.add_scatter(
            x=xs, y=ys, mode="lines+markers", name=option_id,
            line=dict(color=_OPTION_PALETTE[index % len(_OPTION_PALETTE)], width=2),
            marker=dict(size=4),
        )

    y_min, y_max = _yrange(all_values, yrange)
    figure.update_layout(
        # No axis titles — they ate panel space; the metric dropdown + per-contract legend already
        # identify what's plotted. Legend is horizontal along the top (Plotly auto-wraps it to extra
        # rows when there are many contracts) so it no longer eats the chart's right-side width.
        autosize=True,
        hovermode="x unified",
        dragmode="pan",
        margin=dict(l=40, r=10, t=30, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        xaxis=dict(type="date", tickformat="%H:%M", range=[left_edge, right_edge]),
        yaxis=dict(range=[y_min, y_max] if y_min is not None else None,
                   autorange=y_min is None),
    )
    return _apply_oa_theme(figure)


def build_aggregate_gex_chart(gex_rows: list[dict], display_name: str, expiry: str) -> go.Figure:
    """Total / Call / Put GEX over time — **today's session only**, a rolling 60-min window
    (first point ~3 min from the left edge early-session, then rolling). Auto y-range."""
    if not gex_rows:
        return _placeholder("No GEX data available. Fetch data to populate.", 400)

    xs = [_parse_ts(row["timestamp"]) for row in gex_rows]
    figure = go.Figure()
    figure.add_scatter(x=xs, y=[row["total_gex"] for row in gex_rows], mode="lines+markers",
                       name="Total", line=dict(color=theme.AMBER, width=3), marker=dict(size=5))
    figure.add_scatter(x=xs, y=[row["call_gex"] for row in gex_rows], mode="lines+markers",
                       name="Call", line=dict(color=theme.CALL, width=2.5), marker=dict(size=4))
    figure.add_scatter(x=xs, y=[row["put_gex"] for row in gex_rows], mode="lines+markers",
                       name="Put", line=dict(color=theme.PUT, width=2.5), marker=dict(size=4))
    figure.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)

    left, right = rolling_window_range(
        first_t=min(xs), right_anchor=max(xs),
        window_minutes=60, left_pad_minutes=3, right_pad_minutes=6,
    )
    # Title top-left, no legend (it just obscured the chart — the "x unified" hover already names
    # Total/Call/Put with their colour swatches). Replaces the removed card header.
    title_text = f"{display_name} | Aggregate GEX".strip(" |")
    figure.update_layout(
        title=dict(text=title_text, x=0.0, xanchor="left"),
        xaxis_title="Time",
        yaxis_title="Gamma Exposure",
        height=400,
        hovermode="x unified",
        dragmode="pan",
        margin=dict(l=40, r=20, t=40, b=30),
        showlegend=False,
        xaxis=dict(type="date", tickformat="%H:%M", range=[left, right]),
        yaxis=dict(autorange=True, tickformat=",d"),
    )
    return _apply_oa_theme(figure)


def build_risk_reversal_chart(series_rows: list[dict], display_name: str, expiry: str) -> go.Figure:
    """25Δ risk reversal over time as a single line with a zero baseline — **today's session only**,
    a rolling 60-min window (first point ~3 min from the left edge early-session, then rolling).

    Negative = put skew, positive = call skew; the zero line makes the crossover visible.
    `risk_reversal_25` is None on fetches where a 25Δ wing was unreachable — those points render as
    gaps, never as 0. Auto y-range."""
    if not series_rows:
        return _placeholder("No risk-reversal history yet. Fetch data to populate.", 400)

    xs = [_parse_ts(row["timestamp"]) for row in series_rows]
    rr_values = [row.get("risk_reversal_25") for row in series_rows]
    figure = go.Figure()
    figure.add_scatter(x=xs, y=rr_values, mode="lines+markers", name="RR 25Δ",
                       line=dict(color=theme.AMBER, width=3), marker=dict(size=5), connectgaps=False)
    figure.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)

    left, right = rolling_window_range(
        first_t=min(xs), right_anchor=max(xs),
        window_minutes=60, left_pad_minutes=3, right_pad_minutes=6,
    )
    # Title top-left + legend top-right (mirrors the bottom-panel IV-vs-RV chart) — replaces the
    # removed card header.
    title_text = f"{display_name} | 25Δ Risk Reversal".strip(" |")
    figure.update_layout(
        title=dict(text=title_text, x=0.0, xanchor="left"),
        xaxis_title="Time",
        yaxis_title="RR 25Δ (IV pts)",
        height=400,
        hovermode="x unified",
        dragmode="pan",
        margin=dict(l=40, r=20, t=40, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis=dict(type="date", tickformat="%H:%M", range=[left, right]),
        yaxis=dict(autorange=True),
    )
    return _apply_oa_theme(figure)
