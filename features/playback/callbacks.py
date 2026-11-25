"""Playback callbacks."""

from __future__ import annotations

from dash import Dash, Input, Output, State, ctx, no_update

from features.playback.helpers import build_bookmark_marks, build_half_hour_marks
from services.runtime import RuntimeServices
from state.builders import build_loaded_report_state
from state.models import PlaybackState
from ui.formatters import format_expiry_label_short, format_playback_date_label


def _parse_playback_token(token: str) -> tuple[str, int | None, str | None, str | None]:
    parts = token.rsplit("|", 3)
    if len(parts) == 4:
        fetch_group_id, asset_text, segment_code, expiry = parts
        try:
            return fetch_group_id, int(asset_text), segment_code, expiry
        except ValueError:
            return token, None, None, None
    return token, None, None, None


def _bookmarked_fetch_group_ids(
    services: RuntimeServices,
    instrument_key: str | None,
    expiry: str | None,
) -> set[str]:
    if not instrument_key or not expiry:
        return set()
    try:
        asset_text, segment_code = instrument_key.split("|", 1)
        asset_id = int(asset_text)
    except (TypeError, ValueError):
        return set()
    rows = services.mark_moment_service.list_saved_moments(asset_id, segment_code, expiry)
    return {str(row["fetch_group_id"]) for row in rows if row.get("fetch_group_id")}


