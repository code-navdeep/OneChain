from __future__ import annotations

from datetime import datetime, timedelta

from services.vix_service import VixService


NOW = datetime(2026, 6, 8, 14, 30, 0)  # naive wall-clock IST


class _FakeVixRepo:
    """Minimal stand-in: vix_status only reads latest_vix_row() (the newest row, LIMIT 1).
    Rows are held DESC by timestamp, mirroring the real repository's ordering."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows or []

    def latest_vix_row(self) -> dict | None:
        return self._rows[0] if self._rows else None


def _row(value: float, ts: datetime) -> dict:
    return {"value": value, "timestamp": ts.replace(tzinfo=None).isoformat()}


def test_live_when_today_row_after_session_start_regardless_of_age():
    # A real value fetched today after 09:15 is used for the whole session, however old — a real
    # value always beats the static default. NOW=14:30; a 30-min-old and a 2-hour-old (12:30, still
    # after 09:15) row both qualify as live.
    for ts in (NOW - timedelta(minutes=30), NOW - timedelta(hours=2)):
        service = VixService(_FakeVixRepo([_row(13.2, ts)]), default_vix=12.0)
        status = service.vix_status(now=NOW)
        assert status.state == "live"
        assert status.value == 13.2
        assert status.timestamp is not None
        assert status.is_default is False


def test_pre_session_open_row_is_discarded():
    # A today row stamped BEFORE 09:15 does not count → default once the grace has elapsed,
    # unavailable while still within it.
    pre_open = NOW.replace(hour=9, minute=0, second=0, microsecond=0)  # 09:00 today
    elapsed = VixService(_FakeVixRepo([_row(13.2, pre_open)]), default_vix=12.0)
    elapsed._mark_attempt(NOW - timedelta(minutes=6))
    assert elapsed.vix_status(now=NOW).state == "default"
    within = VixService(_FakeVixRepo([_row(13.2, pre_open)]), default_vix=12.0)
    within._mark_attempt(NOW - timedelta(minutes=2))
    assert within.vix_status(now=NOW).state == "unavailable"


def test_latest_post_session_row_wins():
    # DESC ordering → the newest post-09:15 row is the carried value.
    rows = [_row(14.1, NOW - timedelta(minutes=10)), _row(13.2, NOW - timedelta(hours=3))]
    service = VixService(_FakeVixRepo(rows), default_vix=12.0)
    status = service.vix_status(now=NOW)
    assert status.state == "live"
    assert status.value == 14.1


def test_unavailable_when_no_vix_today_and_within_grace():
    service = VixService(_FakeVixRepo([]), default_vix=12.0)
    service._mark_attempt(NOW - timedelta(minutes=2))
    status = service.vix_status(now=NOW)
    assert status.state == "unavailable"
    assert status.value is None


def test_default_when_no_vix_today_and_grace_elapsed():
    service = VixService(_FakeVixRepo([]), default_vix=12.0)
    service._mark_attempt(NOW - timedelta(minutes=6))
    status = service.vix_status(now=NOW)
    assert status.state == "default"
    assert status.value == 12.0


def test_unavailable_when_grace_elapsed_but_no_default_configured():
    service = VixService(_FakeVixRepo([]), default_vix=None)
    service._mark_attempt(NOW - timedelta(minutes=6))
    status = service.vix_status(now=NOW)
    assert status.state == "unavailable"
    assert status.value is None


def test_yesterday_row_does_not_count_as_live():
    rows = [_row(13.2, NOW - timedelta(days=1))]
    # within grace → still unavailable (yesterday's row is not "today")
    within = VixService(_FakeVixRepo(rows), default_vix=12.0)
    within._mark_attempt(NOW - timedelta(minutes=2))
    assert within.vix_status(now=NOW).state == "unavailable"
    # grace elapsed → default, not the stale yesterday value
    elapsed = VixService(_FakeVixRepo(rows), default_vix=12.0)
    elapsed._mark_attempt(NOW - timedelta(minutes=6))
    assert elapsed.vix_status(now=NOW).state == "default"


def test_first_attempt_anchor_resets_across_days():
    service = VixService(_FakeVixRepo([]), default_vix=12.0)
    service._mark_attempt(datetime(2026, 6, 7, 9, 16, 0))  # prior day
    service._mark_attempt(NOW)  # new day → anchor moves to NOW
    # only a moment since today's first attempt → within grace
    assert service.vix_status(now=NOW + timedelta(minutes=2)).state == "unavailable"
