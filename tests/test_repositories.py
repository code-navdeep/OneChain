from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from repositories import (
    AssetRepository,
    MarketDataRepository,
    SavedMomentRepository,
    SnapshotRepository,
)

IST = ZoneInfo("Asia/Kolkata")


def _enriched_row(**overrides) -> dict:
    """A full enriched per-contract row (lot-scaled counts + stored exposures)."""
    base = {
        "timestamp": "2026-04-10T09:15:00",
        "fetch_group_id": "group-1",
        "underlying_asset_id": 13,
        "segment_code": "IDX_I",
        "expiry": "2026-04-30",
        "strike": 22400.0,
        "option_type": "CE",
        "last_price": 100.0,
        "bid_price": 99.0,
        "ask_price": 101.0,
        "underlying_price": 22500.0,
        "iv": 14.0,
        "delta": 0.5,
        "gamma": 0.01,
        "theta": -4.0,
        "vega": 7.0,
        "oi_lots": 15.0,
        "previous_oi_lots": 14.0,
        "oi_change_lots": 1.0,
        "volume_lots": 5.0,
        "straddle_oi_lots": 30.0,
        "gamma_exposure": -200.0,
        "volatility_exposure": 100.0,
    }
    base.update(overrides)
    return base


def test_asset_repository_uses_segment_safe_lookup(tmp_path: Path):
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "\n".join(
            [
                "asset_id,asset_name,exchange,segment_code,strike_size,lot_size,display_name",
                "25,BankNifty,NSE,IDX_I,100,35,BANKNIFTY",
                "25,AdaniEnt,NSE,NSE_EQ,20,1,ADANIENT",
            ]
        ),
        encoding="utf-8",
    )

    repo = AssetRepository(csv_path)
    assert repo.get_asset(25, "IDX_I").display_name == "BANKNIFTY"
    assert repo.get_asset(25, "NSE_EQ").display_name == "ADANIENT"


def test_market_data_repository_crud(tmp_path: Path):
    repo = MarketDataRepository(tmp_path / "market.db")
    inserted = repo.insert_option_rows([_enriched_row()])
    assert inserted == 1
    rows = repo.list_rows_for_fetch_group("group-1")
    assert len(rows) == 1
    assert rows[0]["oi_lots"] == 15.0
    summaries = repo.list_fetch_group_summaries()
    assert summaries[0]["fetch_group_id"] == "group-1"
    pair_summaries = repo.list_fetch_group_pair_summaries()
    assert pair_summaries[0]["fetch_group_id"] == "group-1"
    assert int(pair_summaries[0]["pair_count"]) == 1

    row_id = repo.save_vix_row(14.2, {"previous_close": 13.8})
    assert row_id > 0
    latest_vix = repo.list_vix_rows()[0]
    assert latest_vix["value"] == 14.2
    assert datetime.fromisoformat(str(latest_vix["timestamp"])).tzinfo is None  # naive wall-clock IST


def test_market_data_repository_pair_report_round_trip(tmp_path: Path):
    repo = MarketDataRepository(tmp_path / "market.db")
    record = {
        "timestamp": "2026-04-10T09:15:00",
        "fetch_group_id": "group-1",
        "underlying_asset_id": 13,
        "segment_code": "IDX_I",
        "expiry": "2026-04-30",
        "underlying_price": 22500.0,
        "lot_size": 65,
        "strike_size": 50,
        "total_oi_lots": 30.0,
        "gex_market_regime": "Supportive",
        "max_pain_strike": 22400.0,
    }
    report_id = repo.insert_pair_report(record)
    assert report_id > 0
    stored = repo.get_pair_report("group-1", 13, "IDX_I", "2026-04-30")
    assert stored is not None
    assert stored["total_oi_lots"] == 30.0
    assert stored["gex_market_regime"] == "Supportive"
    assert stored["max_pain_strike"] == 22400.0


