"""Tests for the read-only EOD status reflector (the trigger now lives in test_eod_scheduler)."""

from __future__ import annotations

from dash import no_update

from features.automation.callbacks import _compute_status_reflection
from state.models import AutomationState


TODAY = "2026-06-29"


def test_reflection_no_flip_before_any_eod_run():
    # No run today yet (last_run_date None): never flip auto-refresh, never write status.
    stored = AutomationState(last_started_date=None, in_progress=False, last_status=None)
    updated, next_refresh, status_text = _compute_status_reflection(
        last_run_date=None,
        in_progress=False,
        last_status=None,
        stored=stored,
        auto_refresh_value="5000",
        today=TODAY,
    )
    assert next_refresh is no_update
    assert status_text is no_update
    assert updated.last_started_date is None


def test_reflection_flips_auto_refresh_off_once_on_start_transition():
    # First tick that sees today's run, with the UI not yet recording it → flip selector off + record.
    stored = AutomationState(last_started_date=None, in_progress=True, last_status=None)
    updated, next_refresh, status_text = _compute_status_reflection(
        last_run_date=TODAY,
        in_progress=True,
        last_status="15:30 snapshot run started.",
        stored=stored,
        auto_refresh_value="5000",
        today=TODAY,
    )
    assert next_refresh == "off"
    assert updated.last_started_date == TODAY
    assert status_text == "15:30 snapshot run started."


def test_reflection_flip_is_one_shot_user_can_re_enable():
    # Subsequent tick: the UI already recorded today's start → no_update for the selector even if the
    # user has re-enabled auto-refresh (so the flip never fights the user for the rest of the day).
    stored = AutomationState(last_started_date=TODAY, in_progress=False, last_status="done.")
    updated, next_refresh, status_text = _compute_status_reflection(
        last_run_date=TODAY,
        in_progress=False,
        last_status="done.",
        stored=stored,
        auto_refresh_value="5000",
        today=TODAY,
    )
    assert next_refresh is no_update
    # status unchanged from stored → don't clobber the user's current controls-status.
    assert status_text is no_update


def test_reflection_writes_status_only_when_it_changes():
    stored = AutomationState(last_started_date=TODAY, in_progress=True, last_status="old status")
    _updated, _next_refresh, status_text = _compute_status_reflection(
        last_run_date=TODAY,
        in_progress=False,
        last_status="new status",
        stored=stored,
        auto_refresh_value="off",
        today=TODAY,
    )
    assert status_text == "new status"
