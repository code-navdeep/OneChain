from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from analytics.option_chain import build_enhanced_option_rows, parse_option_chain_response, stamp_option_rows
from analytics.report_builder import build_pair_report
from domain.requests import RequestSpec
from repositories.asset_repository import AssetRepository
from repositories.market_data_repository import MarketDataRepository
from repositories.saved_moment_repository import SavedMomentRepository
from repositories.snapshot_repository import SnapshotRepository
from services.advanced_analysis_service import AdvancedAnalysisService
from services.historical_replay_service import HistoricalReplayService
from services.live_fetch_service import LiveFetchService
from services.mark_moment_service import MarkMomentService
from analytics.historical_metrics import realized_volatility_percent
from services.snapshot_collection_service import (
    SnapshotCollectionService,
    _group_assets_for_collection,
    _select_expiries,
)
from services.request_scheduler_service import (
    RequestSchedulerState,
    build_request_key,
    calculate_wait_time,
    should_bypass_delay,
)
from services.view_data_service import ViewDataService
from domain.assets import AssetDefinition
from state.models import ActiveViewContext
from services.vix_service import VixService


IST = ZoneInfo("Asia/Kolkata")


class FakeApiService:
    def __init__(self, payload_by_pair_key: dict[str, dict]):
        self.payload_by_pair_key = payload_by_pair_key

    def fetch_option_chain(self, asset_id: int, segment_code: str, expiry: str):
        return self.payload_by_pair_key.get(f"{asset_id}|{segment_code}|{expiry}")


class FakeSnapshotApiService:
    def __init__(self, expiries_by_instrument: dict[str, list[str]], payload_by_pair_key: dict[str, dict]):
        self.expiries_by_instrument = expiries_by_instrument
        self.payload_by_pair_key = payload_by_pair_key

    def get_expiry_list(self, asset_id: int, segment_code: str, use_cache: bool = True):
        return list(self.expiries_by_instrument.get(f"{asset_id}|{segment_code}", []))

    def fetch_option_chain(self, asset_id: int, segment_code: str, expiry: str):
        return self.payload_by_pair_key.get(f"{asset_id}|{segment_code}|{expiry}")


def make_asset_repository(tmp_path: Path) -> AssetRepository:
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "\n".join(
            [
                "asset_id,asset_name,exchange,segment_code,strike_size,lot_size,display_name",
                "13,Nifty,NSE,IDX_I,50,65,Nifty",
            ]
        ),
        encoding="utf-8",
    )
    return AssetRepository(csv_path)


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


def _store_enriched(repo, asset_id: int, segment: str, expiry: str, group: str, ts: str, lot_size: int = 65) -> None:
    """Parse + stamp + enrich + store, the way the live writer does (no rate/vol needed)."""
    stamped = stamp_option_rows(
        parse_option_chain_response(sample_option_chain_payload(), asset_id, segment, expiry),
        group,
        timestamp=ts,
    )
    spec = RequestSpec(
        asset_id=asset_id, segment_code=segment, expiry=expiry,
        display_name="Nifty", lot_size=lot_size, strike_size=50,
    )
    repo.insert_option_rows(
        build_enhanced_option_rows(stamped, request_spec=spec, risk_free_rate=None, market_volatility=None)
    )


def test_scheduler_uses_segment_safe_keys_and_previous_key_bypass():
    first_key = build_request_key(25, "IDX_I", "2026-04-30")
    second_key = build_request_key(25, "NSE_EQ", "2026-04-30")
    assert first_key != second_key

    state = RequestSchedulerState(last_request_key=first_key, last_request_time=100.0)
    assert should_bypass_delay(state, second_key) is True
    assert calculate_wait_time(state, second_key, now=101.0) == 0.0


def test_scheduler_waits_for_same_key():
    state = RequestSchedulerState(last_request_key="13|IDX_I|2026-04-30", last_request_time=100.0)
    wait_time = calculate_wait_time(state, "13|IDX_I|2026-04-30", now=101.0)
    assert wait_time > 0


