from __future__ import annotations

from datetime import datetime

from domain.assets import AssetDefinition
from features.controls.helpers import (
    build_asset_options,
    build_basket_remove_options,
    build_expiry_options,
    build_request_spec,
)
from ui.formatters import format_expiry_label, format_mark_time_label


def test_asset_options_are_grouped_indices_then_commodities_then_equities():
    assets = [
        AssetDefinition(236, "ASIANPAINT", "NSE", "NSE_EQ", 20, 250, "ASIANPAINT"),
        AssetDefinition(486502, "CrudeOil", "MCX", "MCX_COMM", 50, 100, "CrudeOil"),
        AssetDefinition(25, "BankNifty", "NSE", "IDX_I", 100, 25, "BankNifty"),
        AssetDefinition(13, "Nifty", "NSE", "IDX_I", 50, 65, "Nifty"),
    ]

    labels = [item["label"] for item in build_asset_options(assets)]
    assert labels == ["BankNifty", "Nifty", "CrudeOil", "ASIANPAINT"]


def test_expiry_labels_are_human_readable():
    options = build_expiry_options(["2026-03-30"])
    assert options == [{"label": "30 Mar 2026", "value": "2026-03-30"}]
    assert format_expiry_label("2026-03-30") == "30 Mar 2026"


def test_mark_time_label_is_human_readable_no_leading_zero():
    assert format_mark_time_label(datetime(2026, 6, 11, 12, 35)) == "12:35pm"
    assert format_mark_time_label(datetime(2026, 6, 11, 9, 5)) == "9:05am"
    assert format_mark_time_label(datetime(2026, 6, 11, 0, 0)) == "12:00am"


def test_request_spec_and_basket_remove_options_use_segment_safe_keys():
    asset = AssetDefinition(25, "ADANIENT", "NSE", "NSE_EQ", 20, 1, "ADANIENT")
    request_spec = build_request_spec(asset, "2026-04-30")
    assert request_spec.pair_key.value == "25|NSE_EQ|2026-04-30"

    options = build_basket_remove_options([request_spec.to_dict()])
    assert options[0]["value"] == "25|NSE_EQ|2026-04-30"
