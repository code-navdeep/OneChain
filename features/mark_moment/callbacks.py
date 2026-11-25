"""Mark This Moment + bookmark-jump callbacks."""

from __future__ import annotations

from dash import Dash, Input, Output, State, no_update

from services.runtime import RuntimeServices
from state.builders import build_loaded_report_state
from state.models import ActiveViewContext, LoadedReportState, PlaybackState


def _parse_bookmark_token(token: str) -> tuple[str, int | None, str | None, str | None]:
    parts = token.rsplit("|", 3)
    if len(parts) == 4:
        fetch_group_id, asset_text, segment_code, expiry = parts
        try:
            return fetch_group_id, int(asset_text), segment_code, expiry
        except ValueError:
            return token, None, None, None
    return token, None, None, None


def _valid_playback_tokens(
    services: RuntimeServices,
    instrument_key: str | None,
    expiry: str | None,
) -> set[str]:
    """Tokens still present in the live playback catalog for this instrument/expiry."""
    if not instrument_key or not expiry:
        return set()
    replay = services.historical_replay_service
    tokens: set[str] = set()
    for date in replay.list_playback_dates(instrument_key=instrument_key, expiry=expiry):
        for moment in replay.list_playback_moments(date, instrument_key=instrument_key, expiry=expiry):
            tokens.add(moment.token)
    return tokens


def register_mark_moment_callbacks(app: Dash, services: RuntimeServices) -> None:
    """Register the Mark save + bookmark-jump callbacks."""

    @app.callback(
        Output("mark-save-status", "children"),
        Output("mark-name-input", "value"),
        Input("mark-save-button", "n_clicks"),
        State("loaded-report-meta-store", "data"),
        State("active-view-context-store", "data"),
        State("mark-name-input", "value"),
        prevent_initial_call=True,
    )
    def save_current_view(
        _clicks: int,
        loaded_state_data: dict | None,
        active_view_data: dict | None,
        label: str | None,
    ):
        # The store starts empty (nothing fetched yet) → None, which the service answers with its
        # "No loaded state available to save." status instead of a callback error.
        loaded_state = LoadedReportState.from_dict(loaded_state_data) if loaded_state_data else None
        active_view = ActiveViewContext.from_dict(active_view_data)
        clean_label = (label or "").strip() or None
        result = services.mark_moment_service.save_loaded_state(loaded_state, active_view, clean_label)
        return result.status_text, ("" if result.save_count else no_update)

    @app.callback(
        Output("playback-bookmark-jump-selector", "data"),
        Input("playback-asset-selector", "value"),
        Input("playback-expiry-selector", "value"),
        Input("mark-save-status", "children"),
        prevent_initial_call=False,
    )
    def populate_bookmark_jump(
        instrument_key: str | None,
        expiry: str | None,
        _mark_save_status: str | None,
    ):
        if not instrument_key or not expiry:
            return []
        try:
            asset_text, segment_code = instrument_key.split("|", 1)
            asset_id = int(asset_text)
        except (TypeError, ValueError):
            return []

        valid_tokens = _valid_playback_tokens(services, instrument_key, expiry)
        rows = services.mark_moment_service.list_saved_moments(asset_id, segment_code, expiry)
        options: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            fetch_group_id = row.get("fetch_group_id")
            if not fetch_group_id:
                continue
            token = f"{fetch_group_id}|{int(row['underlying_asset_id'])}|{row['segment_code']}|{row['expiry']}"
            if token not in valid_tokens or token in seen:
                continue
            seen.add(token)
            options.append({"label": row.get("display_label") or str(row["timestamp"]), "value": token})
        return options

    @app.callback(
        Output("loaded-report-meta-store", "data", allow_duplicate=True),
        Output("loaded-report-rows-store", "data", allow_duplicate=True),
        Output("playback-state-store", "data", allow_duplicate=True),
        Output("main-tabs", "active_tab", allow_duplicate=True),
        Output("playback-status", "children", allow_duplicate=True),
        Input("playback-bookmark-jump-selector", "value"),
        State("playback-state-store", "data"),
        prevent_initial_call=True,
    )
    def jump_to_bookmark(token: str | None, playback_state_data: dict | None):
        if not token:
            return no_update, no_update, no_update, no_update, no_update

        fetch_group_id, asset_id, segment_code, expiry = _parse_bookmark_token(token)
        if asset_id is None or segment_code is None or expiry is None:
            return no_update, no_update, no_update, no_update, "Could not parse bookmark token."

        loaded_pair = services.historical_replay_service.load_pair_from_fetch_group(
            fetch_group_id,
            asset_id=asset_id,
            segment_code=segment_code,
            expiry=expiry,
        )
        if loaded_pair is None:
            return no_update, no_update, no_update, no_update, "That bookmark could not be reconstructed."

        loaded_state = build_loaded_report_state(
            source="playback",
            request_specs=[loaded_pair.request],
            fetch_group_id=fetch_group_id,
            pair_reports={loaded_pair.request.pair_key.value: loaded_pair.report},
            status_text="Loaded bookmark.",
        )
        playback_state = PlaybackState.from_dict(playback_state_data)
        next_state = PlaybackState(
            selected_instrument_key=playback_state.selected_instrument_key,
            selected_expiry=playback_state.selected_expiry,
            selected_date=playback_state.selected_date,
            selected_moment_token=token,
            detached_from_live=True,
            available_moments=playback_state.available_moments,
            newer_live_count=0,
        )
        return loaded_state.to_meta_dict(), loaded_state.rows_payload(), next_state.to_dict(), "chain-tab", loaded_state.status_text