def register_playback_callbacks(app: Dash, services: RuntimeServices) -> None:
    """Register playback browse and load callbacks."""

    @app.callback(
        Output("playback-asset-selector", "data"),
        Output("playback-asset-selector", "value"),
        Output("playback-expiry-selector", "data"),
        Output("playback-expiry-selector", "value"),
        Output("playback-date-selector", "data"),
        Output("playback-date-selector", "value"),
        Output("playback-moment-selector", "data"),
        Output("playback-moment-selector", "value"),
        Output("playback-moment-slider", "max"),
        Output("playback-moment-slider", "marks"),
        Output("playback-moment-slider", "value"),
        Output("playback-state-store", "data"),
        Output("playback-current-label", "children"),
        Output("playback-live-indicator", "children"),
        Output("playback-status", "children"),
        Input("playback-drawer", "is_open"),
        Input("playback-asset-selector", "value"),
        Input("playback-expiry-selector", "value"),
        Input("playback-date-selector", "value"),
        Input("playback-moment-selector", "value"),
        Input("live-loaded-report-meta-store", "data"),
        Input("mark-save-status", "children"),
        State("playback-state-store", "data"),
        prevent_initial_call=False,
    )
    def sync_playback_browse(
        drawer_open: bool,
        selected_instrument_key: str | None,
        selected_expiry: str | None,
        selected_date: str | None,
        selected_moment_token: str | None,
        _latest_live_state: dict | None,
        _mark_save_status: str | None,
        playback_state_data: dict | None,
    ):
        # Visibility gate: do nothing while the drawer is closed (every live tick triggers this via
        # live-loaded-report-meta-store). is_open is an Input, so opening the drawer re-fires this
        # and the dropdowns populate on demand — the same lazy pattern the bottom-panel charts use.
        if not drawer_open:
            return (no_update,) * 15
        playback_state = PlaybackState.from_dict(playback_state_data)

        asset_options = services.historical_replay_service.list_playback_assets()
        valid_assets = {item["value"] for item in asset_options}
        resolved_instrument_key = (
            selected_instrument_key
            if selected_instrument_key in valid_assets
            else (
                playback_state.selected_instrument_key
                if playback_state.selected_instrument_key in valid_assets
                else (asset_options[0]["value"] if asset_options else None)
            )
        )

        expiry_values = services.historical_replay_service.list_playback_expiries(resolved_instrument_key)
        expiry_options = [{"label": format_expiry_label_short(item), "value": item} for item in expiry_values]
        valid_expiries = {item["value"] for item in expiry_options}
        resolved_expiry = (
            selected_expiry
            if selected_expiry in valid_expiries
            else (
                playback_state.selected_expiry
                if playback_state.selected_expiry in valid_expiries
                else (expiry_options[0]["value"] if expiry_options else None)
            )
        )

        available_dates = services.historical_replay_service.list_playback_dates(
            instrument_key=resolved_instrument_key,
            expiry=resolved_expiry,
        )
        date_options = [{"label": format_playback_date_label(item), "value": item} for item in available_dates]
        resolved_date = selected_date if selected_date in available_dates else (available_dates[0] if available_dates else None)

        available_moments = services.historical_replay_service.list_playback_moments(
            resolved_date,
            instrument_key=resolved_instrument_key,
            expiry=resolved_expiry,
        )
        moment_options = [{"label": item.label, "value": item.token} for item in available_moments]
        available_tokens = {item.token for item in available_moments}
        preferred_token = selected_moment_token or playback_state.selected_moment_token
        resolved_token = (
            preferred_token
            if preferred_token in available_tokens
            # Moments are now oldest-first, so the LATEST is [-1] — default to it (scrubber starts at
            # the right; drag left to rewind).
            else (available_moments[-1].token if available_moments else None)
        )

        next_state = PlaybackState(
            selected_instrument_key=resolved_instrument_key,
            selected_expiry=resolved_expiry,
            selected_date=resolved_date,
            selected_moment_token=resolved_token,
            detached_from_live=playback_state.detached_from_live,
            available_moments=available_moments,
            newer_live_count=playback_state.newer_live_count,
        )
        # Bare ticks at every moment (so step=None scrubbing can stop on each); then half-hour time
        # labels (9:30 → 3:30) for a visual sense of position; then the gold ★ on bookmarked moments
        # (which composes on top — a bookmarked half-hour reads "★ 9:30").
        slider_marks = {index: {"label": ""} for index in range(len(available_moments))}
        slider_marks = build_half_hour_marks(available_moments, slider_marks)
        slider_marks = build_bookmark_marks(
            available_moments,
            _bookmarked_fetch_group_ids(services, resolved_instrument_key, resolved_expiry),
            slider_marks,
        )
        slider_value = next(
            (index for index, item in enumerate(available_moments) if item.token == resolved_token),
            0,
        )
        current_label = (
            next((item.label for item in available_moments if item.token == resolved_token), "No playback moment selected.")
            if available_moments
            else "No playback moment selected."
        )
        live_indicator = (
            f"{playback_state.newer_live_count} newer live moment(s) available."
            if playback_state.detached_from_live and playback_state.newer_live_count > 0
            else "Playback is pinned to the selected stored moment."
        )
        status = (
            f"{len(available_moments)} stored moment(s) available."
            if resolved_date
            else "No stored playback dates yet."
        )
        return (
            asset_options,
            resolved_instrument_key,
            expiry_options,
            resolved_expiry,
            date_options,
            resolved_date,
            moment_options,
            resolved_token,
            max(0, len(available_moments) - 1),
            slider_marks,
            slider_value,
            next_state.to_dict(),
            current_label,
            live_indicator,
            status,
        )

    @app.callback(
        Output("playback-moment-selector", "value", allow_duplicate=True),
        Input("playback-prev-button", "n_clicks"),
        Input("playback-next-button", "n_clicks"),
        Input("playback-moment-slider", "value"),
        State("playback-moment-selector", "data"),
        State("playback-moment-selector", "value"),
        prevent_initial_call=True,
    )
    def step_playback_selection(
        _prev_clicks: int,
        _next_clicks: int,
        slider_value: int | None,
        moment_options: list[dict] | None,
        current_token: str | None,
    ):
        options = list(moment_options or [])
        if not options:
            return no_update

        token_order = [str(item["value"]) for item in options]
        current_index = token_order.index(current_token) if current_token in token_order else 0
        trigger = ctx.triggered_id
        if trigger == "playback-prev-button":
            next_index = max(0, current_index - 1)
        elif trigger == "playback-next-button":
            next_index = min(len(token_order) - 1, current_index + 1)
        elif trigger == "playback-moment-slider" and slider_value is not None:
            next_index = max(0, min(int(slider_value), len(token_order) - 1))
        else:
            return no_update
        return token_order[next_index]

    @app.callback(
        Output("loaded-report-meta-store", "data", allow_duplicate=True),
        Output("loaded-report-rows-store", "data", allow_duplicate=True),
        Output("playback-state-store", "data", allow_duplicate=True),
        Output("main-tabs", "active_tab", allow_duplicate=True),
        Output("playback-status", "children", allow_duplicate=True),
        Input("playback-load-button", "n_clicks"),
        Input("playback-back-button", "n_clicks"),
        Input("playback-moment-selector", "value"),
        State("playback-date-selector", "value"),
        State("playback-asset-selector", "value"),
        State("playback-expiry-selector", "value"),
        State("playback-state-store", "data"),
        State("live-loaded-report-meta-store", "data"),
        State("live-loaded-report-rows-store", "data"),
        prevent_initial_call=True,
    )
    def load_or_exit_playback(
        _load_clicks: int,
        _back_clicks: int,
        selected_moment_token: str | None,
        selected_date: str | None,
        selected_instrument_key: str | None,
        selected_expiry: str | None,
        playback_state_data: dict | None,
        live_loaded_meta_data: dict | None,
        live_loaded_rows_data: dict | None,
    ):
        trigger = ctx.triggered_id
        playback_state = PlaybackState.from_dict(playback_state_data)

        # Back to Live is ONLY the explicit button. Closing the drawer (arrow / X) just hides it and
        # leaves the loaded moment on screen.
        if trigger == "playback-back-button":
            if not live_loaded_meta_data:
                return no_update, no_update, playback_state.to_dict(), no_update, "No live state is available to restore."
            next_state = PlaybackState(
                selected_instrument_key=playback_state.selected_instrument_key,
                selected_expiry=playback_state.selected_expiry,
                selected_date=playback_state.selected_date,
                selected_moment_token=playback_state.selected_moment_token,
                detached_from_live=False,
                available_moments=playback_state.available_moments,
                newer_live_count=0,
            )
            # Restore both halves of the live snapshot into the loaded stores.
            return live_loaded_meta_data, live_loaded_rows_data, next_state.to_dict(), no_update, "Returned to latest live state."

        if trigger == "playback-moment-selector" and not playback_state.detached_from_live:
            return no_update, no_update, playback_state.to_dict(), no_update, "Playback selection updated. Click Load to enter playback."

        if not selected_date or not selected_moment_token:
            return no_update, no_update, playback_state.to_dict(), no_update, "Select an asset, expiry, date, and time first."

        fetch_group_id, asset_id, segment_code, expiry = _parse_playback_token(selected_moment_token)
        if asset_id is None or segment_code is None or expiry is None:
            return no_update, no_update, playback_state.to_dict(), no_update, "Could not parse playback token. Please re-select a moment."
        else:
            loaded_pair = services.historical_replay_service.load_pair_from_fetch_group(
                fetch_group_id,
                asset_id=asset_id,
                segment_code=segment_code,
                expiry=expiry,
            )
            if loaded_pair is None:
                loaded_state = None
            else:
                loaded_state = build_loaded_report_state(
                    source="playback",
                    request_specs=[loaded_pair.request],
                    fetch_group_id=fetch_group_id,
                    pair_reports={loaded_pair.request.pair_key.value: loaded_pair.report},
                    status_text="Loaded 1 pair from stored playback moment.",
                )

        if loaded_state is None:
            return no_update, no_update, playback_state.to_dict(), no_update, "That playback moment could not be reconstructed."

        next_state = PlaybackState(
            selected_instrument_key=selected_instrument_key,
            selected_expiry=selected_expiry,
            selected_date=selected_date,
            selected_moment_token=selected_moment_token,
            detached_from_live=True,
            available_moments=playback_state.available_moments,
            newer_live_count=0,
        )
        return loaded_state.to_meta_dict(), loaded_state.rows_payload(), next_state.to_dict(), no_update, loaded_state.status_text
