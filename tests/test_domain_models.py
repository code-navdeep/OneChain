from __future__ import annotations

from domain.assets import AssetDefinition
from domain.keys import InstrumentKey, PairKey
from domain.requests import RequestSpec
from repositories.asset_repository import AssetRepository
from state.builders import (
    build_active_view_context_from_pair_state,
    build_loaded_report_state,
    build_request_plan_state,
)
from state.models import (
    ActiveViewContext,
    ControlDraftState,
    LoadedReportState,
    PlaybackMoment,
    PlaybackState,
    RequestPlanState,
)
from state.validators import (
    validate_active_view_context_payload,
    validate_loaded_report_state_payload,
    validate_request_plan_state_payload,
)


def test_instrument_key_round_trip():
    key = InstrumentKey(asset_id=25, segment_code="IDX_I")
    assert InstrumentKey.from_value(key.value) == key


def test_pair_key_round_trip():
    key = PairKey(asset_id=25, segment_code="NSE_EQ", expiry="2026-04-30")
    assert PairKey.from_value(key.value) == key


def test_asset_definition_preserves_decimal_strike_size():
    # ITC / TATASTEEL trade on a 2.5-wide grid — the CSV value must NOT truncate to 2.
    asset = AssetDefinition.from_mapping(
        {
            "asset_id": "1660",
            "asset_name": "ITC",
            "exchange": "NSE",
            "segment_code": "NSE_EQ",
            "strike_size": "2.5",
            "lot_size": "1600",
            "display_name": "ITC",
            "number_expiries": "2",
        }
    )
    assert asset.strike_size == 2.5


def test_request_spec_round_trips_decimal_strike_size():
    spec = RequestSpec(
        asset_id=1660,
        segment_code="NSE_EQ",
        expiry="2026-04-30",
        display_name="ITC",
        lot_size=1600,
        strike_size=2.5,
    )
    restored = RequestSpec.from_dict(spec.to_dict())
    assert restored.strike_size == 2.5
    assert restored == spec


def test_request_spec_exposes_segment_safe_keys():
    spec = RequestSpec(
        asset_id=25,
        segment_code="NSE_EQ",
        expiry="2026-04-30",
        display_name="ADANIENT",
        lot_size=1,
        strike_size=20,
    )
    assert spec.instrument_key.value == "25|NSE_EQ"
    assert spec.pair_key.value == "25|NSE_EQ|2026-04-30"


def test_loaded_report_state_round_trip_and_validation():
    request_specs = [
        RequestSpec(
            asset_id=13,
            segment_code="IDX_I",
            expiry="2026-04-30",
            display_name="NIFTY",
            lot_size=65,
            strike_size=50,
        )
    ]
    state = build_loaded_report_state(source="live", request_specs=request_specs, fetch_group_id="group-1")
    payload = state.to_dict()
    validate_loaded_report_state_payload(payload)
    restored = LoadedReportState.from_dict(payload)
    assert restored == state


def test_active_view_context_round_trip_and_validation():
    context = ActiveViewContext(
        kind="pair",
        source="live",
        instrument_key="13|IDX_I",
        pair_key="13|IDX_I|2026-04-30",
        fetch_group_id="group-1",
        metadata={"from_test": True},
    )
    payload = context.to_dict()
    validate_active_view_context_payload(payload)
    restored = ActiveViewContext.from_dict(payload)
    assert restored == context


def test_request_plan_state_from_single_instrument_multi_expiry():
    asset_repository = AssetRepository("assets/assets_select.csv")
    draft_state = ControlDraftState(
        request_mode="single_instrument_multi_expiry",
        asset_value="13|IDX_I",
        selected_expiries=["2026-04-30", "2026-05-07"],
    )
    state = build_request_plan_state(draft_state, asset_repository)
    payload = state.to_dict()
    validate_request_plan_state_payload(payload)
    restored = RequestPlanState.from_dict(payload)
    assert restored == state
    assert restored.pair_order == ["13|IDX_I|2026-04-30", "13|IDX_I|2026-05-07"]


def test_request_plan_state_from_multi_instrument_basket():
    asset_repository = AssetRepository("assets/assets_select.csv")
    draft_state = ControlDraftState(
        request_mode="multi_instrument",
        request_basket=[
            {
                "asset_id": 13,
                "segment_code": "IDX_I",
                "expiry": "2026-04-30",
                "display_name": "Nifty",
                "lot_size": 65,
                "strike_size": 50,
                "instrument_key": "13|IDX_I",
                "pair_key": "13|IDX_I|2026-04-30",
            },
            {
                "asset_id": 25,
                "segment_code": "NSE_EQ",
                "expiry": "2026-04-30",
                "display_name": "ADANIENT",
                "lot_size": 300,
                "strike_size": 20,
                "instrument_key": "25|NSE_EQ",
                "pair_key": "25|NSE_EQ|2026-04-30",
            },
        ],
    )
    state = build_request_plan_state(draft_state, asset_repository)
    assert state.pair_order == ["13|IDX_I|2026-04-30", "25|NSE_EQ|2026-04-30"]


def test_active_view_context_builder_splits_instrument_from_pair():
    context = build_active_view_context_from_pair_state(
        source="draft",
        pair_key="25|NSE_EQ|2026-04-30",
    )
    assert context.instrument_key == "25|NSE_EQ"
    assert context.pair_key == "25|NSE_EQ|2026-04-30"


def test_playback_state_round_trip():
    state = PlaybackState(
        selected_date="2026-04-10",
        selected_moment_token="group-1",
        detached_from_live=True,
        available_moments=[
            PlaybackMoment(
                token="group-1",
                date="2026-04-10",
                label="09:15:00 | 1 pair(s)",
                start_ts="2026-04-10T03:45:00+00:00",
                end_ts="2026-04-10T03:45:01+00:00",
                fetch_group_id="group-1",
                pair_count=1,
            )
        ],
        newer_live_count=2,
    )
    restored = PlaybackState.from_dict(state.to_dict())
    assert restored == state
