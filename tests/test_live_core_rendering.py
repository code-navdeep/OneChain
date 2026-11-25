from __future__ import annotations

from pathlib import Path

from domain.requests import RequestSpec
from features.chain.rendering import build_chain_tab
from features.charts.rendering import build_charts_tab
from repositories.market_data_repository import MarketDataRepository
from repositories.snapshot_repository import SnapshotRepository
from services.view_data_service import ViewDataService
from state.builders import build_loaded_report_state
from state.models import LoadedPairState, LoadedReportState
from state.selectors import filter_report_rows_by_strike_range


def sample_report() -> dict:
    return {
        "pair_key": "13|IDX_I|2026-04-30",
        "instrument_key": "13|IDX_I",
        "display_name": "Nifty",
        "expiry": "2026-04-30",
        "lot_size": 65,
        "strike_size": 50,
        "underlying_price": 22510.0,
        "totals": {
            "total_oi": 1950.0,
            "total_volume": 930.0,
            "put_call_ratio": 0.95,
        },
        "rows": [
            {
                "strike": 22400.0, "option_type": "CE", "last_price": 120.0,
                "oi_lots": 15.4, "oi_change_lots": 1.5, "volume_lots": 7.7,
                "iv": 14.2, "gamma_exposure": -33.7,
            },
            {
                "strike": 22400.0, "option_type": "PE", "last_price": 110.0,
                "oi_lots": 14.6, "oi_change_lots": 0.4, "volume_lots": 6.6,
                "iv": 15.4, "gamma_exposure": 38.1,
            },
            {
                "strike": 22500.0, "option_type": "CE", "last_price": 80.0,
                "oi_lots": 13.1, "oi_change_lots": -0.2, "volume_lots": 4.6,
                "iv": 13.8, "gamma_exposure": -27.6,
            },
            {
                "strike": 22500.0, "option_type": "PE", "last_price": 145.0,
                "oi_lots": 18.5, "oi_change_lots": 1.2, "volume_lots": 7.8,
                "iv": 16.1, "gamma_exposure": 39.2,
            },
        ],
    }


def build_loaded_state() -> object:
    request_spec = RequestSpec(
        asset_id=13,
        segment_code="IDX_I",
        expiry="2026-04-30",
        display_name="Nifty",
        lot_size=65,
        strike_size=50,
    )
    return build_loaded_report_state(
        source="live",
        request_specs=[request_spec],
        fetch_group_id="group-1",
        pair_reports={request_spec.pair_key.value: sample_report()},
        status_text="Fetched 1 of 1 request(s)",
    )


def build_view_data_service(tmp_path: Path) -> ViewDataService:
    return ViewDataService(
        market_data_repository=MarketDataRepository(tmp_path / "market.db"),
        snapshot_repository=SnapshotRepository(tmp_path / "snapshot.db"),
    )


def test_filter_report_rows_by_strike_range_keeps_atm_window():
    rows = filter_report_rows_by_strike_range(sample_report(), "1")
    strikes = sorted({row["strike"] for row in rows})
    assert strikes == [22500.0]


def test_chain_renderer_uses_loaded_report_state(tmp_path: Path):
    loaded_state = build_loaded_state()
    card = build_chain_tab(loaded_state, "13|IDX_I|2026-04-30", "All", build_view_data_service(tmp_path))
    rendered = repr(card)
    # The loaded pair's strike rows are rendered into the chain table.
    assert "22400" in rendered
    assert "onechain-chain-card" in rendered


def test_charts_renderer_uses_loaded_report_state():
    loaded_state = build_loaded_state()
    card = build_charts_tab(loaded_state, "13|IDX_I|2026-04-30", "All")
    rendered = repr(card)
    assert "Open Interest by Strike" in rendered
    assert "Gamma Exposure by Strike" in rendered


def test_rate_markers_paint_native_background():
    """Tier-2 Layer-1: a crossing colours its marker cell natively (white=up / sky-blue=down) via
    style_data_conditional — no markdown. Greeks view (no marker columns) has no marker background."""
    from features.chain.table_model import RATE_MARKER_COLUMN_IDS, build_option_chain_table

    rate_markers = {(22400.0, "CE", "oi"): "up", (22400.0, "PE", "iv"): "down"}
    table = build_option_chain_table(sample_report(), view_data_service=None, rate_markers=rate_markers)
    styles = table.style_data_conditional
    assert [
        s for s in styles
        if s.get("if", {}).get("column_id") == "call_oi_marker" and s.get("backgroundColor") == "#ffffff"
    ], "an up crossing should paint call_oi_marker white"
    assert [
        s for s in styles
        if s.get("if", {}).get("column_id") == "put_iv_marker" and s.get("backgroundColor") == "#38bdf8"
    ], "a down crossing should paint put_iv_marker sky-blue"

    greeks = build_option_chain_table(
        sample_report(), view_data_service=None, show_greeks=True, rate_markers=rate_markers
    )
    assert not [
        s for s in greeks.style_data_conditional
        if s.get("if", {}).get("column_id") in RATE_MARKER_COLUMN_IDS and s.get("backgroundColor")
    ], "greeks view has no marker columns → no marker background"


def test_marker_columns_are_plain_not_markdown():
    """The 6 marker columns stay present in the non-greeks layout as plain (non-markdown) columns; the
    greeks view has none."""
    from features.chain.table_model import RATE_MARKER_COLUMN_IDS, define_table_columns

    normal = {c["id"]: c for c in define_table_columns(False)}
    for cid in RATE_MARKER_COLUMN_IDS:
        assert cid in normal, f"{cid} must be present in the non-greeks layout"
        assert "presentation" not in normal[cid], f"{cid} must be a plain column (no markdown)"
    greeks_ids = {c["id"] for c in define_table_columns(True)}
    assert not (set(RATE_MARKER_COLUMN_IDS) & greeks_ids), "greeks view has no marker columns"
