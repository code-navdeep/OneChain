"""In-app status reflector for the server-side EOD automation.

The EOD trigger itself lives in ``services/eod_scheduler.py`` — a server-side thread immune to
browser timer throttling. This module keeps only a thin, **read-only** browser callback that mirrors
the scheduler's status into the UI and flips auto-refresh off **once** when an EOD run starts. It
holds **no** trigger/decision logic: automation lives on exactly one side (the server).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dash import Dash, Input, Output, State, no_update

from services.eod_scheduler import get_or_create_eod_state
from services.runtime import RuntimeServices
from state.models import AutomationState


IST = ZoneInfo("Asia/Kolkata")


def _compute_status_reflection(
    *,
    last_run_date: str | None,
    in_progress: bool,
    last_status: str | None,
    stored: AutomationState,
    auto_refresh_value: str,
    today: str,
):
    """Pure reflection of the scheduler's EOD state into ``(AutomationState, auto-refresh-value,
    status-text)`` for the UI. Two behaviours, both unit-tested:

    - **One-shot auto-refresh "off" flip:** the first tick that sees a run for today
      (``last_run_date == today``) which the UI hasn't recorded yet (``stored.last_started_date !=
      today``) returns ``"off"`` for the selector and records the date. Subsequent ticks see it
      recorded → ``no_update``, so the user can re-enable auto-refresh afterwards (matches the prior
      force-off-once behaviour; the token is rotated during EOD, so fetching after is pointless).
    - **controls-status written only on change:** ``status-text`` is returned only when the EOD status
      text actually changes, so a 10 s reflector tick never clobbers the user's own fetch/controls
      status with a stale EOD line."""
    started_today = last_run_date == today
    flip = started_today and stored.last_started_date != today
    next_refresh = "off" if (flip and auto_refresh_value != "off") else no_update
    status_text = last_status if (last_status and last_status != stored.last_status) else no_update
    updated = AutomationState(
        last_started_date=today if started_today else stored.last_started_date,
        in_progress=in_progress,
        last_status=last_status if last_status is not None else stored.last_status,
    )
    return updated, next_refresh, status_text


def register_automation_callbacks(app: Dash, services: RuntimeServices) -> None:
    """Register the read-only EOD status reflector (no trigger logic — see services/eod_scheduler)."""

    @app.callback(
        Output("automation-state-store", "data"),
        Output("auto-refresh-selector", "value", allow_duplicate=True),
        Output("controls-status", "children", allow_duplicate=True),
        Input("snapshot-automation-interval", "n_intervals"),
        State("automation-state-store", "data"),
        State("auto-refresh-selector", "value"),
        prevent_initial_call=True,
    )
    def reflect_eod_status(
        _ticks: int,
        automation_state_data: dict | None,
        auto_refresh_value: str,
    ):
        eod_state = get_or_create_eod_state(services)
        with eod_state.lock:
            last_run_date = eod_state.last_run_date
            in_progress = eod_state.eod_in_progress
            last_status = eod_state.last_status

        stored = AutomationState.from_dict(automation_state_data)
        today = datetime.now(IST).date().isoformat()
        updated, next_refresh, status_text = _compute_status_reflection(
            last_run_date=last_run_date,
            in_progress=in_progress,
            last_status=last_status,
            stored=stored,
            auto_refresh_value=auto_refresh_value,
            today=today,
        )
        return updated.to_dict(), next_refresh, status_text
