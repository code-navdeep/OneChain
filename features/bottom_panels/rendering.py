"""Bottom analytics panel rendering.

``build_bottom_panels`` renders only the **metric rows** of the fixed-position bottom panel
(summary, skew/pinning, optional advanced, vol-analytics) into the dynamic
``bottom-panels-container``. The wrapper, toggle, window selector — and now the **four charts**
(Price | IV-vs-RV and Aggregate-GEX | Risk-Reversal) — are **static** in ``ui/shell.py``; their
figures are swapped by callbacks so the chart controls survive each tick. ``build_price_figure`` /
``build_iv_rv_figure`` (here) + ``resolve_panel_context`` are the pieces those callbacks reuse.

Honest rendering: every metric that is not cleanly computable shows ``"—"`` via
``_fmt`` — there are no ``or 0`` coercions of metric values in this file.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dash import html
import plotly.graph_objects as go

from analytics.aggregate import build_aggregate_report, is_aggregate_eligible
from analytics.chart_windows import rolling_window_range
from analytics.price_chart import aggregate_ohlc
from analytics.report_builder import risk_reversal_label
from services.view_data_service import ViewDataService
from state.models import ActiveViewContext, LoadedReportState
from state.selectors import get_active_loaded_pair
from ui import theme
from ui.formatters import format_strike
from ui.notices import build_notice


_DARK_PLOT_BG = theme.SURFACE


def _fmt(value) -> str:
    """Format a numeric metric — ``None`` becomes the em-dash placeholder."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_signal(value) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _fmt_number(value, decimals: int = 0) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_strike(value) -> str:
    """A strike tile — thousands-grouped, a half strike keeps its decimal (``402.5``, never ``402``);
    ``None`` becomes the em-dash placeholder."""
    if value is None:
        return "—"
    return format_strike(value, grouped=True)


def _accent(value) -> str | None:
    """Colour accent for a regime / skew value — teal (stabilizing / call skew),
    coral (destabilizing / put skew), else None. Lets the key reads pop without colouring
    every tile. (Tokens track the live values: GEX regimes are Stabilizing/Destabilizing
    since the GEX-sign pass; the IV-RV over/under signal was removed.)"""
    if value is None:
        return None
    text = str(value).upper()
    # DOWN first: "STABILIZING" is a substring of "DESTABILIZING", so the destabilizing
    # check must run before the stabilizing one.
    if any(token in text for token in ("DESTABILIZING", "PUT SKEW")):
        return theme.DOWN
    if any(token in text for token in ("STABILIZING", "CALL SKEW")):
        return theme.UP
    return None


def _metric(label: str, value_str: str, *, accent: str | None = None) -> html.Div:
    """A compact Stat tile — uppercase muted label over a larger mono value (optionally accented)."""
    return html.Div(
        [
            html.Div(label, className="oc-stat-label"),
            html.Div(value_str, className="oc-stat-value", style={"color": accent} if accent else None),
        ],
        className="oc-stat-tile",
    )


def _metric_row(children: list, title: str | None = None) -> html.Div:
    blocks = []
    if title is not None:
        blocks.append(html.Div(title, className="oc-panel-section-title"))
    blocks.append(
        html.Div(children, style={"display": "flex", "flexWrap": "wrap", "alignItems": "flex-start"})
    )
    return html.Div(blocks, className="mb-2 pb-2", style={"borderBottom": "1px solid rgba(255,255,255,0.08)"})


def _resolve_context_end_timestamp(
    loaded_report_state: LoadedReportState,
    active_pair,
    aggregate_report: dict | None,
) -> str | None:
    if loaded_report_state.source == "live":
        return None
    if aggregate_report is not None:
        candidates = [
            str(item.report.get("capture_timestamp") or item.report.get("saved_timestamp") or "")
            for item in loaded_report_state.loaded_pairs
        ]
        candidates = [item for item in candidates if item]
        return max(candidates) if candidates else None
    value = str(active_pair.report.get("capture_timestamp") or active_pair.report.get("saved_timestamp") or "")
    return value or None


