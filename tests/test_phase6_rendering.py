from __future__ import annotations

from pathlib import Path

from analytics.option_chain import build_enhanced_option_rows, parse_option_chain_response, stamp_option_rows
from domain.requests import RequestSpec
from features.bottom_panels.rendering import build_bottom_panels
from repositories.market_data_repository import MarketDataRepository
from repositories.snapshot_repository import SnapshotRepository
from services.view_data_service import ViewDataService
from state.builders import build_loaded_report_state


def sample_option_chain_payload() -> dict:
    return {
        "last_price": 22500,
        "oc": {
            "22400": {
                "ce": {
                    "last_price": 120,
                    "oi": 1000,
                    "previous_oi": 900,
                    "volume": 500,
                    "implied_volatility": 14.2,
                    "greeks": {"delta": 0.51, "gamma": 0.01, "theta": -4.5, "vega": 7.1},
                    "top_bid_price": 119,
                    "top_ask_price": 121,
                },
                "pe": {
                    "last_price": 110,
                    "oi": 950,
                    "previous_oi": 925,
                    "volume": 430,
                    "implied_volatility": 15.4,
                    "greeks": {"delta": -0.49, "gamma": 0.011, "theta": -4.1, "vega": 6.8},
                    "top_bid_price": 109,
                    "top_ask_price": 111,
                },
            }
        },
    }


def _nifty_request(expiry: str = "2026-04-30") -> RequestSpec:
    return RequestSpec(
        asset_id=13,
        segment_code="IDX_I",
        expiry=expiry,
        display_name="Nifty",
        lot_size=65,
        strike_size=50,
    )


def make_view_data_service(tmp_path: Path) -> ViewDataService:
    market_repository = MarketDataRepository(tmp_path / "market.db")
    snapshot_repository = SnapshotRepository(tmp_path / "snapshot.db")
    stamped = stamp_option_rows(
        parse_option_chain_response(sample_option_chain_payload(), 13, "IDX_I", "2026-04-30"),
        "group-1",
        timestamp="2026-04-10T03:45:00+00:00",
    )
    market_repository.insert_option_rows(
        build_enhanced_option_rows(stamped, request_spec=_nifty_request(), risk_free_rate=0.065, market_volatility=0.15)
    )
    snapshot_repository.save_historical_metric(
        {
            "date": "2026-04-10",
            "underlying_asset_id": 13,
            "segment_code": "IDX_I",
            "rv_22d": 0.2,
            "call_iv_rv_spread_22d": 0.03,
            "put_iv_rv_spread_22d": 0.01,
        }
    )
    return ViewDataService(market_data_repository=market_repository, snapshot_repository=snapshot_repository)


def make_loaded_state() -> object:
    request_specs = [
        RequestSpec(
            asset_id=13,
            segment_code="IDX_I",
            expiry="2026-04-30",
            display_name="Nifty",
            lot_size=65,
            strike_size=50,
        ),
        RequestSpec(
            asset_id=13,
            segment_code="IDX_I",
            expiry="2026-05-07",
            display_name="Nifty",
            lot_size=65,
            strike_size=50,
        ),
    ]
    pair_reports = {
        request_specs[0].pair_key.value: {
            "pair_key": request_specs[0].pair_key.value,
            "display_name": "Nifty",
            "expiry": "2026-04-30",
            "underlying_price": 22500,
            "strike_size": 50,
            "totals": {
                "total_oi": 1950,
                "total_volume": 930,
                "put_call_ratio": 0.95,
                "total_gamma_exposure": 82000,
            },
            "levels": {"max_call_oi_strike": 22400, "max_put_oi_strike": 22400},
            "rows": build_enhanced_option_rows(
                stamp_option_rows(
                    parse_option_chain_response(sample_option_chain_payload(), 13, "IDX_I", "2026-04-30"),
                    "group-1",
                    timestamp="2026-04-10T03:45:00+00:00",
                ),
                request_spec=_nifty_request(),
                risk_free_rate=0.065,
                market_volatility=0.15,
            ),
        },
        request_specs[1].pair_key.value: {
            "pair_key": request_specs[1].pair_key.value,
            "display_name": "Nifty",
            "expiry": "2026-05-07",
            "underlying_price": 22520,
            "strike_size": 50,
            "totals": {
                "total_oi": 1800,
                "total_volume": 880,
                "put_call_ratio": 0.9,
                "total_gamma_exposure": 74000,
            },
            "levels": {"max_call_oi_strike": 22400, "max_put_oi_strike": 22400},
            "rows": [],
        },
    }
    return build_loaded_report_state(
        source="live",
        request_specs=request_specs,
        fetch_group_id="group-1",
        pair_reports=pair_reports,
        status_text="Fetched 2 of 2 request(s)",
    )


