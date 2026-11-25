"""Callbacks for the controls feature."""

from __future__ import annotations

from dash import Dash, Input, Output, State, ctx, html, no_update

from adapters.expiry_cache import read_cached_expiries
from config import AppConfig
from features.controls.helpers import (
    build_basket_remove_options,
    build_basket_summary,
    build_expiry_options,
    build_request_spec,
)
from services.runtime import RuntimeServices
from state.builders import build_request_plan_state
from state.models import ControlDraftState


def register_controls_callbacks(
    app: Dash,
    config: AppConfig,
    services: RuntimeServices,
) -> None:
    """Register the control-surface callbacks."""
    # Shared runtime instances — NOT locally constructed: the api_service must share the one
    # KeyedRequestScheduler with the live collector so a ⟳ expiry refresh is paced against live
    # waves instead of hitting Dhan from an independent, unaware client.
    asset_repository = services.asset_repository
    expiry_cache_path = config.expiry_cache_path
    api_service = services.api_service

    @app.callback(
        Output("single-expiry-selector", "data"),
        Output("single-expiry-selector", "value"),
        Output("multi-expiry-selector", "data"),
        Output("multi-expiry-selector", "value"),
        Input("asset-selector", "value"),
        State("single-expiry-selector", "value"),
        State("multi-expiry-selector", "value"),
    )
    def update_expiry_options(
        asset_value: str | None,
        current_single_expiry: str | None,
        current_multi_expiries: list[str] | None,
    ):
        if not asset_value:
            return [], None, [], []

        asset = asset_repository.get_asset_by_instrument_key(asset_value)
        if asset is None:
            return [], None, [], []

        expiry_list = read_cached_expiries(expiry_cache_path, asset.asset_id, asset.segment_code)
        if not expiry_list and api_service is not None:
            expiry_list = api_service.get_expiry_list(asset.asset_id, asset.segment_code, use_cache=True)
        options = build_expiry_options(expiry_list)
        allowed_values = {item["value"] for item in options}

        single_value = current_single_expiry if current_single_expiry in allowed_values else None
        if single_value is None and options:
            single_value = options[0]["value"]

        multi_value = [item for item in (current_multi_expiries or []) if item in allowed_values]
        if not multi_value and single_value:
            multi_value = [single_value]

        return options, single_value, options, multi_value

    @app.callback(
        Output("single-expiry-selector", "data", allow_duplicate=True),
        Output("single-expiry-selector", "value", allow_duplicate=True),
        Output("multi-expiry-selector", "data", allow_duplicate=True),
        Output("multi-expiry-selector", "value", allow_duplicate=True),
        Input("refresh-expiries-button", "n_clicks"),
        Input("refresh-expiries-button-single", "n_clicks"),
        State("asset-selector", "value"),
        State("single-expiry-selector", "value"),
        State("multi-expiry-selector", "value"),
        prevent_initial_call=True,
    )
    def refresh_expiry_options(
        _n_clicks_multi: int,
        _n_clicks_single: int,
        asset_value: str | None,
        current_single_expiry: str | None,
        current_multi_expiries: list[str] | None,
    ):
        """Force-fetch the expiry list from the API (bypassing the JSON cache).

        Fired by the ⟳ button in either mode (`refresh-expiries-button` beside the multi-expiry
        selector, `refresh-expiries-button-single` beside the single-expiry selector); both do the
        same force-refetch. `get_expiry_list(use_cache=False)` re-POSTs and rewrites
        `expiry_cache.json`, so this refreshes both dropdowns and the on-disk cache. A failed/empty
        fetch leaves the current options untouched (never clobbers good expiries).
        """
        if not asset_value or api_service is None:
            return no_update, no_update, no_update, no_update

        asset = asset_repository.get_asset_by_instrument_key(asset_value)
        if asset is None:
            return no_update, no_update, no_update, no_update

        expiry_list = api_service.get_expiry_list(asset.asset_id, asset.segment_code, use_cache=False)
        if not expiry_list:
            return no_update, no_update, no_update, no_update

        options = build_expiry_options(expiry_list)
        allowed_values = {item["value"] for item in options}

        single_value = current_single_expiry if current_single_expiry in allowed_values else None
        if single_value is None and options:
            single_value = options[0]["value"]

        multi_value = [item for item in (current_multi_expiries or []) if item in allowed_values]
        if not multi_value and single_value:
            multi_value = [single_value]

        return options, single_value, options, multi_value

    @app.callback(
        Output("controls-single-expiry-wrapper", "style"),
        Output("controls-multi-expiry-wrapper", "style"),
        Output("controls-basket-wrapper", "style"),
        Input("request-mode-selector", "value"),
    )
    def configure_request_mode(request_mode: str):
        # Wrapper visibility: single-expiry, multi-expiry, basket. (Aggregate lives on the tab bar
        # now; Playback is a bottom drawer — neither is a builder wrapper anymore.)
        if request_mode == "multi_instrument":
            # Multi-instrument: the single-expiry dropdown feeds "Add Request"; basket shown.
            return {"display": "block"}, {"display": "none"}, {"display": "block"}
        # single_instrument: one multi-select expiry dropdown (1 selected = single, N = multi-expiry).
        return {"display": "none"}, {"display": "block"}, {"display": "none"}

    @app.callback(
        Output("aggregate-mode-selector", "disabled"),
        Input("request-mode-selector", "value"),
        Input("multi-expiry-selector", "value"),
    )
    def gate_aggregate_toggle(request_mode: str, multi_expiries: list | None):
        # Aggregate only makes sense for one instrument across >1 expiry — deactivate the tab-bar
        # switch otherwise.
        eligible = request_mode == "single_instrument" and len(multi_expiries or []) > 1
        return not eligible

    @app.callback(
        Output("control-draft-store", "data"),
        Output("request-plan-store", "data"),
        Output("controls-basket-summary", "children"),
        Output("controls-basket-remove-selector", "data"),
        Output("controls-basket-remove-selector", "value"),
        Output("controls-status", "children"),
        Input("request-mode-selector", "value"),
        Input("asset-selector", "value"),
        Input("single-expiry-selector", "value"),
        Input("multi-expiry-selector", "value"),
        Input("strike-range-selector", "value"),
        Input("auto-refresh-selector", "value"),
        Input("aggregate-mode-selector", "value"),
        Input("controls-add-request-button", "n_clicks"),
        Input("controls-remove-request-button", "n_clicks"),
        Input("controls-clear-basket-button", "n_clicks"),
        State("controls-basket-remove-selector", "value"),
        State("control-draft-store", "data"),
        prevent_initial_call=False,
    )
    def reduce_control_draft(
        request_mode: str,
        asset_value: str | None,
        single_expiry: str | None,
        multi_expiries: list[str] | None,
        strike_range: str,
        auto_refresh: str,
        aggregate_mode: str,
        _add_clicks: int,
        _remove_clicks: int,
        _clear_clicks: int,
        remove_pair_key: str | None,
        existing_draft: dict | None,
    ):
        draft = ControlDraftState.from_dict(existing_draft)
        trigger = ctx.triggered_id
        effective_request_mode = request_mode
        basket = [dict(item) for item in draft.request_basket]

        if trigger == "controls-clear-basket-button":
            basket = []
            status = "Basket cleared."
        elif trigger == "controls-remove-request-button" and remove_pair_key:
            basket = [item for item in basket if item.get("pair_key") != remove_pair_key]
            status = "Removed selected basket item."
        elif trigger == "controls-add-request-button":
            asset = asset_repository.get_asset_by_instrument_key(asset_value) if asset_value else None
            if asset is None or not single_expiry:
                status = "Select an asset and expiry before adding a request."
            else:
                request_spec = build_request_spec(asset, single_expiry)
                instrument_key = request_spec.instrument_key.value
                # The Dhan API cannot fetch multiple expiries of multiple instruments in one
                # wave — only one expiry per instrument when mixing instruments. So the basket
                # allows at most one entry per instrument; reject a second expiry of the same.
                existing = next(
                    (item for item in basket if item.get("instrument_key") == instrument_key),
                    None,
                )
                if existing is not None:
                    status = (
                        f"{request_spec.display_name} is already in the basket — "
                        "remove it first to change its expiry."
                    )
                else:
                    basket.append(request_spec.to_dict())
                    status = "Added request to basket."
        else:
            status = "Draft state updated."

        if effective_request_mode == "single_instrument":
            # Single-instrument uses the multi-select expiry dropdown
            # (1 selected = single expiry, N selected = multi-expiry).
            selected_expiries = [item for item in (multi_expiries or []) if item]
        else:
            # multi_instrument is basket-driven; single_expiry feeds "Add Request".
            selected_expiries = [single_expiry] if single_expiry else []

        next_state = ControlDraftState(
            request_mode=effective_request_mode,
            asset_value=asset_value,
            selected_expiries=selected_expiries,
            request_basket=basket,
            strike_range=strike_range,
            toggles={
                "auto_refresh": auto_refresh != "off",
                "aggregate_mode": (
                    effective_request_mode == "single_instrument"
                    and len(selected_expiries) > 1
                    and bool(aggregate_mode)
                ),
            },
        )
        request_plan_state = build_request_plan_state(next_state, asset_repository)

        remove_options = build_basket_remove_options(next_state.request_basket)
        valid_remove_values = {item["value"] for item in remove_options}
        next_remove_value = remove_pair_key if remove_pair_key in valid_remove_values else None

        summary = build_basket_summary(next_state.request_basket)
        if effective_request_mode != "multi_instrument":
            summary = html.Div("Basket builder is used only in multi-instrument mode.")
            remove_options = []
            next_remove_value = None

        return (
            next_state.to_dict(),
            request_plan_state.to_dict(),
            summary,
            remove_options,
            next_remove_value,
            status,
        )