def test_option_chain_pipeline_builds_report_and_enhanced_rows():
    request_spec = RequestSpec(
        asset_id=13, segment_code="IDX_I", expiry="2026-04-30",
        display_name="Nifty", lot_size=65, strike_size=50,
    )
    normalized = parse_option_chain_response(sample_option_chain_payload(), 13, "IDX_I", "2026-04-30")
    stamped = stamp_option_rows(normalized, "group-1", timestamp="2026-04-10T10:00:00+00:00")
    enhanced = build_enhanced_option_rows(
        stamped, request_spec=request_spec, risk_free_rate=0.065, market_volatility=0.15
    )
    report = build_pair_report(request_spec, enhanced)
    assert len(enhanced) == 2
    assert all(row["bs_price"] is not None for row in enhanced)
    assert "oi" not in enhanced[0]  # raw counts are dropped
    # total OI is now in lots: (1000 + 950) / 65 = 30
    assert report["totals"]["total_oi"] == 30.0
    assert report["pair_key"] == "13|IDX_I|2026-04-30"
    # VEX is vega · oi_lots (no spurious extra lot factor).
    for row in enhanced:
        assert row["volatility_exposure"] == pytest.approx(row["vega"] * row["oi_lots"])
    # GEX sign is the mainstream convention: calls positive, puts negative.
    call = next(r for r in enhanced if r["option_type"] == "CE")
    put = next(r for r in enhanced if r["option_type"] == "PE")
    assert call["gamma_exposure"] > 0
    assert put["gamma_exposure"] < 0


def test_live_fetch_service_writes_rows_and_pair_report(tmp_path):
    market = MarketDataRepository(tmp_path / "market.db")
    api_service = FakeApiService({"13|IDX_I|2026-04-30": sample_option_chain_payload()})
    service = LiveFetchService(api_service, market)

    result = service.fetch_request_group(
        [RequestSpec(asset_id=13, segment_code="IDX_I", expiry="2026-04-30", display_name="Nifty", lot_size=65, strike_size=50)]
    )

    assert result.loaded_report_state.pair_order == ["13|IDX_I|2026-04-30"]
    assert len(result.pair_results) == 1
    assert len(market.list_rows_for_fetch_group(result.fetch_group_id)) == 2
    assert market.get_pair_report(result.fetch_group_id, 13, "IDX_I", "2026-04-30") is not None


def test_live_fetch_service_persist_false_writes_nothing(tmp_path):
    market = MarketDataRepository(tmp_path / "market.db")
    api_service = FakeApiService({"13|IDX_I|2026-04-30": sample_option_chain_payload()})
    service = LiveFetchService(api_service, market)

    result = service.fetch_request_group(
        [RequestSpec(asset_id=13, segment_code="IDX_I", expiry="2026-04-30", display_name="Nifty", lot_size=65, strike_size=50)],
        persist=False,
    )

    assert result.pair_results and result.pair_results[0].report["rows"]
    assert market.list_fetch_group_summaries() == []
    assert market.get_pair_report(result.fetch_group_id, 13, "IDX_I", "2026-04-30") is None


class _FakeAdvancedApiService:
    def __init__(self, cached: list[str], full: list[str], payload: dict):
        self.cached = cached
        self.full = full
        self.payload = payload
        self.cache_calls: list[bool] = []

    def get_expiry_list(self, asset_id: int, segment_code: str, use_cache: bool = True):
        self.cache_calls.append(use_cache)
        return list(self.cached) if use_cache else list(self.full)

    def fetch_option_chain(self, asset_id: int, segment_code: str, expiry: str):
        return self.payload


