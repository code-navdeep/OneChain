"""Drawer shell + sparkline figures for the required-move feature.

``build_drawer_shell`` is the **static** right-side ``dbc.Offcanvas`` + the always-visible
arrow toggle; it is mounted once by ``ui/shell.py`` (the selection store lives there too).
``build_drawer_body`` fills the drawer with up to two stacked required-move sparklines, one
per pinned contract. Figures are built from the pure ``analytics.required_move`` payloads.
"""

from __future__ import annotations

from typing import Any

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dcc, html

from ui import theme

_LTP_COLOR = theme.INFO   # blue — distinct from the green put-wall / teal palette (LTP must stand out)
_REVERSAL_COLOR = theme.AMBER
_CALL_PAIN_COLOR = theme.DOWN   # red — reversal at the highest-OI call strike's pain point
_PUT_PAIN_COLOR = theme.UP      # green — reversal at the highest-OI put strike's pain point


def build_sparkline_figure(curve: dict[str, Any], *, title: str) -> go.Figure:
    """A compact Option Reversal sparkline: the option's actual LTP (today, open→now) + up to five
    pain-point reversal lines (self/amber-dash, highest-OI call-wall/red-dash + put-wall/green-dash,
    and the 2nd-highest-OI call/put walls in the same side colour but DOTTED; today, open→close), all
    as option-price-vs-time lines on a today-session IST clock axis, default-viewed through a rolling
    60-minute window (`x_range`)."""
    fig = go.Figure()

    rev_x = curve.get("reversal_x")
    if curve.get("ok") and (curve.get("reversal_y") or curve.get("ltp_y")):
        if curve.get("ltp_x"):
            fig.add_trace(
                go.Scatter(
                    x=curve["ltp_x"], y=curve["ltp_y"], mode="lines", connectgaps=False,
                    line=dict(color=_LTP_COLOR, width=2), name="LTP",
                )
            )
        # Self reversal (amber) — drawn unless it coincides with a wall line (draw_self=False).
        # Legend names carry the actual strikes (e.g. "22500 CE (Self)", "22800 CE", "22300 PE"),
        # attached to the curve by the drawer callback; they fall back to the generic labels.
        if rev_x and curve.get("draw_self", True) and curve.get("reversal_y"):
            fig.add_trace(
                go.Scatter(
                    x=rev_x, y=curve["reversal_y"], mode="lines", connectgaps=False,
                    line=dict(color=_REVERSAL_COLOR, width=2, dash="dash"),
                    name=curve.get("self_label", "Self"),
                )
            )
        # Wall reversal lines: same contract, spot held at the highest-OI call / put pain points.
        if rev_x and curve.get("call_reversal_y"):
            fig.add_trace(
                go.Scatter(
                    x=rev_x, y=curve["call_reversal_y"], mode="lines", connectgaps=False,
                    line=dict(color=_CALL_PAIN_COLOR, width=2, dash="dash"),
                    name=curve.get("call_label", "Call OI"),
                )
            )
        if rev_x and curve.get("put_reversal_y"):
            fig.add_trace(
                go.Scatter(
                    x=rev_x, y=curve["put_reversal_y"], mode="lines", connectgaps=False,
                    line=dict(color=_PUT_PAIN_COLOR, width=2, dash="dash"),
                    name=curve.get("put_label", "Put OI"),
                )
            )
        # 2nd-highest-OI walls: same side colour, DOTTED (vs the primary walls' dash) so the weaker
        # rank reads as a lighter version of the same-side wall. Present only when the panel qualified
        # the 2nd strike past its 75% OI ratio (else the series is [] and no trace is added).
        if rev_x and curve.get("call_2nd_reversal_y"):
            fig.add_trace(
                go.Scatter(
                    x=rev_x, y=curve["call_2nd_reversal_y"], mode="lines", connectgaps=False,
                    line=dict(color=_CALL_PAIN_COLOR, width=2, dash="dot"),
                    name=curve.get("call_2nd_label", "Call OI 2nd"),
                )
            )
        if rev_x and curve.get("put_2nd_reversal_y"):
            fig.add_trace(
                go.Scatter(
                    x=rev_x, y=curve["put_2nd_reversal_y"], mode="lines", connectgaps=False,
                    line=dict(color=_PUT_PAIN_COLOR, width=2, dash="dot"),
                    name=curve.get("put_2nd_label", "Put OI 2nd"),
                )
            )
    else:
        fig.add_annotation(text="not computable", showarrow=False, font=dict(color=theme.MUTED, size=11))

    heading = title

    # Today-session datetime axis (single continuous session → no rangebreaks). The default view is
    # the rolling 60-minute window from the payload; the full open→close series stays pannable.
    xaxis: dict[str, Any] = dict(type="date", gridcolor=theme.GRID, tickfont=dict(size=8), title=None)
    if curve.get("tickformat"):
        xaxis["tickformat"] = curve["tickformat"]
    if curve.get("x_range"):
        xaxis["range"] = curve["x_range"]

    # Y-range anchored on the LTP (price) band so the price line always keeps ≥ ⅕ of the height —
    # the same cap idiom as build_price_figure's ⅓ candle-band cap (features/bottom_panels/rendering.py),
    # but ⅕ here (the price line's intraday range is small, so ⅓ handed it too much height and squeezed
    # the reversal lines). The reversal lines are pulled in nearest-first only while the total band stays
    # ≤ 5× the LTP span; a reversal excursion beyond that is drawn but runs off-screen (pannable) instead
    # of flattening the price. 10% headroom each side. When there's no LTP band to protect (no/flat price
    # yet), it spans every drawn series, as before. (The candle→line difference doesn't matter — this
    # only reads y-extents: the LTP series' min/max is the "band"; the reversal values are the candidates.)
    yaxis: dict[str, Any] = dict(gridcolor=theme.GRID, tickfont=dict(size=8), title=None, hoverformat=".2f")
    ltp_vals = [v for v in (curve.get("ltp_y") or []) if v is not None]
    rev_vals = [
        v
        for v in (
            (curve.get("reversal_y") or [])
            + (curve.get("call_reversal_y") or [])
            + (curve.get("put_reversal_y") or [])
            + (curve.get("call_2nd_reversal_y") or [])
            + (curve.get("put_2nd_reversal_y") or [])
        )
        if v is not None
    ]
    p_lo = min(ltp_vals) if ltp_vals else None
    p_hi = max(ltp_vals) if ltp_vals else None
    span = (p_hi - p_lo) if (p_lo is not None and p_hi is not None) else 0.0
    lo = hi = None
    if span > 0:
        ceiling = 5 * span
        lo, hi = p_lo, p_hi
        # Nearest reversal value first, so a close line isn't crowded out by a far one when both can't fit.
        for level in sorted(rev_vals, key=lambda v: max(0.0, p_lo - v, v - p_hi)):
            new_lo, new_hi = min(lo, level), max(hi, level)
            if (new_hi - new_lo) <= ceiling:
                lo, hi = new_lo, new_hi
            # else: this reversal value is beyond the ⅓ ceiling → left off the range (drawn off-screen).
    else:
        # No LTP band to protect (no/flat price yet) → span every drawn series, as before.
        all_vals = ltp_vals + rev_vals
        if all_vals:
            lo, hi = min(all_vals), max(all_vals)
    if lo is not None and hi is not None:
        pad = (hi - lo) * 0.10 if hi > lo else (abs(hi) * 0.10 or 1.0)
        yaxis["range"] = [lo - pad, hi + pad]

    fig.update_layout(
        title=dict(text=heading, x=0.0, font=dict(size=10, color=theme.INK)),
        template="plotly_dark", paper_bgcolor=theme.SURFACE, plot_bgcolor=theme.SURFACE,
        # No fixed height — the graph is responsive and fills its flex slot (build_drawer_body),
        # so the 4 charts share the full drawer height.
        margin=dict(l=34, r=8, t=32, b=30), autosize=True,
        showlegend=True,
        legend=dict(orientation="h", x=1, xanchor="right", y=1.04, font=dict(size=8), bgcolor="rgba(0,0,0,0)"),
        # Unified hover → the moving vertical cursor line + a tooltip listing every line's value at
        # that time (LTP + the reversal lines). Without it the thin lines in this tiny chart had no
        # spikeline and were near-impossible to hover. Mirrors the GEX/RR/metric charts.
        hovermode="x unified",
        # Show the full trace name in the unified tooltip. Plotly's default hoverlabel.namelength is
        # 15, which clips the strike-carrying labels ("22500 CE (Self)" → "(Sel…"); -1 = no truncation.
        hoverlabel=dict(namelength=-1),
        dragmode="pan",
        xaxis=xaxis,
        yaxis=yaxis,
    )
    return fig


