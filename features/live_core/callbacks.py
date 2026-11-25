"""Live core callbacks — commit (browser → server collector) + render poll (server → browser).

The live fetch loop no longer runs in the browser. A server-side thread
(``services/live_collector.py``) owns fetching; these two callbacks are the browser's ends of it:

- ``commit_live_selection`` — on a Fetch click or an auto-refresh change, writes the request set +
  cadence into the shared collector state (a discrete *commit*), then wakes the thread. It does NOT
  fetch.
- ``render_live_data`` — an always-on render poll: when the collector reports a newer
  ``fetch_group_id`` than this tab last rendered, rebuilds the dashboard from the stored group (the
  proven playback ``load_fetch_group`` path, ``source="live"``) and writes the loaded/live stores.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dash import Dash, Input, Output, State, ctx, no_update

IST = ZoneInfo("Asia/Kolkata")

from services.live_collector import _DEFAULT_CADENCE_MS, get_or_create_live_collector_state
from services.runtime import RuntimeServices
from state.models import (
    ControlDraftState,
    PlaybackState,
    RefreshState,
    RequestPlanState,
)


def register_live_core_callbacks(app: Dash, services: RuntimeServices) -> None:
    """Register the commit + render-poll callbacks for server-side live collection."""

    @app.callback(
        Output("refresh-state-store", "data"),
        Output("controls-status", "children", allow_duplicate=True),
        Input("fetch-live-button", "n_clicks"),
        Input("auto-refresh-selector", "value"),
        State("request-plan-store", "data"),
        State("control-draft-store", "data"),
        State("refresh-state-store", "data"),
        prevent_initial_call=True,
    )
    def commit_live_selection(
        _manual_fetch_clicks: int,
        auto_refresh_value: str,
        request_plan_data: dict | None,
        control_draft_data: dict | None,
        refresh_state_data: dict | None,
    ):
        """Commit the selection to the server collector. Fetch button → commit the current request
        set + queue an immediate wave; auto-refresh change → toggle continuous on/off + cadence on the
        already-committed set (its set only changes on the next Fetch click, matching the old
        last-successful-set semantic)."""
        refresh_state = RefreshState.from_dict(refresh_state_data)
        if services.live_fetch_service is None:
            message = "Missing CLIENT_ID or ACCESS_TOKEN."
            next_refresh_state = RefreshState(
                auto_refresh_enabled=refresh_state.auto_refresh_enabled,
                last_live_request_specs=refresh_state.last_live_request_specs,
                last_strike_range=refresh_state.last_strike_range,
                last_fetch_timestamp=refresh_state.last_fetch_timestamp,
                status_text=message,
            )
            return next_refresh_state.to_dict(), message

        trigger = ctx.triggered_id
        collector = get_or_create_live_collector_state(services)
        active = auto_refresh_value != "off"
        cadence_ms = int(auto_refresh_value) if active else _DEFAULT_CADENCE_MS

        if trigger == "fetch-live-button":
            control_draft = ControlDraftState.from_dict(control_draft_data)
            request_plan = RequestPlanState.from_dict(request_plan_data)
            request_specs = [] if request_plan is None else request_plan.request_specs
            if not request_specs:
                message = "No live request set available."
                next_refresh_state = RefreshState(
                    auto_refresh_enabled=active,
                    last_live_request_specs=refresh_state.last_live_request_specs,
                    last_strike_range=refresh_state.last_strike_range,
                    last_fetch_timestamp=refresh_state.last_fetch_timestamp,
                    status_text=message,
                )
                return next_refresh_state.to_dict(), message

            with collector.lock:
                collector.request_specs = list(request_specs)
                collector.cadence_ms = cadence_ms
                collector.active = active
                collector.pending_one_shot = True
            collector.wake.set()

            message = f"Committed {len(request_specs)} request(s)" + (" (auto-refresh on)." if active else ".")
            next_refresh_state = RefreshState(
                auto_refresh_enabled=active,
                last_live_request_specs=[spec.to_dict() for spec in request_specs],
                last_strike_range=control_draft.strike_range,
                last_fetch_timestamp=datetime.now(IST).replace(tzinfo=None).isoformat(),
                status_text=message,
            )
            return next_refresh_state.to_dict(), message

        # auto-refresh-selector change — retarget continuous on/off + cadence on the committed set.
        with collector.lock:
            collector.active = active
            collector.cadence_ms = cadence_ms
            has_specs = bool(collector.request_specs)
        collector.wake.set()
        if active:
            message = "Auto-refresh on." if has_specs else "Auto-refresh on — fetch a selection to start."
        else:
            message = "Auto-refresh off."
        next_refresh_state = RefreshState(
            auto_refresh_enabled=active,
            last_live_request_specs=refresh_state.last_live_request_specs,
            last_strike_range=refresh_state.last_strike_range,
            last_fetch_timestamp=refresh_state.last_fetch_timestamp,
            status_text=message,
        )
        return next_refresh_state.to_dict(), message

    @app.callback(
        Output("live-loaded-report-meta-store", "data"),
        Output("live-loaded-report-rows-store", "data"),
        Output("loaded-report-meta-store", "data"),
        Output("loaded-report-rows-store", "data"),
        Output("playback-state-store", "data", allow_duplicate=True),
        Output("last-rendered-group-store", "data"),
        Output("vix-heartbeat-store", "data"),
        Input("live-render-interval", "n_intervals"),
        State("last-rendered-group-store", "data"),
        State("playback-state-store", "data"),
        prevent_initial_call=True,
    )
    def render_live_data(
        _ticks: int,
        last_rendered_data: dict | None,
        playback_state_data: dict | None,
    ):
        """Render poll: rebuild + write the stores only when the collector has a NEWER fetch group
        than this tab last rendered (O(1) in-memory check → cheap no-op otherwise). Mirrors the old
        fetch_live_data store-writing tail, minus the fetch (the server thread does that)."""
        collector = get_or_create_live_collector_state(services)
        with collector.lock:
            group_id = collector.last_fetch_group_id
            vix_ts = collector.vix_last_ts
        if not group_id:
            return (no_update,) * 7

        previous = last_rendered_data or {}
        if previous.get("group_id") == group_id:
            # Nothing new since the last repaint — no DB read, no store writes.
            return (no_update,) * 7

        # Rebuild from the stored group via the proven playback inflate path. source="live" is
        # MANDATORY — the live-only UI (alerts, change feed, required-move + metric drawers) gates on
        # source=="live"; "playback" would blank them.
        loaded_state = services.historical_replay_service.load_fetch_group(group_id, source="live")
        if loaded_state is None:
            return (no_update,) * 7

        playback_state = PlaybackState.from_dict(playback_state_data)
        live_meta = loaded_state.to_meta_dict()
        live_rows = loaded_state.rows_payload()
        # Hold the visible (loaded) stores when the user is detached in playback, so the live snapshot
        # keeps refreshing behind the drawer; surface the count of newer live moments.
        should_hold_display = playback_state.detached_from_live
        loaded_meta = no_update if should_hold_display else live_meta
        loaded_rows = no_update if should_hold_display else live_rows
        next_playback_state = no_update
        if should_hold_display:
            next_playback_state = PlaybackState(
                selected_instrument_key=playback_state.selected_instrument_key,
                selected_expiry=playback_state.selected_expiry,
                selected_date=playback_state.selected_date,
                selected_moment_token=playback_state.selected_moment_token,
                detached_from_live=True,
                available_moments=playback_state.available_moments,
                newer_live_count=playback_state.newer_live_count + 1,
            ).to_dict()

        # Bump the VIX heartbeat only when the collector fetched a newer VIX (so the indicator
        # re-checks vix_status() then, not on every wave).
        vix_heartbeat = no_update
        if vix_ts and vix_ts != previous.get("vix_ts"):
            vix_heartbeat = {"source": "collector", "ts": vix_ts}

        return (
            live_meta,
            live_rows,
            loaded_meta,
            loaded_rows,
            next_playback_state,
            {"group_id": group_id, "vix_ts": vix_ts},
            vix_heartbeat,
        )