def test_advanced_analysis_service_force_refetches_on_cache_shortfall(tmp_path: Path):
    asset_repository = make_asset_repository(tmp_path)
    market = MarketDataRepository(tmp_path / "market.db")
    api_service = _FakeAdvancedApiService(
        cached=["2099-12-31"],  # only 1 cached < 3 requested → force refetch
        full=["2099-12-31", "2100-01-31", "2100-02-28"],
        payload=sample_option_chain_payload(),
    )
    live_fetch = LiveFetchService(api_service, market)
    service = AdvancedAnalysisService(api_service, asset_repository, live_fetch)

    analysis = service.run(13, "IDX_I", 3, "All")

    assert api_service.cache_calls == [True, False]   # cache first, then force refresh
    assert "error" not in analysis
    assert analysis["request"]["expiry_count_fetched"] == 3
    assert market.list_fetch_group_summaries() == []  # ephemeral: advanced writes nothing


def test_live_fetch_service_can_include_vix_snapshot(tmp_path):
    market = MarketDataRepository(tmp_path / "market.db")
    api_service = FakeApiService({"13|IDX_I|2026-04-30": sample_option_chain_payload()})
    vix_service = VixService(market, fetcher=lambda: (14.8, {"previous_close": 14.0, "net_change": 0.8}))
    service = LiveFetchService(api_service, market, vix_service=vix_service)

    result = service.fetch_request_group(
        [RequestSpec(asset_id=13, segment_code="IDX_I", expiry="2026-04-30", display_name="Nifty", lot_size=65, strike_size=50)],
        fetch_vix=True,
    )

    assert result.metadata["vix_snapshot"]["value"] == 14.8
    assert len(market.list_vix_rows()) == 1


def test_vix_service_persists_values(tmp_path):
    market = MarketDataRepository(tmp_path / "market.db")
    service = VixService(market, fetcher=lambda: (14.8, {"previous_close": 14.0, "net_change": 0.8}))
    snapshot = service.fetch_and_store()
    assert snapshot is not None
    assert len(market.list_vix_rows()) == 1


def test_historical_replay_service_rebuilds_fetch_group(tmp_path: Path):
    asset_repository = make_asset_repository(tmp_path)
    market = MarketDataRepository(tmp_path / "market.db")
    _store_enriched(market, 13, "IDX_I", "2026-04-30", "group-1", "2026-04-10T03:45:00+00:00")

    service = HistoricalReplayService(market, asset_repository)
    dates = service.list_playback_dates()
    moments = service.list_playback_moments(dates[0])
    loaded_state = service.load_fetch_group("group-1", source="playback")

    assert dates == ["2026-04-10"]
    assert moments[0].fetch_group_id == "group-1"
    assert loaded_state is not None
    assert loaded_state.source == "playback"
    assert loaded_state.pair_order == ["13|IDX_I|2026-04-30"]


def test_historical_replay_moments_are_oldest_first(tmp_path: Path):
    # Stored out of time order; list_playback_moments must return oldest-first so the scrubber runs
    # left→right = forward in time.
    asset_repository = make_asset_repository(tmp_path)
    market = MarketDataRepository(tmp_path / "market.db")
    _store_enriched(market, 13, "IDX_I", "2026-04-30", "g-late", "2026-04-10T09:00:00+00:00")
    _store_enriched(market, 13, "IDX_I", "2026-04-30", "g-early", "2026-04-10T04:00:00+00:00")
    _store_enriched(market, 13, "IDX_I", "2026-04-30", "g-mid", "2026-04-10T06:30:00+00:00")

    service = HistoricalReplayService(market, asset_repository)
    moments = service.list_playback_moments(service.list_playback_dates()[0])
    starts = [m.start_ts for m in moments]
    assert starts == sorted(starts)  # ascending (oldest-first)