def build_drawer_body(entries: list[tuple[str, dict[str, Any]]]) -> html.Div:
    """Stacked sparklines on a fixed 2-slot grid that shares the drawer's full height; a muted
    prompt when nothing is pinned.

    The grid is always 2 slots (50% each): the first `len(entries)` slots hold the sparklines and a
    remaining slot (when only one is pinned) is an empty reserved placeholder (a faint dashed
    outline) — so a chart's height is always a clean half, never 100%. A flex column (height 100% of
    the offcanvas body) gives each slot an equal share; the responsive figures fill their slot. The
    pin cap is 2 (`MAX_PINNED`), so a third pin evicts the oldest — the view never grows past two."""
    if not entries:
        return html.Div(
            "Select an option's price in the chain to pin its reversal curve.",
            style={"color": theme.MUTED, "fontSize": "13px", "padding": "12px"},
        )
    slot_count = 2
    slots: list[html.Div] = []
    for title, curve in entries:
        slots.append(
            dcc.Graph(
                figure=build_sparkline_figure(curve, title=title),
                config={"displayModeBar": False, "responsive": True},
                style={"flex": "1 1 0", "minHeight": 0, "width": "100%"},
            )
        )
    for _ in range(slot_count - len(entries)):
        slots.append(
            html.Div(
                style={
                    "flex": "1 1 0", "minHeight": 0, "width": "100%",
                    "border": f"1px dashed {theme.BORDER}", "borderRadius": "6px",
                }
            )
        )
    return html.Div(
        slots,
        style={"display": "flex", "flexDirection": "column", "height": "100%", "gap": "10px"},
    )


