"""Tests for the server-side EOD scheduler trigger + dedup authority."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from repositories import SnapshotRepository
from services.eod_scheduler import (
    _EodRuntimeState,
    run_eod_pipeline,
    should_run_eod_now,
)
from services.snapshot_collection_service import SnapshotRunResult


IST = ZoneInfo("Asia/Kolkata")
NO_HOLIDAYS: frozenset = frozenset()
# 2026-06-29 is a Monday; 2026-06-27 a Saturday.
MONDAY = lambda h, m: datetime(2026, 6, 29, h, m, 0, tzinfo=IST)


# --- should_run_eod_now (pure trigger decision) -------------------------------------------------- #


def test_window_fires_across_the_full_1530_to_1535_window():
    for minute in (30, 31, 33, 35):
        assert should_run_eod_now(MONDAY(15, minute), already_ran_today=False, holidays=NO_HOLIDAYS) is True


def test_window_rejects_outside_1530_to_1535():
    assert should_run_eod_now(MONDAY(15, 29), already_ran_today=False, holidays=NO_HOLIDAYS) is False
    assert should_run_eod_now(MONDAY(15, 36), already_ran_today=False, holidays=NO_HOLIDAYS) is False
    assert should_run_eod_now(MONDAY(9, 30), already_ran_today=False, holidays=NO_HOLIDAYS) is False


def test_window_rejects_weekend():
    saturday = datetime(2026, 6, 27, 15, 30, 0, tzinfo=IST)
    assert should_run_eod_now(saturday, already_ran_today=False, holidays=NO_HOLIDAYS) is False


def test_window_rejects_holiday():
    holidays = frozenset({MONDAY(15, 30).date()})
    assert should_run_eod_now(MONDAY(15, 30), already_ran_today=False, holidays=holidays) is False


def test_window_rejects_when_already_ran_today():
    assert should_run_eod_now(MONDAY(15, 30), already_ran_today=True, holidays=NO_HOLIDAYS) is False


# --- run_eod_pipeline (latch + status + guard flag) ---------------------------------------------- #


class _FakeMarket:
    def prune_older_than(self, _cutoff):
        return {"option_rows": 0}

    def vacuum(self):
        pass


class _FakeSaved:
    def prune_older_than(self, _cutoff):
        return 0


class _FakeCollector:
    def collect_once(self, *, log=None):
        return SnapshotRunResult(
            started_at="2026-06-29T15:30:00+05:30",
            completed_at="2026-06-29T15:31:05+05:30",
            groups_processed=1,
            assets_processed=1,
            pairs_processed=2,
            rows_written=4,
            status="success",
            message="ok",
        )


def _fake_services(tmp_path: Path, *, collector) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_collection_service=collector,
        market_data_repository=_FakeMarket(),
        saved_moment_repository=_FakeSaved(),
        api_service=None,  # renew_access_token returns None → "Token renewal failed", no .env write
        env_file_path=tmp_path / ".env",
    )


def test_run_eod_pipeline_latches_date_clears_flag_and_records_status(tmp_path: Path):
    state = _EodRuntimeState()
    services = _fake_services(tmp_path, collector=_FakeCollector())
    now = datetime(2026, 6, 29, 15, 30, 0, tzinfo=IST)

    run_eod_pipeline(services, state, now=now)

    assert state.last_run_date == "2026-06-29"   # dedup latch set
    assert state.eod_in_progress is False        # guard flag cleared after the run
    assert "success" in state.last_status
    assert "2 pair(s)" in state.last_status
    assert "Retention prune" in state.last_status


def test_run_eod_pipeline_skips_cleanly_without_snapshot_service(tmp_path: Path):
    state = _EodRuntimeState()
    services = _fake_services(tmp_path, collector=None)
    services.snapshot_collection_service = None
    now = datetime(2026, 6, 29, 15, 30, 0, tzinfo=IST)

    run_eod_pipeline(services, state, now=now)

    assert state.last_run_date == "2026-06-29"   # latched so it doesn't retry every minute
    assert state.eod_in_progress is False
    assert "skipped" in state.last_status.lower()


# --- has_collection_for_date (cross-restart dedup authority) -------------------------------------- #


def test_has_collection_for_date_true_only_after_a_success(tmp_path: Path):
    repo = SnapshotRepository(tmp_path / "snapshot.db")
    repo.record_collection_status(
        asset_id=13,
        segment_code="IDX_I",
        status="success",
        message="ok",
        timestamp="2026-06-29T15:30:05",
    )
    assert repo.has_collection_for_date("2026-06-29") is True
    assert repo.has_collection_for_date("2026-06-28") is False


def test_has_collection_for_date_ignores_non_success_rows(tmp_path: Path):
    repo = SnapshotRepository(tmp_path / "snapshot.db")
    repo.record_collection_status(
        asset_id=13,
        segment_code="IDX_I",
        status="error",
        message="boom",
        timestamp="2026-06-29T15:30:05",
    )
    assert repo.has_collection_for_date("2026-06-29") is False
