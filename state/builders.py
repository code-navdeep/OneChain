"""Helpers for building state objects."""

from __future__ import annotations

from uuid import uuid4

from domain.requests import RequestSpec
from repositories.asset_repository import AssetRepository
from state.models import (
    ActiveViewContext,
    ControlDraftState,
    LoadedPairState,
    LoadedReportState,
    RequestPlanState,
)


def make_state_version() -> str:
    """Generate a new opaque state version token."""
    return str(uuid4())


def build_loaded_report_state(
    source: str,
    request_specs: list[RequestSpec],
    fetch_group_id: str | None = None,
    active_pair_key: str | None = None,
    pair_reports: dict[str, dict] | None = None,
    status_text: str = "",
) -> LoadedReportState:
    """Build a minimal loaded-report state from ordered request specs."""
    loaded_pairs = [
        LoadedPairState(
            request=spec,
            display_label=f"{spec.display_name} | {spec.expiry}",
            view_key=spec.pair_key.value,
            report=dict((pair_reports or {}).get(spec.pair_key.value, {})),
        )
        for spec in request_specs
    ]
    pair_order = [item.resolved_view_key for item in loaded_pairs]
    resolved_active = active_pair_key or (pair_order[0] if pair_order else None)
    return LoadedReportState(
        source=source,
        version=make_state_version(),
        pair_order=pair_order,
        loaded_pairs=loaded_pairs,
        fetch_group_id=fetch_group_id,
        active_pair_key=resolved_active,
        status_text=status_text,
    )


def build_request_specs_from_control_draft(
    draft_state: ControlDraftState,
    asset_repository: AssetRepository,
) -> list[RequestSpec]:
    """Build canonical request specs from the current control draft."""
    if draft_state.request_mode == "multi_instrument":
        return [RequestSpec.from_dict(item) for item in draft_state.request_basket]

    if not draft_state.asset_value:
        return []

    asset = asset_repository.get_asset_by_instrument_key(draft_state.asset_value)
    if asset is None:
        return []

    return [
        RequestSpec(
            asset_id=asset.asset_id,
            segment_code=asset.segment_code,
            expiry=expiry,
            display_name=asset.display_name,
            lot_size=asset.lot_size,
            strike_size=asset.strike_size,
        )
        for expiry in draft_state.selected_expiries
    ]


def build_request_plan_state(
    draft_state: ControlDraftState,
    asset_repository: AssetRepository,
    source: str = "draft",
    active_pair_key: str | None = None,
) -> RequestPlanState:
    """Build canonical request-plan state from the current control draft."""
    request_specs = build_request_specs_from_control_draft(draft_state, asset_repository)
    pair_order = [spec.pair_key.value for spec in request_specs]
    resolved_active_pair_key = active_pair_key if active_pair_key in pair_order else (pair_order[0] if pair_order else None)
    return RequestPlanState(
        source=source,
        version=make_state_version(),
        request_mode=draft_state.request_mode,
        pair_order=pair_order,
        request_specs=request_specs,
        active_pair_key=resolved_active_pair_key,
    )


def build_active_view_context_from_pair_state(
    *,
    source: str,
    pair_key: str | None,
    fetch_group_id: str | None = None,
    metadata: dict | None = None,
) -> ActiveViewContext:
    """Build active view context from a pair selection."""
    instrument_key = None
    if pair_key:
        instrument_key = pair_key.rsplit("|", 1)[0]

    return ActiveViewContext(
        kind="pair",
        source=source,
        instrument_key=instrument_key,
        pair_key=pair_key,
        fetch_group_id=fetch_group_id,
        metadata=dict(metadata or {}),
    )


def build_active_view_context_from_aggregate_state(
    *,
    source: str,
    instrument_key: str,
    aggregate_view_key: str,
    fetch_group_id: str | None = None,
    metadata: dict | None = None,
) -> ActiveViewContext:
    """Build active view context for synthetic aggregate mode."""
    return ActiveViewContext(
        kind="aggregate",
        source=source,
        instrument_key=instrument_key,
        pair_key=aggregate_view_key,
        fetch_group_id=fetch_group_id,
        metadata=dict(metadata or {}),
    )