def test_chain_strike_range_is_display_only(tmp_path: Path):
    from features.chain.rendering import build_chain_tab

    service = make_view_data_service(tmp_path)
    loaded_state = make_loaded_state()
    all_view = repr(build_chain_tab(loaded_state, "13|IDX_I|2026-04-30", "All", service))
    narrow_view = repr(build_chain_tab(loaded_state, "13|IDX_I|2026-04-30", "8", service))
    # The strike range is a display filter only: the ATM strike renders under any window.
    assert "22400" in all_view
    assert "22400" in narrow_view


def test_chain_yesterday_reference_suppressed_in_aggregate_mode():
    from features.chain.table_model import build_chain_table_data

    class _RecordingViewData:
        """Records the yesterday/trophy reads so the aggregate gate is directly observable."""

        def __init__(self):
            self.yesterday_calls: list[tuple] = []
            self.trophy_calls: list[tuple] = []

        def get_yesterday_totals(self, asset_id, segment_code, expiry):
            self.yesterday_calls.append((asset_id, segment_code, expiry))
            return None

        def get_oi_change_trophy_data(self, asset_id, segment_code, expiry):
            self.trophy_calls.append((asset_id, segment_code, expiry))
            return None

    rows = [
        {"option_type": "CE", "strike": 22400.0, "oi_lots": 5.0, "last_price": 100.0,
         "volume_lots": 2.0, "iv": 12.0, "gamma_exposure": 1.0, "previous_oi_lots": 4.0},
        {"option_type": "PE", "strike": 22600.0, "oi_lots": 6.0, "last_price": 90.0,
         "volume_lots": 2.0, "iv": 13.0, "gamma_exposure": -1.0, "previous_oi_lots": 5.0},
    ]
    recorder = _RecordingViewData()

    # Aggregate report (as resolve_chain_report hands it over, request attached): the EOD snapshot
    # stamps each expiry at its own timestamp, so no honest multi-expiry "yesterday" exists —
    # the reference reads must be SUPPRESSED entirely, never a single-expiry-wrong comparison.
    aggregate_report = {
        "pair_key": "aggregate|13|IDX_I",
        "expiry": "Aggregate",
        "underlying_price": 22500.0,
        "lot_size": 65,
        "aggregate_member_pairs": ["13|IDX_I|2026-04-30", "13|IDX_I|2026-05-07"],
        "request": {"asset_id": 13, "segment_code": "IDX_I", "expiry": "2026-04-30"},
        "rows": rows,
    }
    build_chain_table_data(aggregate_report, view_data_service=recorder)
    assert recorder.yesterday_calls == []
    assert recorder.trophy_calls == []

    # Per-pair report: both reads fire once, with the plain (asset, segment, expiry) identity.
    pair_report = {
        "pair_key": "13|IDX_I|2026-04-30",
        "expiry": "2026-04-30",
        "underlying_price": 22500.0,
        "lot_size": 65,
        "request": {"asset_id": 13, "segment_code": "IDX_I", "expiry": "2026-04-30"},
        "rows": rows,
    }
    build_chain_table_data(pair_report, view_data_service=recorder)
    assert recorder.yesterday_calls == [(13, "IDX_I", "2026-04-30")]
    assert recorder.trophy_calls == [(13, "IDX_I", "2026-04-30")]