def test_historical_replay_service_supports_asset_expiry_date_time_filters(tmp_path: Path):
    asset_repository = make_asset_repository(tmp_path)
    market = MarketDataRepository(tmp_path / "market.db")
    _store_enriched(market, 13, "IDX_I", "2026-04-30", "group-1", "2026-04-10T03:45:00+00:00")
    _store_enriched(market, 13, "IDX_I", "2026-05-07", "group-2", "2026-04-11T03:46:00+00:00")

    service = HistoricalReplayService(market, asset_repository)
    assets = service.list_playback_assets()
    assert assets and assets[0]["value"] == "13|IDX_I"

    expiries = service.list_playback_expiries("13|IDX_I")
    assert expiries == ["2026-04-30", "2026-05-07"]

    dates = service.list_playback_dates("13|IDX_I", "2026-05-07")
    assert dates == ["2026-04-11"]

    moments = service.list_playback_moments("2026-04-11", "13|IDX_I", "2026-05-07")
    assert len(moments) == 1
    assert moments[0].token.endswith("|13|IDX_I|2026-05-07")


def test_historical_replay_catalog_cache_refreshes_when_new_group_is_written(tmp_path: Path):
    asset_repository = make_asset_repository(tmp_path)
    market = MarketDataRepository(tmp_path / "market.db")
    _store_enriched(market, 13, "IDX_I", "2026-04-30", "group-1", "2026-04-10T03:45:00+00:00")

    service = HistoricalReplayService(market, asset_repository)
    first_dates = service.list_playback_dates("13|IDX_I", "2026-04-30")
    assert first_dates == ["2026-04-10"]

    _store_enriched(market, 13, "IDX_I", "2026-04-30", "group-2", "2026-04-11T03:45:00+00:00")
    refreshed_dates = service.list_playback_dates("13|IDX_I", "2026-04-30")

    assert refreshed_dates == ["2026-04-11", "2026-04-10"]


def test_mark_moment_service_saves_and_lists_moments(tmp_path: Path):
    asset_repository = make_asset_repository(tmp_path)
    market = MarketDataRepository(tmp_path / "market.db")
    saved_repository = SavedMomentRepository(tmp_path / "market.db")
    _store_enriched(market, 13, "IDX_I", "2026-04-30", "group-1", "2026-04-10T03:45:00+00:00")
    replay_service = HistoricalReplayService(market, asset_repository)
    mark_service = MarkMomentService(saved_repository, asset_repository)
    loaded_state = replay_service.load_fetch_group("group-1", source="live")

    save_result = mark_service.save_loaded_state(
        loaded_state,
        active_view_context=ActiveViewContext(kind="pair", source="live", pair_key="13|IDX_I|2026-04-30"),
        label="Open",
    )
    moment_rows = mark_service.list_saved_moments(13, "IDX_I", "2026-04-30")

    assert save_result.save_count == 1
    assert save_result.source_mode == "live"
    assert save_result.view_kind == "pair"
    assert moment_rows[0]["fetch_group_id"] == "group-1"
    assert moment_rows[0]["source_mode"] == "live"
    assert moment_rows[0]["view_kind"] == "pair"


def test_mark_moment_service_saves_playback_aggregate_as_member_pairs(tmp_path: Path):
    asset_repository = make_asset_repository(tmp_path)
    market = MarketDataRepository(tmp_path / "market.db")
    saved_repository = SavedMomentRepository(tmp_path / "market.db")
    _store_enriched(market, 13, "IDX_I", "2026-04-30", "group-1", "2026-04-10T03:45:00+00:00")
    _store_enriched(market, 13, "IDX_I", "2026-05-07", "group-1", "2026-04-10T03:45:00+00:00")

    replay_service = HistoricalReplayService(market, asset_repository)
    mark_service = MarkMomentService(saved_repository, asset_repository)
    loaded_state = replay_service.load_fetch_group("group-1", source="playback")

    save_result = mark_service.save_loaded_state(
        loaded_state,
        active_view_context=ActiveViewContext(
            kind="aggregate",
            source="playback",
            instrument_key="13|IDX_I",
            pair_key="aggregate|13|IDX_I",
            fetch_group_id="group-1",
        ),
        label="Playback Aggregate",
    )

    first_expiry_rows = mark_service.list_saved_moments(13, "IDX_I", "2026-04-30")
    second_expiry_rows = mark_service.list_saved_moments(13, "IDX_I", "2026-05-07")

    assert save_result.save_count == 2
    assert save_result.source_mode == "playback"
    assert save_result.view_kind == "aggregate"
    assert "playback aggregate view" in save_result.status_text.lower()
    assert first_expiry_rows[0]["source_mode"] == "playback"
    assert second_expiry_rows[0]["view_kind"] == "aggregate"


