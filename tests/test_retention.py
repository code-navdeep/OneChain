from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from services.eod_scheduler import _prune_retention
from repositories import MarketDataRepository, SavedMomentRepository, SnapshotRepository


IST = ZoneInfo("Asia/Kolkata")
OLD_TS = "2026-01-01T09:15:00"
NEW_TS = "2026-06-10T09:15:00"
CUTOFF = "2026-03-01T00:00:00"


def _option_row(timestamp: str, strike: float) -> dict:
    """An enriched per-contract row (lot-scaled counts + stored exposures)."""
    return {
        "timestamp": timestamp,
        "fetch_group_id": "grp",
        "underlying_asset_id": 13,
        "segment_code": "IDX_I",
        "expiry": "2026-04-30",
        "strike": strike,
        "option_type": "CE",
        "last_price": 100.0,
        "bid_price": 99.0,
        "ask_price": 101.0,
        "underlying_price": 22000.0,
        "iv": 12.5,
        "delta": 0.5,
        "gamma": 0.01,
        "theta": -5.0,
        "vega": 11.0,
        "oi_lots": 15.0,
        "previous_oi_lots": 14.0,
        "oi_change_lots": 1.0,
        "volume_lots": 7.0,
        "straddle_oi_lots": 30.0,
        "gamma_exposure": -200.0,
        "volatility_exposure": 100.0,
    }


def test_market_data_prune_deletes_old_keeps_new(tmp_path: Path):
    repo = MarketDataRepository(tmp_path / "market.db")
    repo.insert_option_rows([_option_row(OLD_TS, 21000), _option_row(NEW_TS, 22000)])
    # a freshly written VIX row (now()) must survive an old cutoff
    repo.save_vix_row(13.5, {"previous_close": 13.0})

    deleted = repo.prune_older_than(CUTOFF)

    assert deleted["option_rows"] == 1
    remaining = repo.list_rows_for_fetch_group("grp")
    assert [row["timestamp"] for row in remaining] == [NEW_TS]
    assert len(repo.list_vix_rows()) == 1  # recent VIX row not pruned


def test_saved_moment_prune_deletes_old_keeps_new(tmp_path: Path):
    repo = SavedMomentRepository(tmp_path / "market.db")
    repo.save_moment(13, "IDX_I", "2026-04-30", label="old", timestamp=OLD_TS)
    repo.save_moment(13, "IDX_I", "2026-04-30", label="new", timestamp=NEW_TS)

    deleted = repo.prune_older_than(CUTOFF)

    assert deleted == 1
    remaining = repo.list_moments(13, "IDX_I", "2026-04-30")
    assert [row["timestamp"] for row in remaining] == [NEW_TS]


def test_vacuum_runs_and_db_stays_queryable(tmp_path: Path):
    repo = MarketDataRepository(tmp_path / "market.db")
    repo.insert_option_rows([_option_row(NEW_TS, 22000)])
    repo.prune_older_than(CUTOFF)
    repo.vacuum()  # must not raise
    assert len(repo.list_rows_for_fetch_group("grp")) == 1


def test_prune_retention_prunes_market_only_snapshot_is_permanent(tmp_path: Path):
    market = MarketDataRepository(tmp_path / "market.db")
    saved = SavedMomentRepository(tmp_path / "market.db")
    snapshot = SnapshotRepository(tmp_path / "snapshot.db")

    now = datetime(2026, 6, 1, tzinfo=IST).replace(tzinfo=None)
    old = "2026-01-01T09:15:00"
    recent = "2026-05-30T09:15:00"
    market.insert_option_rows([_option_row(old, 21000), _option_row(recent, 22000)])
    saved.save_moment(13, "IDX_I", "2026-04-30", label="old", timestamp=old)
    snapshot.insert_option_rows([_option_row(old, 21000)])  # a months-old EOD snapshot row

    services = SimpleNamespace(
        market_data_repository=market,
        saved_moment_repository=saved,
        snapshot_repository=snapshot,
    )

    summary = _prune_retention(services, now=now)

    assert "row(s) removed" in summary
    # market.db is the rolling cache: old rows pruned, recent kept.
    assert [row["timestamp"] for row in market.list_rows_for_fetch_group("grp")] == [recent]
    assert saved.list_moments(13, "IDX_I", "2026-04-30") == []
    # snapshot.db is the permanent record — the months-old row is NOT pruned.
    assert len(snapshot.list_option_rows()) == 1