def test_chain_gex_column_survives_json_round_trip():
    import json

    from analytics.report_builder import build_pair_report
    from features.chain.table_model import build_table_rows

    request = _nifty_request()
    rows = build_enhanced_option_rows(
        stamp_option_rows(
            parse_option_chain_response(sample_option_chain_payload(), 13, "IDX_I", "2026-04-30"),
            "group-1",
            timestamp="2026-04-10T03:45:00+00:00",
        ),
        request_spec=request,
        risk_free_rate=0.065,
        market_volatility=0.15,
    )
    report = build_pair_report(request, rows)
    assert report["gex_analysis"]["gex_by_strike"]  # float keys in-process

    # The dcc.Store serializes the report to JSON, which stringifies the float keys.
    round_tripped = json.loads(json.dumps(report))
    table_data, _, _ = build_table_rows(round_tripped, request.lot_size)
    gex_cells = [row["strike_gex"] for row in table_data if row.get("strike_gex") not in ("-", None, "")]
    assert gex_cells, "per-strike GEX column is empty after the JSON round-trip"


def test_chain_bs_valuation_border():
    from features.chain.table_model import build_conditional_styles, define_table_columns
    from ui import theme

    report = {
        "underlying_price": 22500,
        "levels": {},
        "max_pain_strike": 0,
        "rows": [
            {"strike": 22500.0, "option_type": "CE", "last_price": 200.0, "bs_price": 100.0},  # +100% → overvalued
            {"strike": 22500.0, "option_type": "PE", "last_price": 50.0, "bs_price": None},      # no BS price → no border
        ],
    }
    styles = build_conditional_styles([{"strike": "22500"}], define_table_columns(False), report, False)
    call_borders = [s for s in styles if s["if"].get("column_id") == "call_last_price" and "border" in s]
    # Strongly overvalued (≥20%) → the stronger tier = 2px SOLID (magnitude is style now, not width).
    assert any(s["border"] == f"2px solid {theme.DOWN}" for s in call_borders)
    put_borders = [s for s in styles if s["if"].get("column_id") == "put_last_price" and "border" in s]
    assert put_borders == []


def test_charts_use_dark_theme_and_palette():
    from features.charts.rendering import _build_bar_figure
    from ui import theme

    figure = _build_bar_figure(
        [{"strike": 22500, "oi_lots": 10}], [{"strike": 22500, "oi_lots": 8}], "oi_lots", "OI"
    )
    assert figure.layout.paper_bgcolor == theme.SURFACE
    assert figure.layout.plot_bgcolor == theme.SURFACE
    colors = [trace.marker.color for trace in figure.data]
    assert theme.PUT in colors and theme.CALL in colors


def test_bottom_panels_renderer_uses_loaded_state_and_view_data(tmp_path: Path):
    service = make_view_data_service(tmp_path)
    loaded_state = make_loaded_state()
    card = build_bottom_panels(loaded_state, "13|IDX_I|2026-04-30", service, vol_window=5)
    rendered = repr(card)
    # Single flat metric row (no group headings / section title).
    assert "GEX Regime" in rendered
    assert "Vol Regime" in rendered
    assert "Delta Skew" in rendered
    assert "Max Vega Strike" in rendered
    assert "Vega Skew" in rendered
    assert "Vol Analytics" in rendered
    # The Price + IV/RV charts moved to the static shell (figures swapped by callbacks), so they are
    # no longer part of build_bottom_panels' output — it renders only the metric rows now.
    assert "bottom-iv-rv-chart-graph" not in rendered
    # 5d snapshot metric exists in the fixture → reconstructed Call IV (rv + spread) appears
    assert "Call IV" in rendered
    # Skew row is always present (shows "—" when the report carries no skew, as here)
    assert "Risk Reversal 25Δ" in rendered
    # Pinning row is always present too (shows "—" when the report carries no pinning)
    assert "Pinning Pressure" in rendered