def _resolve_context_end_date(context_end_timestamp: str | None) -> str | None:
    if not context_end_timestamp:
        return None
    return context_end_timestamp[:10]


def resolve_panel_context(
    loaded_state: LoadedReportState | None,
    active_pair_key: str | None,
    active_view_context: ActiveViewContext | None,
) -> dict | None:
    """Resolve the active pair (or the on-the-fly aggregate) + its capture-time bounds.

    Shared by ``build_bottom_panels`` (metric rows) and the static price/IV-RV/GEX/RR chart
    callbacks so all of them scope to exactly the same pair — live or playback, aggregate falls
    back to the first expiry, identical to the old in-panel flow. Returns ``None`` when nothing
    is loaded.
    """
    if loaded_state is None or not loaded_state.loaded_pairs:
        return None
    active_pair = get_active_loaded_pair(loaded_state, active_pair_key)
    if active_pair is None:
        return None
    aggregate = (
        active_view_context is not None
        and active_view_context.kind == "aggregate"
        and is_aggregate_eligible(loaded_state)
    )
    aggregate_report = build_aggregate_report(loaded_state.loaded_pairs) if aggregate else None
    request = loaded_state.loaded_pairs[0].request if aggregate_report is not None else active_pair.request
    report = dict(aggregate_report or active_pair.report)
    context_end_timestamp = _resolve_context_end_timestamp(loaded_state, active_pair, aggregate_report)
    return {
        "request": request,
        "report": report,
        "aggregate": aggregate_report is not None,
        "context_end_timestamp": context_end_timestamp,
        "context_end_date": _resolve_context_end_date(context_end_timestamp),
    }


def _build_summary_row(report: dict) -> html.Div:
    """The analytics summary — a single flat, unlabeled row of metrics: the two
    regimes followed by the greeks/vega figures. No group headings, no section title.

    The GEX/VEX *totals* are intentionally not shown here (they duplicated the chain
    and used a different number format); the regimes carry the GEX/VEX read. Values
    come from `gex_analysis` / `vex_analysis` / `greeks_summary`; anything not
    computable (e.g. greeks/regimes in aggregate mode) renders "—".
    """
    gex = dict(report.get("gex_analysis", {}))
    vex = dict(report.get("vex_analysis", {}))
    greeks = dict(report.get("greeks_summary", {}))
    regime = gex.get("market_regime")
    metrics = [
        _metric("GEX Regime", _fmt_signal(regime), accent=_accent(regime)),
        _metric("Vol Regime", _fmt_signal(vex.get("vol_regime"))),
        _metric("Avg Call Delta", _fmt(greeks.get("avg_call_delta"))),
        _metric("Avg Put Delta", _fmt(greeks.get("avg_put_delta"))),
        _metric("Delta Skew", _fmt(greeks.get("delta_skew"))),
        _metric("Max Vega Strike", _fmt_strike(greeks.get("max_vega_strike"))),
        _metric("Avg Theta", _fmt(greeks.get("avg_theta"))),
        _metric("Vega Skew", _fmt(greeks.get("vega_skew"))),
    ]
    return _metric_row(metrics, title="Positioning · Greeks")


def _build_skew_row(report: dict) -> html.Div:
    """Skew (25Δ risk reversal) as its own stacked row: RR₂₅ + both 25Δ leg IVs + ATM IV +
    a factual skew tag. Aggregate mode has no `skew`, so fields render "—"."""
    skew = dict(report.get("skew") or {})
    rr = skew.get("rr_25")
    return _metric_row(
        [
            _metric("Risk Reversal 25Δ", _fmt(rr)),
            _metric("Call 25Δ IV", _fmt(skew.get("call_iv_25d"))),
            _metric("Put 25Δ IV", _fmt(skew.get("put_iv_25d"))),
            _metric("ATM Call IV", _fmt(skew.get("atm_call_iv"))),
            _metric("ATM Put IV", _fmt(skew.get("atm_put_iv"))),
            _metric("ATM Gap", _fmt(skew.get("atm_gap"))),
            _metric("Skew", _fmt_signal(skew_label := risk_reversal_label(rr)), accent=_accent(skew_label)),
        ],
        title="Skew — 25Δ Risk Reversal",
    )