def test_view_data_service_reads_price_and_snapshot_series(tmp_path: Path):
    asset_repository = make_asset_repository(tmp_path)
    market = MarketDataRepository(tmp_path / "market.db")
    snapshot_repository = SnapshotRepository(tmp_path / "snapshot.db")
    # The spot is now read per-fetch from pair_reports (live path = today's session), so seed one
    # pair_reports row stamped today rather than option_rows on a fixed past date.
    today = datetime.now(IST).date().isoformat()
    market.insert_pair_report({
        "timestamp": f"{today}T09:20:00", "fetch_group_id": "group-1",
        "underlying_asset_id": 13, "segment_code": "IDX_I", "expiry": "2026-04-30",
        "underlying_price": 22500.0,
    })
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
    service = ViewDataService(market_data_repository=market, snapshot_repository=snapshot_repository)

    price_series = service.get_underlying_price_series(13, "IDX_I", "2026-04-30")
    history = service.get_instrument_historical_metrics(13, "IDX_I")
    latest = service.get_latest_historical_metric(13, "IDX_I")

    assert asset_repository.get_asset(13, "IDX_I").display_name == "Nifty"
    assert len(price_series) == 1
    assert len(history) == 1
    assert latest["rv_22d"] == 0.2


def _snapshot_row(strike: float, option_type: str, iv: float, *, spot: float, ts: str) -> dict:
    """A full snapshot options_data row (every non-nullable column) for one contract."""
    return {
        "timestamp": ts, "fetch_group_id": "g-1", "underlying_asset_id": 13,
        "segment_code": "IDX_I", "expiry": "2026-04-30", "strike": strike, "option_type": option_type,
        "last_price": 10.0, "bid_price": 0.0, "ask_price": 0.0, "underlying_price": spot,
        "iv": iv, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
        "oi_lots": 5.0, "previous_oi_lots": 4.0, "oi_change_lots": 1.0, "volume_lots": 2.0,
        "straddle_oi_lots": 10.0, "gamma_exposure": 1.0, "volatility_exposure": 1.0,
    }


def test_get_yesterday_totals_windows_iv_to_atm_but_totals_stay_full_chain(tmp_path: Path):
    snapshot = SnapshotRepository(tmp_path / "snapshot.db")
    spot = 22500.0
    ts = "2026-06-13T10:00:00"  # a prior date, before today's 09:00 IST cutoff
    rows: list[dict] = []
    # 12 strikes each side (step 100). The nearest 10/side are the ±10 ATM window; i ∈ {11, 12}
    # are the far OTM wings, given an extreme IV that must be EXCLUDED from the windowed mean.
    for i in range(1, 13):
        wing = i >= 11
        iv = 500.0 if wing else 15.0
        for option_type in ("CE", "PE"):
            rows.append(_snapshot_row(spot + i * 100, option_type, iv, spot=spot, ts=ts))
            rows.append(_snapshot_row(spot - i * 100, option_type, iv, spot=spot, ts=ts))
    snapshot.insert_option_rows(rows)

    service = ViewDataService(
        market_data_repository=MarketDataRepository(tmp_path / "market.db"),
        snapshot_repository=snapshot,
    )
    totals = service.get_yesterday_totals(13, "IDX_I", "2026-04-30")

    assert totals is not None
    # IV averaged only over the ±10 window → the 500 wings are excluded, leaving 15.
    assert totals["avg_call_iv"] == 15.0
    assert totals["avg_put_iv"] == 15.0
    # OI totals stay full-chain: 12 strikes/side × 2 sides = 24 contracts per option type × 5 lots.
    assert totals["total_call_oi"] == 24 * 5.0
    assert totals["total_put_oi"] == 24 * 5.0


