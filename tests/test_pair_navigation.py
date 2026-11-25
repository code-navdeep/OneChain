from __future__ import annotations

from domain.requests import RequestSpec
from state.builders import build_loaded_report_state
from state.models import LoadedPairState, LoadedReportState, RequestPlanState
from ui.pair_navigation import build_pair_navigation, build_pair_tab_label


def test_pair_tab_label_shows_name_and_short_yearless_expiry():
    assert build_pair_tab_label("Nifty", "2026-04-30") == "Nifty | 30 Apr"
    # no leading zero on the day, no year
    assert build_pair_tab_label("ADANIENT", "2026-05-07") == "ADANIENT | 7 May"


def test_pair_navigation_renders_tabs_for_multi_pair_plan():
    request_specs = [
        RequestSpec(
            asset_id=13,
            segment_code="IDX_I",
            expiry="2026-04-30",
            display_name="Nifty",
            lot_size=65,
            strike_size=50,
        ),
        RequestSpec(
            asset_id=13,
            segment_code="IDX_I",
            expiry="2026-05-07",
            display_name="Nifty",
            lot_size=65,
            strike_size=50,
        ),
    ]
    request_plan = RequestPlanState(
        source="draft",
        version="v1",
        request_mode="single_instrument_multi_expiry",
        pair_order=[spec.pair_key.value for spec in request_specs],
        request_specs=request_specs,
        active_pair_key=request_specs[0].pair_key.value,
    )
    card = build_pair_navigation(
        request_plan_state=request_plan,
        loaded_report_state=None,
        active_pair_key=request_specs[1].pair_key.value,
    )
    assert "loaded-pair-tabs" in repr(card)


def test_pair_navigation_prefers_loaded_state_when_available():
    request_specs = [
        RequestSpec(
            asset_id=25,
            segment_code="NSE_EQ",
            expiry="2026-04-30",
            display_name="ADANIENT",
            lot_size=300,
            strike_size=20,
        ),
        RequestSpec(
            asset_id=13,
            segment_code="IDX_I",
            expiry="2026-04-30",
            display_name="Nifty",
            lot_size=65,
            strike_size=50,
        ),
    ]
    loaded_state = build_loaded_report_state(source="live", request_specs=request_specs)
    card = build_pair_navigation(
        request_plan_state=None,
        loaded_report_state=loaded_state,
        active_pair_key=request_specs[0].pair_key.value,
    )
    assert "ADANIENT | 30 Apr" in repr(card)


def test_pair_navigation_does_not_add_aggregate_tab_for_single_instrument_multi_expiry_loaded_state():
    request_specs = [
        RequestSpec(
            asset_id=13,
            segment_code="IDX_I",
            expiry="2026-04-30",
            display_name="Nifty",
            lot_size=65,
            strike_size=50,
        ),
        RequestSpec(
            asset_id=13,
            segment_code="IDX_I",
            expiry="2026-05-07",
            display_name="Nifty",
            lot_size=65,
            strike_size=50,
        ),
    ]
    loaded_state = build_loaded_report_state(source="live", request_specs=request_specs)
    card = build_pair_navigation(
        request_plan_state=None,
        loaded_report_state=loaded_state,
        active_pair_key=request_specs[0].pair_key.value,
    )
    rendered = repr(card)
    assert "Aggregate" not in rendered
    assert "Nifty | 30 Apr" in rendered
    assert "Nifty | 7 May" in rendered