def _build_pinning_row(report: dict) -> html.Div:
    """Pinning pressure as its own stacked row: the gamma-magnet strike, its strength, and
    spot's distance. Aggregate mode has no `pinning`, so fields render "—"."""
    pinning = dict(report.get("pinning") or {})
    distance_pts = pinning.get("distance_pts")
    distance_pct = pinning.get("distance_pct")
    distance_text = (
        f"{_fmt_number(distance_pts)} ({_fmt(distance_pct)}%)"
        if distance_pts is not None
        else "—"
    )
    return _metric_row(
        [
            _metric("Pin Strike", _fmt_strike(pinning.get("pin_strike"))),
            _metric("Pin Strength", _fmt(pinning.get("pin_strength"))),
            _metric("Distance", distance_text),
        ],
        title="Pinning Pressure",
    )


def _build_advanced_row(advanced_report_data: dict, instrument_key: str) -> html.Div | None:
    if advanced_report_data.get("instrument_key") != instrument_key:
        return None

    zg = advanced_report_data.get("zero_gamma_strike")
    zg_text = f"₹{float(zg):,.0f}" if zg else "—"
    dist_pts = advanced_report_data.get("distance_to_zero_gamma_pts")
    dist_pct = advanced_report_data.get("distance_to_zero_gamma_pct")
    dist_text = f"{_fmt_number(dist_pts)} ({_fmt(dist_pct)}%)" if dist_pts is not None else "—"

    children = [
        _metric("Multi-Expiry GEX Regime", _fmt_signal(adv_regime := advanced_report_data.get("gex_regime")), accent=_accent(adv_regime)),
        _metric("Zero Gamma", zg_text),
        _metric("Distance to Zero Gamma", dist_text),
    ]
    # Per-expiry bracketing ATM IV (Call / Put) as on-theme stat tiles, one per expiry.
    for row in advanced_report_data.get("expiry_metrics", []):
        children.append(
            _metric(
                row.get("expiry_label", row.get("expiry", "")),
                f"CE {_fmt(row.get('atm_call_iv'))}  PE {_fmt(row.get('atm_put_iv'))}",
            )
        )

    # As-of marker — the Advanced block is manual/ephemeral while the rest of the panel is live.
    captured_at = advanced_report_data.get("captured_at")
    title = "Advanced Report"
    if captured_at:
        try:
            title = f"Advanced Report — {datetime.fromisoformat(captured_at).strftime('%H:%M')} IST"
        except (TypeError, ValueError):
            pass

    return _metric_row(children, title=title)


def _build_vol_metrics_row(latest_history: dict | None, vol_window: int) -> html.Div:
    w = vol_window
    if latest_history is None:
        get = lambda _key: None  # noqa: E731
        rv = call_spread = put_spread = None
        latest_date = None
    else:
        get = latest_history.get
        rv = get(f"rv_{w}d")
        call_spread = get(f"call_iv_rv_spread_{w}d")
        put_spread = get(f"put_iv_rv_spread_{w}d")
        latest_date = get("date")

    call_iv = rv + call_spread if (rv is not None and call_spread is not None) else None
    put_iv = rv + put_spread if (rv is not None and put_spread is not None) else None

    return _metric_row(
        [
            _metric("RV", _fmt(rv)),
            _metric("Call IV", _fmt(call_iv)),
            _metric("Put IV", _fmt(put_iv)),
            _metric("Call IV Pct", _fmt(get(f"call_iv_percentile_{w}d"))),
            _metric("Put IV Pct", _fmt(get(f"put_iv_percentile_{w}d"))),
            _metric("IV Skew", _fmt(get(f"iv_skew_{w}d"))),
            _metric("Call IV-RV", _fmt(call_spread)),
            _metric("Put IV-RV", _fmt(put_spread)),
        ],
        title=f"Vol Analytics — {w}D Window" + (f" (as of {latest_date})" if latest_date else ""),
    )


