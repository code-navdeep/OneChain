"""Caching behaviour for the per-tick / on-open laggy reads.

Covers the incremental aggregate-GEX cache on ViewDataService (past days frozen, live day
appends only newer snapshots, day-range sweeps do no new DB work) and the playback catalog's
O(1)-signature incremental rebuild on HistoricalReplayService.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from analytics.option_chain import build_enhanced_option_rows, parse_option_chain_response, stamp_option_rows
from domain.requests import RequestSpec
from repositories.asset_repository import AssetRepository
from repositories.market_data_repository import MarketDataRepository
from services.historical_replay_service import HistoricalReplayService
from services.view_data_service import ViewDataService

IST = ZoneInfo("Asia/Kolkata")
ASSETS_CSV = Path(__file__).resolve().parents[1] / "assets" / "assets_select.csv"


# --- aggregate-GEX incremental cache ------------------------------------------------- #


class _SpyGexRepo:
    """Stand-in MarketDataRepository for the single read get_oa_aggregate_gex now uses (today-only).

    The first range read (``since_iso`` = ``...T09:00:00``) is the session base read → returns the
    base points; any later read (``since_iso`` = the last cached timestamp) is a live-day delta →
    returns the queued ``_delta``. Records every call so tests can assert the access pattern.
    """

    def __init__(self, base: list[dict]):
        self._base = base
        self._delta: list[dict] = []
        self.range_calls: list[tuple[str, str | None]] = []

    def list_aggregate_gex_in_range(self, asset_id, segment_code, expiry, *, since_iso, until_iso=None):
        self.range_calls.append((since_iso, until_iso))
        if since_iso.endswith("T09:00:00"):
            return list(self._base)
        return list(self._delta)


def _point(ts: str, gex: float = 1.0) -> dict:
    return {"timestamp": ts, "total_gex": gex, "call_gex": gex / 2, "put_gex": gex / 2}


def test_gex_initial_read_floors_at_session_start():
    today = datetime.now(IST).date().isoformat()
    spy = _SpyGexRepo(base=[_point(f"{today}T09:15:00", 1.0), _point(f"{today}T09:20:00", 2.0)])
    svc = ViewDataService(market_data_repository=spy, snapshot_repository=None)

    first = svc.get_oa_aggregate_gex(13, "IDX_I", "2026-04-30")
    assert [p["total_gex"] for p in first] == [1.0, 2.0]
    assert spy.range_calls[0] == (f"{today}T09:00:00", None)  # today-only, floored at 09:00, open-ended


def test_gex_no_new_ticks_serves_cached():
    today = datetime.now(IST).date().isoformat()
    spy = _SpyGexRepo(base=[_point(f"{today}T09:15:00", 1.0)])
    svc = ViewDataService(market_data_repository=spy, snapshot_repository=None)

    first = svc.get_oa_aggregate_gex(13, "IDX_I", "2026-04-30")
    second = svc.get_oa_aggregate_gex(13, "IDX_I", "2026-04-30")  # _delta empty → nothing appended
    assert second == first
    # one initial session read + one (cheap, empty) incremental check — never a full re-aggregation.
    assert len(spy.range_calls) == 2
    assert spy.range_calls[1] == (f"{today}T09:15:00", None)  # since = last cached ts (exclusive)


def test_gex_live_day_appends_only_newer_snapshots():
    today = datetime.now(IST).date().isoformat()
    spy = _SpyGexRepo(base=[_point(f"{today}T09:15:00", 1.0), _point(f"{today}T09:20:00", 2.0)])
    svc = ViewDataService(market_data_repository=spy, snapshot_repository=None)

    first = svc.get_oa_aggregate_gex(13, "IDX_I", "2026-04-30")
    assert len(first) == 2

    spy._delta = [_point(f"{today}T09:25:00", 3.0)]  # a new tick lands
    second = svc.get_oa_aggregate_gex(13, "IDX_I", "2026-04-30")
    assert [p["total_gex"] for p in second] == [1.0, 2.0, 3.0]
    assert spy.range_calls[1] == (f"{today}T09:20:00", None)  # since = last cached ts (exclusive)


def test_gex_cache_self_heals_out_of_order_and_duplicate_timestamps():
    # If a stray out-of-order or duplicate timestamp ever reaches the append-only cache, the returned
    # series must still be strictly ascending in time with no duplicates — so the chart can't draw a
    # chord across the session (the phantom straight-line bug). See ViewDataService._sort_dedup.
    today = datetime.now(IST).date().isoformat()
    spy = _SpyGexRepo(base=[_point(f"{today}T09:15:00", 1.0), _point(f"{today}T09:20:00", 2.0)])
    svc = ViewDataService(market_data_repository=spy, snapshot_repository=None)

    svc.get_oa_aggregate_gex(13, "IDX_I", "2026-04-30")  # seed the cache
    # A malformed delta: an earlier ts (09:10), a duplicate (09:20), and a genuinely newer one (09:25).
    spy._delta = [
        _point(f"{today}T09:25:00", 3.0),
        _point(f"{today}T09:10:00", 9.0),
        _point(f"{today}T09:20:00", 2.0),
    ]
    result = svc.get_oa_aggregate_gex(13, "IDX_I", "2026-04-30")

    timestamps = [p["timestamp"] for p in result]
    assert timestamps == [
        f"{today}T09:10:00", f"{today}T09:15:00", f"{today}T09:20:00", f"{today}T09:25:00",
    ]  # de-duplicated + strictly ascending, regardless of the malformed delta order
    assert len(timestamps) == len(set(timestamps))  # no duplicate timestamps


# --- price chart incremental cache (per-fetch spot from pair_reports) ---------------- #


class _SpyPriceRepo:
    """Stand-in for the single read get_underlying_price_series uses (today-only live path).

    The first read floors at the session start (a midnight marker, ``...T00:00:00``) → returns the
    base points; any later read (``since`` = the last cached timestamp) is a live-day delta → returns
    the queued ``_delta``. Records every call so tests can assert the access pattern.
    """

    def __init__(self, base: list[dict]):
        self._base = base
        self._delta: list[dict] = []
        self.range_calls: list[tuple[str, str | None]] = []

    def list_underlying_price_in_range(self, asset_id, segment_code, expiry, *, since_iso, until_iso=None):
        self.range_calls.append((since_iso, until_iso))
        if since_iso.endswith("T00:00:00"):
            return list(self._base)
        return list(self._delta)


def _spot(ts: str, price: float = 22500.0) -> dict:
    return {"timestamp": ts, "fetch_group_id": "g", "underlying_price": price}


def test_price_initial_read_floors_at_session_start():
    today = datetime.now(IST).date().isoformat()
    spy = _SpyPriceRepo(base=[_spot(f"{today}T09:15:00", 22500.0), _spot(f"{today}T09:20:00", 22510.0)])
    svc = ViewDataService(market_data_repository=spy, snapshot_repository=None)

    first = svc.get_underlying_price_series(13, "IDX_I", "2026-04-30")
    assert [p["underlying_price"] for p in first] == [22500.0, 22510.0]
    assert spy.range_calls[0] == (f"{today}T00:00:00", None)  # live, floored at midnight, open-ended


def test_price_no_new_ticks_serves_cached():
    today = datetime.now(IST).date().isoformat()
    spy = _SpyPriceRepo(base=[_spot(f"{today}T09:15:00", 22500.0)])
    svc = ViewDataService(market_data_repository=spy, snapshot_repository=None)

    first = svc.get_underlying_price_series(13, "IDX_I", "2026-04-30")
    second = svc.get_underlying_price_series(13, "IDX_I", "2026-04-30")  # _delta empty → nothing appended
    assert second == first
    # one initial session read + one (cheap, empty) incremental check — never a full re-read.
    assert len(spy.range_calls) == 2
    assert spy.range_calls[1] == (f"{today}T09:15:00", None)  # since = last cached ts (exclusive)


def test_price_live_day_appends_only_newer_snapshots():
    today = datetime.now(IST).date().isoformat()
    spy = _SpyPriceRepo(base=[_spot(f"{today}T09:15:00", 22500.0), _spot(f"{today}T09:20:00", 22510.0)])
    svc = ViewDataService(market_data_repository=spy, snapshot_repository=None)

    first = svc.get_underlying_price_series(13, "IDX_I", "2026-04-30")
    assert len(first) == 2

    spy._delta = [_spot(f"{today}T09:25:00", 22520.0)]  # a new tick lands
    second = svc.get_underlying_price_series(13, "IDX_I", "2026-04-30")
    assert [p["underlying_price"] for p in second] == [22500.0, 22510.0, 22520.0]
    assert spy.range_calls[1] == (f"{today}T09:20:00", None)  # since = last cached ts (exclusive)


def test_price_playback_bypasses_cache_with_bounded_read():
    spy = _SpyPriceRepo(base=[_spot("2026-04-10T09:15:00", 22000.0)])
    svc = ViewDataService(market_data_repository=spy, snapshot_repository=None)

    # end_timestamp set → a one-off bounded read of the captured session, never cached.
    svc.get_underlying_price_series(13, "IDX_I", "2026-04-30", end_timestamp="2026-04-10T15:30:00")
    svc.get_underlying_price_series(13, "IDX_I", "2026-04-30", end_timestamp="2026-04-10T15:30:00")
    # bounded (since=session midnight, inclusive captured end) and NOT cached → two real reads.
    assert spy.range_calls == [
        ("2026-04-10T00:00:00", "2026-04-10T15:30:00"),
        ("2026-04-10T00:00:00", "2026-04-10T15:30:00"),
    ]


# --- playback catalog incremental rebuild -------------------------------------------- #


def _sample_payload() -> dict:
    return {
        "last_price": 22500,
        "oc": {
            "22400": {
                "ce": {"last_price": 120, "oi": 1000, "previous_oi": 900, "volume": 500,
                        "implied_volatility": 14.2,
                        "greeks": {"delta": 0.51, "gamma": 0.01, "theta": -4.5, "vega": 7.1},
                        "top_bid_price": 119, "top_ask_price": 121},
                "pe": {"last_price": 110, "oi": 950, "previous_oi": 925, "volume": 430,
                        "implied_volatility": 15.4,
                        "greeks": {"delta": -0.49, "gamma": 0.011, "theta": -4.1, "vega": 6.8},
                        "top_bid_price": 109, "top_ask_price": 111},
            }
        },
    }


def _store(repo, group: str, ts: str, expiry: str = "2026-04-30") -> None:
    stamped = stamp_option_rows(
        parse_option_chain_response(_sample_payload(), 13, "IDX_I", expiry), group, timestamp=ts
    )
    spec = RequestSpec(asset_id=13, segment_code="IDX_I", expiry=expiry,
                       display_name="Nifty", lot_size=65, strike_size=50)
    repo.insert_option_rows(
        build_enhanced_option_rows(stamped, request_spec=spec, risk_free_rate=None, market_volatility=None)
    )


class _CountingMarketRepo(MarketDataRepository):
    def __init__(self, path):
        super().__init__(path)
        self.bounds_calls = 0
        self.since_calls: list[int] = []

    def fetch_group_id_bounds(self):
        self.bounds_calls += 1
        return super().fetch_group_id_bounds()

    def list_fetch_group_pair_summaries_since(self, min_row_id):
        self.since_calls.append(min_row_id)
        return super().list_fetch_group_pair_summaries_since(min_row_id)


def test_catalog_unchanged_bounds_serve_cache_without_rescan(tmp_path: Path):
    repo = _CountingMarketRepo(tmp_path / "market.db")
    _store(repo, "group-1", "2026-04-10T03:45:00+00:00")
    svc = HistoricalReplayService(repo, AssetRepository(ASSETS_CSV))

    assert svc.list_playback_dates("13|IDX_I", "2026-04-30") == ["2026-04-10"]
    assert repo.since_calls == [0]  # one cold full build

    svc.list_playback_dates("13|IDX_I", "2026-04-30")
    svc.list_playback_assets()
    assert repo.since_calls == [0]  # bounds unchanged → cache hit, no further scans


def test_catalog_new_group_appended_via_incremental_scan(tmp_path: Path):
    repo = _CountingMarketRepo(tmp_path / "market.db")
    _store(repo, "group-1", "2026-04-10T03:45:00+00:00")
    svc = HistoricalReplayService(repo, AssetRepository(ASSETS_CSV))
    svc.list_playback_dates("13|IDX_I", "2026-04-30")  # cold build → since(0)

    _store(repo, "group-2", "2026-04-11T03:45:00+00:00")
    dates = svc.list_playback_dates("13|IDX_I", "2026-04-30")

    assert dates == ["2026-04-11", "2026-04-10"]
    assert repo.since_calls[-1] > 0  # incremental — scanned only id > previous max, not a full rebuild


def test_catalog_drops_pruned_group(tmp_path: Path):
    repo = _CountingMarketRepo(tmp_path / "market.db")
    _store(repo, "group-1", "2026-04-10T03:45:00+00:00")
    _store(repo, "group-2", "2026-04-11T03:45:00+00:00")
    svc = HistoricalReplayService(repo, AssetRepository(ASSETS_CSV))
    assert svc.list_playback_dates("13|IDX_I", "2026-04-30") == ["2026-04-11", "2026-04-10"]

    repo.prune_older_than("2026-04-11T00:00:00")  # deletes group-1's rows only
    dates = svc.list_playback_dates("13|IDX_I", "2026-04-30")

    assert dates == ["2026-04-11"]  # pruned group's date is gone (entry dropped via max_row_id < min_id)