def test_market_data_repository_risk_reversal_round_trip_and_series(tmp_path: Path):
    repo = MarketDataRepository(tmp_path / "market.db")
    base = {
        "underlying_asset_id": 13,
        "segment_code": "IDX_I",
        "expiry": "2026-04-30",
        "underlying_price": 22500.0,
        "lot_size": 65,
        "strike_size": 50,
    }
    # list_risk_reversal_series is today-only (from 09:00 IST) — stamp the rows in today's session.
    today = datetime.now(IST).date().isoformat()
    repo.insert_pair_report({
        **base, "timestamp": f"{today}T09:20:00", "fetch_group_id": "g1",
        "risk_reversal_25": -1.5, "call_iv_25d": 14.0, "put_iv_25d": 15.5,
    })
    repo.insert_pair_report({
        **base, "timestamp": f"{today}T09:25:00", "fetch_group_id": "g2",
        "risk_reversal_25": -1.2, "call_iv_25d": 14.1, "put_iv_25d": 15.3,
    })

    stored = repo.get_pair_report("g2", 13, "IDX_I", "2026-04-30")
    assert stored["risk_reversal_25"] == -1.2
    assert stored["call_iv_25d"] == 14.1
    assert stored["put_iv_25d"] == 15.3

    series = repo.list_risk_reversal_series(13, "IDX_I", "2026-04-30")
    assert [row["risk_reversal_25"] for row in series] == [-1.5, -1.2]  # ascending by time


def test_snapshot_repository_metric_crud(tmp_path: Path):
    repo = SnapshotRepository(tmp_path / "snapshot.db")
    metric_id = repo.save_historical_metric(
        {
            "date": "2026-04-10",
            "underlying_asset_id": 13,
            "segment_code": "IDX_I",
            "rv_22d": 0.21,
            "call_iv_rv_spread_22d": 0.03,
            "put_iv_rv_spread_22d": 0.01,
        }
    )
    assert metric_id > 0
    rows = repo.list_historical_metrics(13, "IDX_I")
    assert len(rows) == 1
    assert rows[0]["rv_22d"] == 0.21
    assert repo.get_latest_historical_metric(13, "IDX_I")["date"] == "2026-04-10"

    status_id = repo.record_collection_status(
        asset_id=13,
        segment_code="IDX_I",
        status="success",
        message="ok",
    )
    assert status_id > 0
    with repo.connection() as conn:
        status_row = conn.execute("SELECT timestamp FROM collection_status WHERE id = ?", (status_id,)).fetchone()
    assert status_row is not None
    assert datetime.fromisoformat(str(status_row["timestamp"])).tzinfo is None  # naive wall-clock IST


def test_snapshot_repository_upsert_handles_wide_metric_payload(tmp_path: Path):
    repo = SnapshotRepository(tmp_path / "snapshot.db")
    repo.save_historical_metric(
        {
            "date": "2026-04-10",
            "underlying_asset_id": 13,
            "segment_code": "IDX_I",
            "rv_22d": 18.5,
            "rv_252d": 16.0,
            "call_iv_percentile_180d": 42.0,
            "put_iv_rv_spread_252d": 7.5,
            "iv_skew_90d": -1.2,
        }
    )
    rows = repo.list_historical_metrics(13, "IDX_I")
    assert len(rows) == 1
    assert rows[0]["rv_252d"] == 16.0
    assert rows[0]["call_iv_percentile_180d"] == 42.0
    assert rows[0]["put_iv_rv_spread_252d"] == 7.5
    assert rows[0]["iv_skew_90d"] == -1.2
    assert rows[0]["iv_skew_22d"] is None  # column exists but wasn't in the payload


def test_snapshot_repository_daily_closes_returns_latest_tick_per_day(tmp_path: Path):
    repo = SnapshotRepository(tmp_path / "snapshot.db")
    repo.insert_option_rows(
        [
            _enriched_row(timestamp="2026-04-10T08:55:00+05:30", fetch_group_id="day-1-a", underlying_price=22500.0),
            _enriched_row(timestamp="2026-04-10T09:15:00+05:30", fetch_group_id="day-1-b", underlying_price=22600.0),
            _enriched_row(timestamp="2026-04-11T09:15:00+05:30", fetch_group_id="day-2", underlying_price=22700.0),
        ]
    )
    closes = repo.list_daily_underlying_closes(13, "IDX_I", limit_days=5)
    assert len(closes) == 2
    assert closes[0]["trade_date"] == "2026-04-10"
    assert closes[0]["underlying_price"] == 22600.0
    assert closes[1]["trade_date"] == "2026-04-11"
    assert closes[1]["underlying_price"] == 22700.0