def build_price_figure(
    price_series: list[dict],
    display_name: str,
    *,
    call_pain_level: float | None = None,
    put_pain_level: float | None = None,
) -> go.Figure:
    """Single-session 5-min candlestick as a pure ``go.Figure`` (statically mounted; only the
    figure is swapped).

    The **whole session** of candles is in the trace, so panning reveals every candle — the x-range
    below is only the *default view*, never a cap. That view is a fixed **3-hour** window whose
    right edge is ``max(first + 1.5h, newest + 15min)``: early in the session the first candle sits at
    the **centre** and candles fill rightward; once the newest reaches the anchor it pins **15 min
    (3 candles) off the right edge** and the window **rolls**, older candles sliding left of the view
    (still pannable). The window is always 3h wide → **fixed candle width**. The y-range is driven by
    the **candle band** (lowest low → highest high over the visible candles, in totality) with **15%
    padding** each side; a pain line is pulled into the range only while it keeps the total band ≤ **3×
    the candle span** (so the candle band always holds ≥ ⅓ of the height) — a wall farther than that
    **vanishes** off-screen rather than crushing the candles. Hover shows **2 decimals, no SI suffix**.
    ``call_pain_level`` / ``put_pain_level`` draw horizontal reference lines at the pain point of each
    side's highest-OI strike (call=red, put=green); the lines are always drawn, the cap only governs
    whether the y-range reaches them.
    Zero candles → a dark "no data" note (a ``.figure`` output can't carry a dbc.Alert)."""
    ohlc_rows = aggregate_ohlc(price_series, timeframe_minutes=5)
    if not ohlc_rows:
        empty = go.Figure()
        empty.update_layout(
            template="plotly_dark",
            paper_bgcolor=_DARK_PLOT_BG,
            plot_bgcolor=_DARK_PLOT_BG,
            height=350,
            dragmode="pan",
            margin={"l": 40, "r": 20, "t": 40, "b": 30},
            xaxis={"visible": False},
            yaxis={"visible": False},
            annotations=[{
                "text": "No price history available.",
                "showarrow": False,
                "font": {"color": theme.MUTED, "size": 13},
            }],
        )
        return empty
    figure = go.Figure(
        go.Candlestick(
            x=[row["timestamp"] for row in ohlc_rows],
            open=[row["open"] for row in ohlc_rows],
            high=[row["high"] for row in ohlc_rows],
            low=[row["low"] for row in ohlc_rows],
            close=[row["close"] for row in ohlc_rows],
            increasing_line_color=theme.UP,
            decreasing_line_color=theme.DOWN,
        )
    )
    # Default 3-hour view: centre the first candle, fill right, then pin 15 min off the right edge
    # and roll. No rangebreaks — a single continuous session has no gap to hide, and the pre-open
    # space on the left is exactly what centres the first candle.
    first_t = datetime.fromisoformat(ohlc_rows[0]["timestamp"])
    newest_t = datetime.fromisoformat(ohlc_rows[-1]["timestamp"])
    # 3h window, first candle centred early-session (left_pad = window/2 = 90min) → max(first+1.5h, newest+15min).
    left_edge, right_edge = rolling_window_range(
        first_t=first_t, right_anchor=newest_t,
        window_minutes=180, left_pad_minutes=90, right_pad_minutes=15,
    )

    # Pain-point reference lines: call side's highest-OI strike (red) / put side's (green). Drawn at
    # an underlying-price level, so they sit on this chart's y-axis.
    pain_levels = [lvl for lvl in (call_pain_level, put_pain_level) if lvl not in (None, 0)]
    if call_pain_level not in (None, 0):
        figure.add_hline(y=float(call_pain_level), line_width=1.5, line_dash="dot",
                         line_color=theme.DOWN)
    if put_pain_level not in (None, 0):
        figure.add_hline(y=float(put_pain_level), line_width=1.5, line_dash="dot",
                         line_color=theme.UP)

    # Y-range driven by the candle band, with the pain lines pulled in only while they keep the total
    # band ≤ 3× the candle span (candles ≥ ⅓ of the height). A wall too far for that ceiling vanishes
    # off-screen instead of crushing the candles. 15% headroom each side. When there's no candle span
    # to protect (single / flat session), the cap is a no-op and the lines are included as before.
    visible = [r for r in ohlc_rows if left_edge <= datetime.fromisoformat(r["timestamp"]) <= right_edge] or ohlc_rows
    c_hi = max(r["high"] for r in visible)
    c_lo = min(r["low"] for r in visible)
    span = c_hi - c_lo
    if span <= 0:
        hi = max([c_hi] + pain_levels)
        lo = min([c_lo] + pain_levels)
    else:
        ceiling = 3 * span
        lo, hi = c_lo, c_hi
        # Nearest wall first, so a close wall isn't crowded out by a far one when both can't fit.
        for level in sorted(pain_levels, key=lambda v: max(0.0, c_lo - v, v - c_hi)):
            new_lo, new_hi = min(lo, level), max(hi, level)
            if (new_hi - new_lo) <= ceiling:
                lo, hi = new_lo, new_hi
            # else: wall is beyond the ⅓ ceiling → left out of the range (drawn but off-screen).
    pad = (hi - lo) * 0.15 if hi > lo else (abs(hi) * 0.15 or 1.0)

    figure.update_layout(
        title=f"{display_name} | Price",
        template="plotly_dark",
        font={"color": theme.INK, "family": "Geist Mono, monospace"},
        plot_bgcolor=_DARK_PLOT_BG,
        paper_bgcolor=_DARK_PLOT_BG,
        xaxis_rangeslider_visible=False,
        dragmode="pan",
        # Unified hover → the vertical cursor spikeline that tracks the mouse, for consistency with
        # the GEX / RR / metric charts (they all use it). Gives the crosshair + an OHLC tooltip.
        hovermode="x unified",
        height=350,
        margin={"l": 40, "r": 20, "t": 40, "b": 30},
        yaxis={"range": [lo - pad, hi + pad], "hoverformat": ".2f"},
        xaxis={"range": [left_edge.isoformat(), right_edge.isoformat()]},
    )
    return figure


