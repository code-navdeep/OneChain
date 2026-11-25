"""Alert-ticker rendering — the scrolling marquee content for the bottom-panel header."""

from __future__ import annotations

from typing import Any

from dash import html

from analytics.alerts import ALERT_CATEGORY_COLORS


# Scroll-duration scaling. The track travels container_width + text_width, so a constant
# visual speed needs duration = base + chars × per_char. Keeps the marquee at a steady pace
# whether there is one alert or a full strip.
_TICKER_BASE_SECONDS = 8.0
_TICKER_SECONDS_PER_CHAR = 0.08


def build_ticker(alerts: list[dict[str, Any]]) -> html.Div:
    """Build the marquee from the rolling ``Alert``-dict list.

    Empty list → an empty (non-scrolling) span. Non-empty → a fixed "LIVE" badge + a scrolling
    viewport whose track holds one colour-coded span per alert (colour by ``category``), each led by a
    dot and showing ``time body``. The viewport pauses on hover (CSS in ``onechain.css``); the
    animation duration is set inline so the speed stays constant regardless of strip length.
    """
    if not alerts:
        return html.Div(html.Span("", className="oc-ticker-track"), className="oc-ticker")

    items: list = []
    for alert in alerts:
        color = ALERT_CATEGORY_COLORS[alert["category"]]
        display = f"{alert.get('time', '')} {alert.get('body', '')}".strip()
        items.append(html.Span(["● ", display], className="oc-ticker-item", style={"color": color}))
        items.append(html.Span("   ·   ", className="oc-ticker-sep"))

    total_len = sum(len(a.get("body", "")) + len(a.get("time", "")) for a in alerts) + len(alerts) * 6
    duration = _TICKER_BASE_SECONDS + total_len * _TICKER_SECONDS_PER_CHAR
    track = html.Span(items, className="oc-ticker-track", style={"animationDuration": f"{duration:.1f}s"})

    return html.Div(
        [
            html.Span([html.Span(className="oc-live-dot"), "LIVE"], className="oc-ticker-live"),
            html.Div(track, className="oc-ticker-viewport"),
        ],
        className="oc-ticker-row",
    )