def test_get_yesterday_totals_computes_bracketing_atm_skew_distinct_from_range_skew(tmp_path: Path):
    # Bracketing-ATM IV (the two strikes nearest spot) must be computed independently of the
    # ±10-window RANGE average — reusing analytics.report_builder.compute_atm_iv_gap, the same
    # primitive that produces today's live atm_gap. Give the near-ATM strikes (i=1) a different IV
    # than the rest of the window (i=2..10) so the two figures provably diverge.
    snapshot = SnapshotRepository(tmp_path / "snapshot.db")
    spot = 22500.0
    ts = "2026-06-13T10:00:00"
    rows: list[dict] = []
    for i in range(1, 11):
        call_iv = 20.0 if i == 1 else 10.0
        put_iv = 26.0 if i == 1 else 14.0
        rows.append(_snapshot_row(spot + i * 100, "CE", call_iv, spot=spot, ts=ts))
        rows.append(_snapshot_row(spot - i * 100, "CE", call_iv, spot=spot, ts=ts))
        rows.append(_snapshot_row(spot + i * 100, "PE", put_iv, spot=spot, ts=ts))
        rows.append(_snapshot_row(spot - i * 100, "PE", put_iv, spot=spot, ts=ts))
    snapshot.insert_option_rows(rows)

    service = ViewDataService(
        market_data_repository=MarketDataRepository(tmp_path / "market.db"),
        snapshot_repository=snapshot,
    )
    totals = service.get_yesterday_totals(13, "IDX_I", "2026-04-30")

    assert totals is not None
    # Range average over all 20 strikes/side: (20*2 + 10*18)/20 = 11.0 ; (26*2 + 14*18)/20 = 15.2.
    assert totals["avg_call_iv"] == pytest.approx(11.0)
    assert totals["avg_put_iv"] == pytest.approx(15.2)
    # Bracketing ATM = average of the two nearest (i=1) strikes on each side — 20.0 / 26.0 — NOT the
    # range average above, proving atm_call_iv/atm_put_iv are the bracketing values.
    assert totals["atm_call_iv"] == pytest.approx(20.0)
    assert totals["atm_put_iv"] == pytest.approx(26.0)
    # atm_gap (6.0) differs from the range skew (4.2) — the two are genuinely distinct metrics.
    assert totals["atm_gap"] == pytest.approx(6.0)
    range_skew = totals["avg_put_iv"] - totals["avg_call_iv"]
    assert range_skew == pytest.approx(4.2)
    assert totals["atm_gap"] != pytest.approx(range_skew)


def test_snapshot_grouping_respects_number_expiries_batches():
    assets = [
        AssetDefinition(1, "A", "NSE", "NSE_EQ", 10, 1, "A", number_expiries=1),
        AssetDefinition(2, "B", "NSE", "NSE_EQ", 10, 1, "B", number_expiries=1),
        AssetDefinition(3, "C", "NSE", "NSE_EQ", 10, 1, "C", number_expiries=2),
        AssetDefinition(4, "D", "NSE", "NSE_EQ", 10, 1, "D", number_expiries=1),
    ]
    groups = _group_assets_for_collection(assets)
    assert [[item.asset_id for item in group] for group in groups] == [[1, 2], [3], [4]]


def test_snapshot_expiry_selection_skips_same_day_nearest():
    selected = _select_expiries(
        ["2026-04-10", "2026-04-17", "2026-04-24"],
        number_expiries=2,
        today_ist="2026-04-10",
    )
    assert selected == ["2026-04-17", "2026-04-24"]