_DRAWER_WIDTH = "40%"


def drawer_toggle_style(is_open: bool) -> dict:
    """Pin the arrow to the drawer's left edge when open, to the screen edge when closed.

    ``zIndex 1900`` sits below the bottom analytics panel (``2000``) so an open panel covers the
    arrow (it no longer obscures the panel), but above the sticky tab bars.
    """
    return {
        "position": "fixed",
        "right": _DRAWER_WIDTH if is_open else "0",
        "top": "45%",
        "zIndex": 1900,
        "borderRadius": "6px 0 0 6px",
        "padding": "10px 6px",
        "transition": "right 0.3s ease",
    }


def build_drawer_shell() -> html.Div:
    """Static Offcanvas + the fixed arrow toggle, mounted once in the shell."""
    toggle = dbc.Button(
        "◂",
        id="rmd-drawer-toggle",
        n_clicks=0,
        color="dark",
        size="sm",
        style=drawer_toggle_style(False),
    )
    # Header carries the title + a Clear button (clears every pinned contract). The title accepts a
    # component, so the button rides beside the text; styled as a small grey outline button, matching
    # the existing outline-button pattern (`★ Mark`, `Clear Basket`) — on-theme, no new chrome.
    # (The Intraday/Swing toggle was removed — the chart is intraday-only, today-session.)
    header = html.Div(
        [
            html.Span(
                "Reversal Curves",
                style={"fontSize": "0.9rem", "fontWeight": 600, "whiteSpace": "nowrap"},
            ),
            dbc.Button("Clear", id="rmd-clear-button", color="secondary", outline=True,
                       size="sm", n_clicks=0, className="flex-shrink-0"),
        ],
        # One flex `gap` for uniform spacing; nowrap keeps the header on ONE line so it never steals
        # vertical space from the charts. justify-content-between pushes Clear to the right edge.
        style={"display": "flex", "alignItems": "center", "justifyContent": "space-between",
               "flexWrap": "nowrap", "gap": "0.5rem"},
    )
    drawer = dbc.Offcanvas(
        html.Div(id="rmd-drawer-body", children=build_drawer_body([]), style={"height": "100%"}),
        id="rmd-drawer",
        title=header,
        placement="end",
        is_open=False,
        backdrop=False,
        scrollable=True,
        # No header X — the fixed arrow toggle (rmd-drawer-toggle) is the sole open/close control,
        # so the X is redundant chrome (and crowded the Clear button). Mirrors the playback drawer.
        close_button=False,
        # zIndex 1500 lifts the drawer above the sticky sub-tabs (1190) so it's no longer behind
        # them; still below the bottom analytics panel (2000).
        style={"width": _DRAWER_WIDTH, "zIndex": 1500},
    )
    return html.Div([toggle, drawer])