def test_bottom_panels_skew_row_shows_risk_reversal(tmp_path: Path):
    from analytics.report_builder import build_pair_report
    from state.builders import build_loaded_report_state

    service = make_view_data_service(tmp_path)
    spot = 22500.0
    specs = [
        (22300, "CE", 0.75, 13.0), (22400, "CE", 0.60, 13.5), (22500, "CE", 0.50, 14.0),
        (22600, "CE", 0.30, 14.5), (22700, "CE", 0.15, 15.0),
        (22300, "PE", -0.15, 16.0), (22400, "PE", -0.30, 15.5), (22500, "PE", -0.50, 15.0),
        (22600, "PE", -0.70, 14.5), (22700, "PE", -0.85, 14.0),
    ]
    rows = [
        {
            "strike": float(strike), "option_type": otype, "delta": delta, "iv": iv,
            "gamma": 0.01, "vega": 7.0, "theta": -4.0, "last_price": 100.0,
            "oi_lots": 10.0, "previous_oi_lots": 9.0, "oi_change_lots": 1.0, "volume_lots": 5.0,
            "gamma_exposure": -1.0 if otype == "CE" else 1.0, "volatility_exposure": 70.0,
            "underlying_price": spot, "bs_price": None,
        }
        for strike, otype, delta, iv in specs
    ]
    request = _nifty_request()
    report = build_pair_report(request, rows)
    loaded_state = build_loaded_report_state(
        source="live",
        request_specs=[request],
        fetch_group_id="group-1",
        pair_reports={request.pair_key.value: report},
    )
    rendered = repr(build_bottom_panels(loaded_state, request.pair_key.value, service, vol_window=5))
    assert "Risk Reversal 25Δ" in rendered
    assert "Put skew" in rendered  # rr_25 = -1.0 → put skew
    # Pinning row carries a real pin strike (uniform gamma mass → spot strike wins).
    # The Pin Strike tile shows a whole strike with a thousands separator.
    assert "Pin Strike" in rendered
    assert "22,500" in rendered


def test_bottom_panels_advanced_row_appears_for_matching_instrument(tmp_path: Path):
    service = make_view_data_service(tmp_path)
    loaded_state = make_loaded_state()
    advanced_report = {
        "instrument_key": "13|IDX_I",
        "gex_regime": "Stabilizing",
        "zero_gamma_strike": 22500.0,
        "distance_to_zero_gamma_pts": 120.0,
        "distance_to_zero_gamma_pct": 0.53,
        "captured_at": "2026-04-10T14:32:05",
        "expiry_metrics": [
            {"expiry_label": "30 Apr", "atm_call_iv": 14.2, "atm_put_iv": 15.4},
        ],
    }
    card = build_bottom_panels(
        loaded_state,
        "13|IDX_I|2026-04-30",
        service,
        vol_window=5,
        advanced_report_data=advanced_report,
    )
    rendered = repr(card)
    # As-of timestamp in the title; relabeled multi-expiry regime; distance tile; on-theme IV tiles.
    assert "Advanced Report — 14:32 IST" in rendered
    assert "Multi-Expiry GEX Regime" in rendered
    assert "Stabilizing" in rendered
    assert "Distance to Zero Gamma" in rendered
    assert "30 Apr" in rendered  # per-expiry ATM IV tile label
    # Removed metrics must be gone entirely.
    assert "Combined OI" not in rendered
    assert "Combined GEX" not in rendered
    assert "Volatility" not in rendered


def test_bottom_panels_empty_state_returns_alert(tmp_path: Path):
    service = make_view_data_service(tmp_path)
    card = build_bottom_panels(None, None, service)
    assert "Fetch data to view analytics." in repr(card)


def test_report_builder_computes_analytics_set():
    from analytics.report_builder import build_pair_report

    request = RequestSpec(
        asset_id=13,
        segment_code="IDX_I",
        expiry="2026-04-30",
        display_name="Nifty",
        lot_size=65,
        strike_size=50,
    )
    rows = build_enhanced_option_rows(
        stamp_option_rows(
            parse_option_chain_response(sample_option_chain_payload(), 13, "IDX_I", "2026-04-30"),
            "group-1",
            timestamp="2026-04-10T03:45:00+00:00",
        ),
        request_spec=request,
        risk_free_rate=0.065,
        market_volatility=0.15,
    )
    report = build_pair_report(request, rows)

    gex = report["gex_analysis"]
    assert "call_gex" in gex and "put_gex" in gex
    # CE contributes positive GEX, PE negative (mainstream dealer sign convention)
    assert gex["put_gex"] < 0 < gex["call_gex"]
    assert gex["market_regime"] in {"Stabilizing", "Destabilizing"}

    vex = report["vex_analysis"]
    assert vex["call_vex"] > 0 and vex["put_vex"] > 0
    assert isinstance(vex["vol_regime"], str)

    greeks = report["greeks_summary"]
    assert greeks["avg_call_delta"] == 0.51
    assert greeks["avg_put_delta"] == -0.49
    assert greeks["delta_skew"] == 1.0
    assert greeks["max_vega_strike"] == 22400.0
    assert greeks["vega_skew"] == 0.3