def test_snapshot_expiry_selection_sorts_unordered_input():
    # The API/cache order is NOT trusted: the list is sorted (ISO lex == chronological) before
    # the same-day drop and the first-N pick, so an unordered response can't keep the same-day
    # expiry or select the wrong N.
    selected = _select_expiries(
        ["2026-04-24", "2026-04-10", "2026-04-17"],
        number_expiries=2,
        today_ist="2026-04-10",
    )
    assert selected == ["2026-04-17", "2026-04-24"]


def test_snapshot_realized_volatility_requires_window_plus_one_closes():
    assert realized_volatility_percent(
        [
            {"trade_date": "2026-04-01", "underlying_price": 100.0},
            {"trade_date": "2026-04-02", "underlying_price": 101.0},
            {"trade_date": "2026-04-03", "underlying_price": 102.0},
            {"trade_date": "2026-04-04", "underlying_price": 103.0},
            {"trade_date": "2026-04-05", "underlying_price": 104.0},
        ],
        window_days=5,
    ) is None


def test_snapshot_realized_volatility_computes_annualized_percent():
    rv = realized_volatility_percent(
        [
            {"trade_date": "2026-04-01", "underlying_price": 100.0},
            {"trade_date": "2026-04-02", "underlying_price": 104.0},
            {"trade_date": "2026-04-03", "underlying_price": 103.0},
            {"trade_date": "2026-04-04", "underlying_price": 107.0},
            {"trade_date": "2026-04-05", "underlying_price": 106.0},
            {"trade_date": "2026-04-06", "underlying_price": 110.0},
        ],
        window_days=5,
    )
    assert rv is not None
    assert round(float(rv), 6) == round(41.44608816830391, 6)


def test_snapshot_collection_service_persists_grouped_fetch_ids_and_status_rows(tmp_path: Path):
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "\n".join(
            [
                "asset_id,asset_name,exchange,segment_code,strike_size,lot_size,display_name,number_expiries",
                "13,Nifty,NSE,IDX_I,50,65,Nifty,2",
                "25,BankNifty,NSE,IDX_I,100,35,BankNifty,1",
                "15,FinNifty,NSE,IDX_I,50,65,FinNifty,1",
            ]
        ),
        encoding="utf-8",
    )
    asset_repository = AssetRepository(csv_path)
    snapshot_repository = SnapshotRepository(tmp_path / "snapshot.db")
    today_ist = datetime.now(IST).date().isoformat()
    expiries = [today_ist, "2099-12-31", "2100-01-31"]
    payload = sample_option_chain_payload()
    api_service = FakeSnapshotApiService(
        expiries_by_instrument={
            "13|IDX_I": expiries,
            "25|IDX_I": expiries[:2],
            "15|IDX_I": expiries[:2],
        },
        payload_by_pair_key={
            "13|IDX_I|2099-12-31": payload,
            "13|IDX_I|2100-01-31": payload,
            "25|IDX_I|2099-12-31": payload,
            "15|IDX_I|2099-12-31": payload,
        },
    )
    service = SnapshotCollectionService(api_service, asset_repository, snapshot_repository)

    result = service.collect_once()
    rows = snapshot_repository.list_option_rows()
    assert result.status == "success"
    assert result.pairs_processed == 4
    assert result.rows_written == 8
    assert len(rows) == 8

    asset_13_groups = {str(row["fetch_group_id"]) for row in rows if int(row["underlying_asset_id"]) == 13}
    asset_25_groups = {str(row["fetch_group_id"]) for row in rows if int(row["underlying_asset_id"]) == 25}
    asset_15_groups = {str(row["fetch_group_id"]) for row in rows if int(row["underlying_asset_id"]) == 15}
    assert len(asset_13_groups) == 1
    assert len(asset_25_groups) == 1
    assert len(asset_15_groups) == 1
    assert asset_25_groups == asset_15_groups
    assert asset_13_groups != asset_25_groups

    with snapshot_repository.connection() as conn:
        status_rows = conn.execute(
            "SELECT 1 FROM collection_status WHERE status = 'success'"
        ).fetchall()
    assert len(status_rows) == 4