def test_snapshot_repository_atm_context_returns_all_expiries_per_date(tmp_path: Path):
    # The CM-IV series needs every collected expiry per date (not just the nearest), with the
    # expiry column, and the latest tick per (date, expiry).
    repo = SnapshotRepository(tmp_path / "snapshot.db")
    repo.insert_option_rows(
        [
            _enriched_row(timestamp="2026-04-10T09:15:00", fetch_group_id="a1", expiry="2026-04-17", iv=12.0),
            _enriched_row(timestamp="2026-04-10T15:25:00", fetch_group_id="a2", expiry="2026-04-17", iv=13.0),
            _enriched_row(timestamp="2026-04-10T15:25:00", fetch_group_id="b1", expiry="2026-05-15", iv=18.0),
        ]
    )
    rows = repo.list_daily_atm_context_rows(13, "IDX_I", lookback_days=5)
    assert {row["expiry"] for row in rows} == {"2026-04-17", "2026-05-15"}  # all expiries, not MIN-collapsed
    assert all("date" in row and "expiry" in row for row in rows)
    assert [row["iv"] for row in rows if row["expiry"] == "2026-04-17"] == [13.0]  # latest tick per (date, expiry)


def test_snapshot_repository_latest_rows_use_iso_timestamp_cutoff(tmp_path: Path):
    repo = SnapshotRepository(tmp_path / "snapshot.db")
    today_ist = datetime.now(IST).date()
    before = datetime(today_ist.year, today_ist.month, today_ist.day, 8, 55, 0, tzinfo=IST).replace(tzinfo=None).isoformat()
    after = datetime(today_ist.year, today_ist.month, today_ist.day, 10, 5, 0, tzinfo=IST).replace(tzinfo=None).isoformat()
    repo.insert_option_rows(
        [
            _enriched_row(timestamp=before, fetch_group_id="snap-1", oi_lots=15.0),
            _enriched_row(timestamp=after, fetch_group_id="snap-2", oi_lots=22.0),
        ]
    )
    rows = repo.list_latest_snapshot_rows(13, "IDX_I", expiry="2026-04-30")
    assert len(rows) == 1
    assert rows[0]["oi_lots"] == 15.0


def test_snapshot_repository_latest_rows_support_naive_timestamps(tmp_path: Path):
    repo = SnapshotRepository(tmp_path / "snapshot.db")
    today_ist = datetime.now(IST).date()
    before = f"{today_ist.isoformat()} 08:50:00"
    after = f"{today_ist.isoformat()} 10:10:00"
    repo.insert_option_rows(
        [
            _enriched_row(timestamp=before, fetch_group_id="older-1", option_type="PE", oi_lots=33.0),
            _enriched_row(timestamp=after, fetch_group_id="older-2", option_type="PE", oi_lots=44.0),
        ]
    )
    rows = repo.list_latest_snapshot_rows(13, "IDX_I", expiry="2026-04-30")
    assert len(rows) == 1
    assert rows[0]["oi_lots"] == 33.0


def test_saved_moment_repository_pair_based_crud(tmp_path: Path):
    repo = SavedMomentRepository(tmp_path / "market.db")
    first_id = repo.save_moment(
        asset_id=13,
        segment_code="IDX_I",
        expiry="2026-04-30",
        label="Open",
        save_batch_id="batch-1",
        fetch_group_id="group-1",
        source_mode="live",
        view_kind="pair",
        timestamp="2026-04-10T09:15:00",
    )
    second_id = repo.save_moment(
        asset_id=13,
        segment_code="IDX_I",
        expiry="2026-04-30",
        label="Later",
        save_batch_id="batch-1",
        fetch_group_id="group-2",
        source_mode="playback",
        view_kind="aggregate",
        timestamp="2026-04-10T10:00:00",
    )
    assert first_id > 0
    assert second_id > first_id

    assets = repo.list_assets()
    assert assets == [{"underlying_asset_id": 13, "segment_code": "IDX_I"}]
    expiries = repo.list_expiries(13, "IDX_I")
    assert expiries == ["2026-04-30"]
    moments = repo.list_moments(13, "IDX_I", "2026-04-30")
    assert len(moments) == 2
    assert repo.get_moment(second_id)["fetch_group_id"] == "group-2"
    assert repo.get_moment(second_id)["source_mode"] == "playback"
    assert repo.get_moment(second_id)["view_kind"] == "aggregate"

    repo.delete_moment(first_id)
    remaining = repo.list_moments(13, "IDX_I", "2026-04-30")
    assert len(remaining) == 1
