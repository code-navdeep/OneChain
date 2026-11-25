"""Layout for the controls feature."""

from __future__ import annotations

from dash import html
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc

from config import AppConfig
from features.controls.helpers import (
    AUTO_REFRESH_OPTIONS,
    REQUEST_MODE_OPTIONS,
    STRIKE_RANGE_OPTIONS,
    build_asset_options,
    build_expiry_options,
)
from repositories.asset_repository import AssetRepository
from adapters.expiry_cache import read_cached_expiries


# DMC Styles API — guarantees the input + dropdown + options are dark and legible regardless of the
# global color scheme (no class-name guessing). `comboboxProps.zIndex` lifts the menu above the
# sticky tab bar.
_SELECT_STYLES = {
    "input": {"backgroundColor": "#101725", "color": "#e8edf7", "borderColor": "#2a3a5c"},
    "dropdown": {"backgroundColor": "#18233c", "borderColor": "#2a3a5c"},
    "option": {"color": "#e8edf7"},
    "label": {"color": "#8a97b5", "fontWeight": 600, "fontSize": "0.72rem", "marginBottom": "2px"},
    "pill": {"backgroundColor": "rgba(34,211,168,0.18)", "color": "#bff3e4"},
    "empty": {"color": "#8a97b5"},
}
_COMBO = {"zIndex": 3000}


def build_controls_layout(config: AppConfig) -> html.Div:
    """Build the compact, set-and-forget control surface (DMC command bar).

    Only the fetch-related controls live here (Mode / Asset / Expiries / Strike / Auto / Fetch) plus
    the Multi-instrument basket. View toggles (Aggregate, Greeks), Mark, the Advanced controls and
    Playback have moved out (tab bar / Advanced tab / bottom drawer). The inputs are dark-native
    ``dmc.Select`` / ``dmc.MultiSelect``.
    """
    asset_repository = AssetRepository(config.assets_csv_path)
    assets = asset_repository.list_assets()
    asset_options = build_asset_options(assets)
    # Default to Nifty (the 90% case) rather than the alphabetically-first asset.
    default_asset_value = next(
        (opt["value"] for opt in asset_options if opt["label"] == "Nifty"),
        asset_options[0]["value"] if asset_options else None,
    )

    default_expiry_options = []
    if default_asset_value:
        asset = asset_repository.get_asset_by_instrument_key(default_asset_value)
        if asset is not None:
            default_expiry_options = build_expiry_options(
                read_cached_expiries(
                    config.data_dir / "expiry_cache.json",
                    asset.asset_id,
                    asset.segment_code,
                )
            )

    default_single_expiry = default_expiry_options[0]["value"] if default_expiry_options else None

    return html.Div(
        [
            dbc.Row(
                [
                    dbc.Col(
                        dmc.Select(
                            id="request-mode-selector",
                            label="Mode",
                            data=REQUEST_MODE_OPTIONS,
                            value="single_instrument",
                            allowDeselect=False,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        md=2,
                    ),
                    dbc.Col(
                        dmc.Select(
                            id="asset-selector",
                            label="Asset",
                            data=asset_options,
                            value=default_asset_value,
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
                        [
                            html.Div(
                                html.Div(
                                    [
                                        dmc.Select(
                                            id="single-expiry-selector",
                                            label="Expiry",
                                            data=default_expiry_options,
                                            value=default_single_expiry,
                                            allowDeselect=False,
                                            size="sm",
                                            miw="150px",
                                            className="flex-grow-1",
                                            comboboxProps=_COMBO,
                                            styles=_SELECT_STYLES,
                                        ),
                                        dmc.Button(
                                            "⟳",
                                            id="refresh-expiries-button-single",
                                            variant="default",
                                            size="sm",
                                            n_clicks=0,
                                            className="ms-1 flex-shrink-0",
                                        ),
                                    ],
                                    className="d-flex align-items-end",
                                ),
                                id="controls-single-expiry-wrapper",
                            ),
                            html.Div(
                                html.Div(
                                    [
                                        dmc.MultiSelect(
                                            id="multi-expiry-selector",
                                            label="Expiries",
                                            data=default_expiry_options,
                                            value=[default_single_expiry] if default_single_expiry else [],
                                            size="sm",
                                            miw="150px",
                                            className="flex-grow-1",
                                            comboboxProps=_COMBO,
                                            styles=_SELECT_STYLES,
                                        ),
                                        dmc.Button(
                                            "⟳",
                                            id="refresh-expiries-button",
                                            variant="default",
                                            size="sm",
                                            n_clicks=0,
                                            className="ms-1 flex-shrink-0",
                                        ),
                                    ],
                                    className="d-flex align-items-end",
                                ),
                                id="controls-multi-expiry-wrapper",
                                style={"display": "none"},
                            ),
                        ],
                        md=3,
                    ),
                    dbc.Col(
                        dmc.Select(
                            id="strike-range-selector",
                            label="Strike",
                            data=STRIKE_RANGE_OPTIONS,
                            value="10",
                            allowDeselect=False,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        md=2,
                    ),
                    dbc.Col(
                        dmc.Select(
                            id="auto-refresh-selector",
                            label="Auto",
                            data=AUTO_REFRESH_OPTIONS,
                            value="off",
                            allowDeselect=False,
                            size="sm",
                            w="100%",
                            comboboxProps=_COMBO,
                            styles=_SELECT_STYLES,
                        ),
                        md=1,
                    ),
                    dbc.Col(
                        dmc.Button(
                            "Fetch",
                            id="fetch-live-button",
                            color="teal",
                            size="sm",
                            n_clicks=0,
                        ),
                        width="auto",
                    ),
                ],
                className="g-2 align-items-end",
            ),
            dbc.Row(
                dbc.Col(
                    html.Div(
                        "Ready to fetch live data.",
                        id="controls-status",
                        className="onechain-controls-status small",
                    ),
                    width=12,
                ),
                className="g-2 mt-1",
            ),
            html.Div(
                [
                    dbc.Row(
                        [
                            dbc.Col(
                                dmc.Button("Add Request", id="controls-add-request-button", color="blue", size="sm", n_clicks=0),
                                width="auto",
                            ),
                            dbc.Col(
                                dmc.Select(
                                    id="controls-basket-remove-selector",
                                    data=[],
                                    placeholder="Select basket item to remove",
                                    size="sm",
                                    w="100%",
                                    comboboxProps=_COMBO,
                                    styles=_SELECT_STYLES,
                                ),
                                md=5,
                            ),
                            dbc.Col(
                                dmc.Button("Remove Selected", id="controls-remove-request-button", variant="default", size="sm", n_clicks=0),
                                width="auto",
                            ),
                            dbc.Col(
                                dmc.Button("Clear Basket", id="controls-clear-basket-button", color="red", variant="outline", size="sm", n_clicks=0),
                                width="auto",
                            ),
                        ],
                        className="g-2 align-items-end",
                    ),
                    html.Hr(className="my-2"),
                    html.Div(id="controls-basket-summary", children="Basket is empty.", className="small"),
                ],
                id="controls-basket-wrapper",
                style={"display": "none"},
                className="mt-2",
            ),
        ],
        className="onechain-controls-content",
    )