def build_iv_rv_figure(
    instrument_history: list[dict],
    vol_window: int,
    display_name: str,
    *,
    aggregate: bool,
) -> go.Figure:
    w = vol_window
    ascending = list(reversed(instrument_history))  # history is ORDER BY date DESC
    dates: list[str] = []
    rv_values: list[float] = []
    call_iv_values: list[float | None] = []
    put_iv_values: list[float | None] = []
    for row in ascending:
        rv = row.get(f"rv_{w}d")
        if rv is None:
            # No valid measure at this timescale on this date (history not yet deep
            # enough for the window) — skip the date entirely.
            continue
        dates.append(row.get("date"))
        rv_values.append(rv)
        call_spread = row.get(f"call_iv_rv_spread_{w}d")
        put_spread = row.get(f"put_iv_rv_spread_{w}d")
        call_iv_values.append(rv + call_spread if call_spread is not None else None)
        put_iv_values.append(rv + put_spread if put_spread is not None else None)

    if not dates:
        # No plottable history yet — return a dark "no data" placeholder so this chart matches its
        # neighbours (price / GEX / RR all show a centered note when empty) instead of an empty frame.
        empty = go.Figure()
        empty.update_layout(
            template="plotly_dark",
            paper_bgcolor=_DARK_PLOT_BG,
            plot_bgcolor=_DARK_PLOT_BG,
            height=350,
            dragmode="pan",
            margin={"l": 40, "r": 20, "t": 40, "b": 30},
            xaxis={"visible": False},
            yaxis={"visible": False},
            annotations=[{
                "text": "No IV/RV data available. Fetch data to populate.",
                "showarrow": False,
                "font": {"color": theme.MUTED, "size": 13},
            }],
        )
        return empty

    figure = go.Figure()
    if not aggregate:
        figure.add_scatter(x=dates, y=call_iv_values, mode="lines+markers", name="Call IV", line={"color": theme.CALL, "width": 2})
        figure.add_scatter(x=dates, y=put_iv_values, mode="lines+markers", name="Put IV", line={"color": theme.PUT, "width": 2})
    figure.add_scatter(x=dates, y=rv_values, mode="lines+markers", name="RV", line={"color": theme.INK, "width": 2, "dash": "dash"})
    figure.update_layout(
        title=f"{display_name} | IV vs RV ({w}D)",
        template="plotly_dark",
        font={"color": theme.INK, "family": "Geist Mono, monospace"},
        plot_bgcolor=_DARK_PLOT_BG,
        paper_bgcolor=_DARK_PLOT_BG,
        xaxis_title="Date",
        yaxis_title="Volatility (%)",
        # Unified hover → the vertical cursor spikeline that tracks the mouse, consistent with the
        # Price / GEX / RR / metric charts (they all use it). Was the one panel chart without it.
        hovermode="x unified",
        dragmode="pan",
        height=350,
        margin={"l": 40, "r": 20, "t": 40, "b": 30},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},
    )
    if aggregate:
        # Aggregate IV across expiries has no meaning, so Call/Put IV are suppressed above; note it
        # in-figure (the old dbc.Alert can't live inside a `.figure` output). Bottom-center inside the
        # plot — clear of the top-left title, the top-right legend, and the Plotly modebar (the old
        # top-left y=1.12 spot was overlapped/clipped by the title + tools).
        figure.add_annotation(
            text="Aggregate mode — IV suppressed; RV is instrument-level.",
            xref="paper", yref="paper", x=0.5, y=0.04, xanchor="center", yanchor="bottom",
            showarrow=False, font={"color": theme.MUTED, "size": 10},
        )
    return figure


