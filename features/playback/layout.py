"""Playback feature layout — the compact body of the bottom slide-in drawer."""

from __future__ import annotations

from dash import dcc, html
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc


# Dark-native dmc.Select styling (same Styles API as the command bar / Advanced — legible menu).
# The drawer sits at the screen bottom, so the menus open UPWARD (`position: top-start`) and portal
# out of the drawer's clip box; `zIndex` lifts them above the drawer + bottom panel.
_SELECT_STYLES = {
    "input": {"backgroundColor": "#101725", "color": "#e8edf7", "borderColor": "#2a3a5c"},
    "dropdown": {"backgroundColor": "#18233c", "borderColor": "#2a3a5c"},
    "option": {"color": "#e8edf7"},
    "label": {"color": "#8a97b5", "fontWeight": 600, "fontSize": "0.72rem", "marginBottom": "2px"},
    "pill": {"backgroundColor": "rgba(34,211,168,0.18)", "color": "#bff3e4"},
    "empty": {"color": "#8a97b5"},
}
_COMBO = {"zIndex": 3000, "position": "top-start"}


def build_playback_drawer_body() -> html.Div:
    """Compact playback controls for the bottom overlay drawer.

    Holds the same control ids the playback/mark callbacks drive (asset/expiry/date/moment selectors,
    Prev/Load/Next/Back buttons, scrubber, bookmark-jump, and the current/live/status labels). The
    ``playback-live-indicator`` is where the "N newer live moment(s) available" signal surfaces while
    the view is pinned to a stored moment and live keeps refreshing behind. The selectors are
    dark-native ``dmc.Select`` and span the full drawer width; only the buttons stay content-sized.
    """
    return html.Div(
        [
            dbc.Row(
                [
                    dbc.Col(
                        dmc.Select(
                            id="playback-asset-selector",
                            label="Asset",
                            data=[],
                            allowDeselect=False,
                            searchable=True,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        md=2,
                    ),
                    dbc.Col(
                        dmc.Select(
                            id="playback-expiry-selector",
                            label="Expiry",
                            data=[],
                            allowDeselect=False,
                            searchable=True,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        md=1,
                    ),
                    dbc.Col(
                        dmc.Select(
                            id="playback-date-selector",
                            label="Date",
                            data=[],
                            allowDeselect=False,
                            searchable=True,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        md=1,
                    ),
                    dbc.Col(
                        dmc.Select(
                            id="playback-moment-selector",
                            label="Time",
                            data=[],
                            allowDeselect=False,
                            searchable=True,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        md=3,
                    ),
                    dbc.Col(
                        dmc.Select(
                            id="playback-bookmark-jump-selector",
                            label="Bookmark",
                            data=[],
                            placeholder="Jump to Bookmark",
                            searchable=True,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        md=2,
                    ),
                    dbc.Col(
                        [
                            html.Div(" ", className="small mb-0"),
                            dbc.ButtonGroup(
                                [
                                    dbc.Button("Prev", id="playback-prev-button", color="secondary", size="sm", n_clicks=0),
                                    dbc.Button("Load", id="playback-load-button", color="primary", size="sm", n_clicks=0),
                                    dbc.Button("Next", id="playback-next-button", color="secondary", size="sm", n_clicks=0),
                                    dbc.Button("Back to Live", id="playback-back-button", color="warning", size="sm", n_clicks=0),
                                ],
                                size="sm",
                            ),
                        ],
                        width="auto",
                    ),
                ],
                className="g-2 align-items-end",
            ),
            html.Div(
                dcc.Slider(
                    id="playback-moment-slider",
                    min=0,
                    max=0,
                    step=None,
                    value=0,
                    marks={},
                    updatemode="mouseup",
                ),
                className="mt-2",
            ),
            html.Div(
                [
                    html.Span(id="playback-current-label", className="small me-3"),
                    html.Span(id="playback-live-indicator", className="small me-3"),
                    html.Span(id="playback-status", className="small", style={"color": "var(--oc-muted)"}),
                ],
                className="mt-1",
            ),
        ]
    )