def test_snapshot_collection_service_writes_snapshot_rows_and_metrics(tmp_path: Path):
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "\n".join(
            [
                "asset_id,asset_name,exchange,segment_code,strike_size,lot_size,display_name,number_expiries",
                "13,Nifty,NSE,IDX_I,50,65,Nifty,1",
            ]
        ),
        encoding="utf-8",
    )
    asset_repository = AssetRepository(csv_path)
    snapshot_repository = SnapshotRepository(tmp_path / "snapshot.db")
    api_service = FakeSnapshotApiService(
        expiries_by_instrument={"13|IDX_I": ["2099-04-10"]},
        payload_by_pair_key={"13|IDX_I|2099-04-10": sample_option_chain_payload()},
    )
    service = SnapshotCollectionService(api_service, asset_repository, snapshot_repository)

    result = service.collect_once()
    rows = snapshot_repository.list_option_rows()
    metrics = snapshot_repository.list_historical_metrics(13, "IDX_I")

    assert result.status in {"success", "partial_failure"}
    assert len(rows) == 2
    assert rows[0]["oi_lots"] is not None  # stored enriched, not raw
    assert len(metrics) == 1
    assert metrics[0]["rv_22d"] is None  # one snapshot day => not enough closes => honest None


def test_snapshot_collection_service_uses_daily_closes_for_rv_metric(tmp_path: Path):
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "\n".join(
            [
                "asset_id,asset_name,exchange,segment_code,strike_size,lot_size,display_name,number_expiries",
                "13,Nifty,NSE,IDX_I,50,65,Nifty,1",
            ]
        ),
        encoding="utf-8",
    )
    asset_repository = AssetRepository(csv_path)
    snapshot_repository = SnapshotRepository(tmp_path / "snapshot.db")
    # 22 seeded dates + today's collect_once snapshot = 23 daily closes, enough for the smallest
    # (22d) window (needs window + 1 = 23 closes). Deterministic, varied series → non-degenerate RV.
    historical_dates = [f"2026-04-{day:02d}" for day in range(1, 23)]
    historical_prices = [22000.0 + 100.0 * i + (150.0 if i % 2 else 0.0) for i in range(22)]
    for date_value, price in zip(historical_dates, historical_prices):
        _store_enriched(
            snapshot_repository, 13, "IDX_I", "2099-04-10",
            f"seed|{date_value}", f"{date_value}T09:15:00+05:30",
        )
        # overwrite the underlying_price for that day's stored rows
        with snapshot_repository.connection() as conn:
            conn.execute(
                "UPDATE options_data SET underlying_price = ? WHERE fetch_group_id = ?",
                (price, f"seed|{date_value}"),
            )

    api_service = FakeSnapshotApiService(
        expiries_by_instrument={"13|IDX_I": ["2099-04-10"]},
        payload_by_pair_key={"13|IDX_I|2099-04-10": sample_option_chain_payload()},
    )
    service = SnapshotCollectionService(api_service, asset_repository, snapshot_repository)

    service.collect_once()
    metrics = snapshot_repository.list_historical_metrics(13, "IDX_I")

    assert len(metrics) == 1
    # collect_once fed the persisted daily closes into RV over the smallest window (22d); the stored
    # value must equal what the pure function yields on those same closes (and be a real, positive RV).
    closes = snapshot_repository.list_daily_underlying_closes(13, "IDX_I", limit_days=30)
    expected_rv_22d = realized_volatility_percent(closes, window_days=22)
    assert expected_rv_22d is not None and expected_rv_22d > 0
    assert metrics[0]["rv_22d"] == pytest.approx(expected_rv_22d)
