"""The meta/rows store split — LoadedReportState.to_meta_dict / rows_payload / from_meta_and_rows.

The split must be lossless: rehydrating the meta + rows halves reproduces the original state
(including the per-strike rows and multi-pair/aggregate states), and the meta half on its own
carries every scalar but no rows.
"""

from __future__ import annotations

from domain.requests import RequestSpec
from state.models import LoadedPairState, LoadedReportState


def _spec(asset_id: int, expiry: str) -> RequestSpec:
    return RequestSpec(
        asset_id=asset_id,
        segment_code="IDX_I",
        expiry=expiry,
        display_name="Nifty",
        lot_size=65,
        strike_size=50,
    )


def _pair(asset_id: int, expiry: str) -> LoadedPairState:
    spec = _spec(asset_id, expiry)
    report = {
        "underlying_price": 22500.0,
        "totals": {"call_oi": 10, "put_oi": 12},
        "rows": [
            {"strike": 22500.0, "option_type": "CE", "oi_lots": 5, "pain_point": 22550.0},
            {"strike": 22500.0, "option_type": "PE", "oi_lots": 7, "pain_point": 22450.0},
        ],
    }
    return LoadedPairState(request=spec, display_label="Nifty | " + expiry, view_key=spec.pair_key.value, report=report)


def _state(*pairs: LoadedPairState) -> LoadedReportState:
    pair_list = list(pairs)
    return LoadedReportState(
        source="live",
        version="v1",
        pair_order=[p.resolved_view_key for p in pair_list],
        loaded_pairs=pair_list,
        fetch_group_id="fg-1",
        active_pair_key=pair_list[0].resolved_view_key if pair_list else None,
    )


def test_meta_rows_round_trip_single_pair():
    state = _state(_pair(13, "2026-04-30"))
    rehydrated = LoadedReportState.from_meta_and_rows(state.to_meta_dict(), state.rows_payload())
    assert rehydrated is not None
    assert rehydrated.to_dict() == state.to_dict()


def test_meta_rows_round_trip_multi_pair_aggregate():
    state = _state(_pair(13, "2026-04-30"), _pair(13, "2026-05-07"), _pair(13, "2026-05-14"))
    rehydrated = LoadedReportState.from_meta_and_rows(state.to_meta_dict(), state.rows_payload())
    assert rehydrated.to_dict() == state.to_dict()
    # every pair's rows came back, keyed by view_key
    for original, restored in zip(state.loaded_pairs, rehydrated.loaded_pairs):
        assert restored.report["rows"] == original.report["rows"]


def test_meta_dict_carries_scalars_but_no_rows():
    state = _state(_pair(13, "2026-04-30"))
    meta = state.to_meta_dict()
    pair_report = meta["loaded_pairs"][0]["report"]
    assert "rows" not in pair_report
    assert pair_report["underlying_price"] == 22500.0
    assert pair_report["totals"] == {"call_oi": 10, "put_oi": 12}
    # a meta-only from_dict yields empty rows (scalar readers never touch them)
    meta_only = LoadedReportState.from_dict(meta)
    assert meta_only.loaded_pairs[0].report.get("rows", []) == []


def test_rows_payload_keyed_by_view_key():
    state = _state(_pair(13, "2026-04-30"), _pair(13, "2026-05-07"))
    payload = state.rows_payload()
    assert set(payload.keys()) == {"13|IDX_I|2026-04-30", "13|IDX_I|2026-05-07"}
    assert len(payload["13|IDX_I|2026-04-30"]) == 2


def test_from_meta_and_rows_none_meta_returns_none():
    assert LoadedReportState.from_meta_and_rows(None, {}) is None
    assert LoadedReportState.from_meta_and_rows({}, {}) is None


def test_from_meta_and_rows_tolerates_missing_rows():
    # meta present, rows store empty (e.g. a meta-only writer path) → scalars intact, rows empty.
    state = _state(_pair(13, "2026-04-30"))
    rehydrated = LoadedReportState.from_meta_and_rows(state.to_meta_dict(), None)
    assert rehydrated is not None
    assert rehydrated.loaded_pairs[0].report.get("rows", []) == []
    assert rehydrated.loaded_pairs[0].report["underlying_price"] == 22500.0
