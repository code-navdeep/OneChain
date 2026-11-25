"""Tests for the server-side live collector (fetch loop moved off the browser)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from services.eod_scheduler import _EodRuntimeState
from services.live_collector import (
    _LiveCollectorState,
    _collector_iteration,
    _market_open_for_collection,
)


IST = ZoneInfo("Asia/Kolkata")
NO_HOLIDAYS: frozenset = frozenset()
# 2026-06-29 is a Monday; 2026-06-27 a Saturday.
MONDAY = lambda h, m: datetime(2026, 6, 29, h, m, 0, tzinfo=IST)


class _FakeLiveFetch:
    """Records fetch_request_group calls (source + fetch_vix flag), returns a distinct group id."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._n = 0

    def fetch_request_group(self, specs, *, source, fetch_vix):
        self._n += 1
        self.calls.append({"source": source, "fetch_vix": fetch_vix, "count": len(specs)})
        return SimpleNamespace(fetch_group_id=f"grp-{self._n}", status_text=f"Fetched {len(specs)}")


def _services(*, live_fetch, holidays=NO_HOLIDAYS, eod_in_progress=False) -> SimpleNamespace:
    eod = _EodRuntimeState()
    eod.eod_in_progress = eod_in_progress
    return SimpleNamespace(live_fetch_service=live_fetch, holidays=holidays, _eod_runtime_state=eod)


# --- _market_open_for_collection (pure gate) ----------------------------------------------------- #


def test_market_open_weekday_before_close():
    assert _market_open_for_collection(MONDAY(11, 0), NO_HOLIDAYS) is True


def test_market_closed_at_and_after_1530():
    assert _market_open_for_collection(MONDAY(15, 30), NO_HOLIDAYS) is False
    assert _market_open_for_collection(MONDAY(16, 0), NO_HOLIDAYS) is False


def test_market_closed_on_weekend():
    saturday = datetime(2026, 6, 27, 11, 0, 0, tzinfo=IST)
    assert _market_open_for_collection(saturday, NO_HOLIDAYS) is False


def test_market_closed_on_holiday():
    holidays = frozenset({MONDAY(11, 0).date()})
    assert _market_open_for_collection(MONDAY(11, 0), holidays) is False


# --- _collector_iteration (one loop step) -------------------------------------------------------- #


def test_pending_one_shot_runs_one_wave_with_vix_then_clears():
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()
    state.request_specs = ["a", "b"]
    state.pending_one_shot = True
    services = _services(live_fetch=fake)

    last_wave, last_vix = _collector_iteration(
        services, state, last_wave=0.0, last_vix=0.0, now_ist=MONDAY(11, 0), monotonic=lambda: 100.0
    )

    assert len(fake.calls) == 1
    assert fake.calls[0]["source"] == "live"
    assert fake.calls[0]["fetch_vix"] is True     # a single fetch always refreshes VIX
    assert state.pending_one_shot is False        # cleared
    assert state.last_fetch_group_id == "grp-1"
    assert last_wave == 100.0 and last_vix == 100.0


def test_continuous_runs_when_cadence_elapsed_and_fetches_vix_when_stale():
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()
    state.request_specs = ["a"]
    state.active = True
    state.cadence_ms = 5000
    services = _services(live_fetch=fake)

    # monotonic=100, last_wave=0 → 100000 ms elapsed ≥ 5000; last_vix=0 → 100 ≥ 60 → VIX too.
    last_wave, last_vix = _collector_iteration(
        services, state, last_wave=0.0, last_vix=0.0, now_ist=MONDAY(11, 0), vix_seconds=60, monotonic=lambda: 100.0
    )
    assert len(fake.calls) == 1
    assert fake.calls[0]["fetch_vix"] is True
    assert last_wave == 100.0 and last_vix == 100.0


def test_continuous_skips_vix_within_the_subcadence():
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()
    state.request_specs = ["a"]
    state.active = True
    state.cadence_ms = 5000
    services = _services(live_fetch=fake)

    # cadence elapsed (10 s) but VIX fetched 10 s ago (< 60 s) → wave without VIX; last_vix unchanged.
    last_wave, last_vix = _collector_iteration(
        services, state, last_wave=90.0, last_vix=90.0, now_ist=MONDAY(11, 0), vix_seconds=60, monotonic=lambda: 100.0
    )
    assert len(fake.calls) == 1
    assert fake.calls[0]["fetch_vix"] is False
    assert last_wave == 100.0 and last_vix == 90.0


def test_continuous_noop_when_cadence_not_elapsed():
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()
    state.request_specs = ["a"]
    state.active = True
    state.cadence_ms = 5000
    services = _services(live_fetch=fake)

    # only 2 s since the last wave (< 5 s cadence) → no fetch.
    _collector_iteration(
        services, state, last_wave=98.0, last_vix=0.0, now_ist=MONDAY(11, 0), monotonic=lambda: 100.0
    )
    assert fake.calls == []


def test_continuous_latches_off_when_market_closed():
    # Continuous auto-refresh (active, no pending) stops at the close / off-session.
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()
    state.request_specs = ["a"]
    state.active = True
    services = _services(live_fetch=fake)

    _collector_iteration(
        services, state, last_wave=0.0, last_vix=0.0, now_ist=MONDAY(16, 0), monotonic=lambda: 100.0
    )
    assert fake.calls == []
    assert state.active is False
    assert "market closed" in (state.last_status or "").lower()


def test_one_shot_runs_even_when_market_closed():
    # A manual one-shot Fetch runs regardless of market hours (after-close case).
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()
    state.request_specs = ["a"]
    state.pending_one_shot = True
    services = _services(live_fetch=fake)

    _collector_iteration(
        services, state, last_wave=0.0, last_vix=0.0, now_ist=MONDAY(16, 0), monotonic=lambda: 100.0
    )
    assert len(fake.calls) == 1 and fake.calls[0]["fetch_vix"] is True  # it fetched (with VIX)
    assert state.pending_one_shot is False


def test_one_shot_runs_on_weekend():
    # ...and on a non-trading day (Saturday) — the essential "Fetch works any day/time".
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()
    state.request_specs = ["a"]
    state.pending_one_shot = True
    services = _services(live_fetch=fake)
    saturday = datetime(2026, 6, 27, 11, 0, 0, tzinfo=IST)

    _collector_iteration(
        services, state, last_wave=0.0, last_vix=0.0, now_ist=saturday, monotonic=lambda: 100.0
    )
    assert len(fake.calls) == 1
    assert state.pending_one_shot is False


def test_skips_while_eod_in_progress_without_latching_off():
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()
    state.request_specs = ["a"]
    state.active = True
    services = _services(live_fetch=fake, eod_in_progress=True)

    _collector_iteration(
        services, state, last_wave=0.0, last_vix=0.0, now_ist=MONDAY(15, 20), monotonic=lambda: 100.0
    )
    assert fake.calls == []
    assert state.active is True   # merely stood down; resumes after EOD


def test_noop_when_nothing_committed():
    fake = _FakeLiveFetch()
    state = _LiveCollectorState()   # active=False, pending=False, no specs
    services = _services(live_fetch=fake)

    _collector_iteration(
        services, state, last_wave=0.0, last_vix=0.0, now_ist=MONDAY(11, 0), monotonic=lambda: 100.0
    )
    assert fake.calls == []