def build_bottom_panels(
    loaded_report_state: LoadedReportState | None,
    active_pair_key: str | None,
    view_data_service: ViewDataService,
    active_view_context: ActiveViewContext | None = None,
    vol_window: int = 22,
    advanced_report_data: dict | None = None,
) -> html.Div:
    """Render the bottom-panel **metric rows** (summary, skew/pinning, optional advanced, vol).

    The four charts are static in the shell and refreshed by their own figure callbacks — this
    builds only the text/stat rows that live in the dynamic ``bottom-panels-container``.
    """
    context = resolve_panel_context(loaded_report_state, active_pair_key, active_view_context)
    if context is None:
        return build_notice("Fetch data to view analytics.")

    request = context["request"]
    report = context["report"]

    # Full history up to the loaded context's date — the window selector picks which
    # column-family to read, NOT how many rows to fetch.
    instrument_history = view_data_service.get_instrument_historical_metrics(
        asset_id=request.asset_id,
        segment_code=request.segment_code,
        end_date=context["context_end_date"],
    )
    latest_history = instrument_history[0] if instrument_history else None

    rows: list = [
        _build_summary_row(report),
        _build_skew_row(report),
        _build_pinning_row(report),
    ]
    if advanced_report_data:
        advanced_row = _build_advanced_row(advanced_report_data, request.instrument_key.value)
        if advanced_row is not None:
            rows.append(advanced_row)
    rows.append(_build_vol_metrics_row(latest_history, vol_window))

    return html.Div(rows)
