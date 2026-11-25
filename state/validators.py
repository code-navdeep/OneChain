"""Validation helpers for state payloads used in Dash stores."""

from __future__ import annotations


def validate_loaded_report_state_payload(payload: dict) -> None:
    required_keys = {"source", "version", "pair_order", "loaded_pairs"}
    missing = required_keys - set(payload.keys())
    if missing:
        raise ValueError(f"LoadedReportState payload missing keys: {sorted(missing)}")


def validate_active_view_context_payload(payload: dict) -> None:
    required_keys = {"kind", "source", "metadata"}
    missing = required_keys - set(payload.keys())
    if missing:
        raise ValueError(f"ActiveViewContext payload missing keys: {sorted(missing)}")


def validate_request_plan_state_payload(payload: dict) -> None:
    required_keys = {"source", "version", "request_mode", "pair_order", "request_specs"}
    missing = required_keys - set(payload.keys())
    if missing:
        raise ValueError(f"RequestPlanState payload missing keys: {sorted(missing)}")
