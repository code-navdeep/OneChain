"""Pure helpers for the controls feature."""

from __future__ import annotations

from typing import Any

from dash import html

from domain.assets import AssetDefinition
from domain.requests import RequestSpec
from ui.formatters import format_expiry_label

REQUEST_MODE_OPTIONS = [
    {"label": "Single Instrument", "value": "single_instrument"},
    {"label": "Multi Instrument", "value": "multi_instrument"},
]

STRIKE_RANGE_OPTIONS = [
    {"label": "±8", "value": "8"},
    {"label": "±10", "value": "10"},
    {"label": "±12", "value": "12"},
    {"label": "±16", "value": "16"},
    {"label": "±32", "value": "32"},
    {"label": "All", "value": "All"},
]

AUTO_REFRESH_OPTIONS = [
    {"label": "Off", "value": "off"},
    {"label": "5s", "value": "5000"},
    {"label": "15s", "value": "15000"},
    {"label": "30s", "value": "30000"},
    {"label": "1m", "value": "60000"},
    {"label": "5m", "value": "300000"},
]

AGGREGATE_MODE_OPTIONS = [
    {"label": "Off", "value": "off"},
    {"label": "On", "value": "on"},
]


def _asset_sort_bucket(asset: AssetDefinition) -> tuple[int, str]:
    if asset.segment_code == "IDX_I":
        bucket = 0
    elif asset.segment_code == "MCX_COMM":
        bucket = 1
    else:
        bucket = 2
    return bucket, asset.display_name.lower()


def build_asset_options(assets: list[AssetDefinition]) -> list[dict[str, str]]:
    """Build segment-safe asset dropdown options in the preferred order."""
    ordered_assets = sorted(assets, key=_asset_sort_bucket)
    return [
        {"label": asset.display_name, "value": asset.instrument_key.value}
        for asset in ordered_assets
    ]


def build_expiry_options(expiries: list[str]) -> list[dict[str, str]]:
    return [{"label": format_expiry_label(expiry), "value": expiry} for expiry in expiries]


def build_request_spec(asset: AssetDefinition, expiry: str) -> RequestSpec:
    return RequestSpec(
        asset_id=asset.asset_id,
        segment_code=asset.segment_code,
        expiry=expiry,
        display_name=asset.display_name,
        lot_size=asset.lot_size,
        strike_size=asset.strike_size,
    )


def build_basket_remove_options(basket: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"label": f"{item['display_name']} | {format_expiry_label(item['expiry'])}", "value": item["pair_key"]}
        for item in basket
    ]


def build_basket_summary(basket: list[dict[str, Any]]) -> html.Div:
    if not basket:
        return html.Div("Basket is empty.")

    return html.Div(
        [
            html.Div(f"{len(basket)} request(s) in basket"),
            html.Ul(
                [
                    html.Li(f"{item['display_name']} | {format_expiry_label(item['expiry'])}")
                    for item in basket
                ]
            ),
        ]
    )
