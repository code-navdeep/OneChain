"""On-theme empty-state / suppressed-feature notices.

A single restrained notice that matches the dark "Midnight Quant" theme — used in place of
Bootstrap's default light ``dbc.Alert`` / ``dbc.Card(dbc.CardBody(text))``, which render as
jarring pastel boxes on the navy UI. The visual contract lives in ``.oc-notice`` (+ tone
modifiers) in ``assets/onechain.css``: a dark surface, muted text, and a thin left accent border
for severity (teal=info, amber=warning, neutral=muted) — never a full pastel fill.
"""

from __future__ import annotations

from dash import html

_TONES = {"muted", "info", "warning"}


def build_notice(message: str, *, tone: str = "muted", className: str = "") -> html.Div:
    """A dark, on-theme notice block. ``tone`` selects the left accent border colour;
    ``className`` is a layout-only passthrough (e.g. ``h-100`` to fill a chart column)."""
    tone = tone if tone in _TONES else "muted"
    classes = f"oc-notice oc-notice-{tone}" + (f" {className}" if className else "")
    return html.Div(message, className=classes)
