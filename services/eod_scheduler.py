"""Server-side EOD scheduler — the daily 15:30 IST snapshot → prune → token-renewal trigger.

This replaces the old browser-`dcc.Interval`-driven trigger. `dcc.Interval` is a JavaScript timer in
the browser tab, so browsers throttle it when the tab is backgrounded (≈1/min after 5 min) and stop it
entirely when the tab is closed/discarded — which silently skipped the whole EOD pipeline on any day
the dashboard tab wasn't open-and-ticking during that one minute. A process-resident Python thread is
invisible to the browser's timer throttling, so it fires reliably as long as the app process is up
(which it always is at 15:30).

The thread owns nothing UI: it runs the pipeline and writes its status into a small shared
``_EodRuntimeState`` on the services container. The browser keeps only a read-only status reflector
(``features/automation``) and a live-fetch guard (``features/live_core``), both of which *read* that
shared state — they never decide or run EOD. Automation lives on exactly one side: the server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
import threading
from zoneinfo import ZoneInfo

from services.runtime import RuntimeServices
from services.token_renewal_service import renew_access_token


IST = ZoneInfo("Asia/Kolkata")

# EOD progress logger — set to INFO in app._configure_logging so its lines surface in the terminal
# and the daily-rotated log file, data/logs/onechain.log (a healthy run visibly progresses instead
# of looking hung, and the run's record survives the terminal session).
_EOD_LOGGER = logging.getLogger("onechain.eod")

# Trigger window (IST): the scheduler fires once between 15:30 and 15:35 inclusive (the 15:35 minute
# counts), polling every ~60 s. The window is wider than the poll so the trigger is never stepped over;
# the dedup latch stops the several in-window polls from each launching a duplicate run.
_WINDOW_START = time(15, 30)
_WINDOW_END = time(15, 36)  # exclusive — covers 15:30:00 .. 15:35:59

# market.db rolling-cache retention. snapshot.db is the permanent record and is NEVER pruned (its
# options_data feeds the long-window historical metrics, which need up to ~a year of history).
RETENTION_DAYS = 7


@dataclass
class _EodRuntimeState:
    """Shared state read by the scheduler thread (writer), the status reflector, and the fetch guard."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    last_run_date: str | None = None   # IST date (YYYY-MM-DD) a run was started — the dedup latch
    eod_in_progress: bool = False      # True only while the pipeline runs — the live-fetch guard flag
    last_status: str | None = None


def get_or_create_eod_state(services: RuntimeServices) -> _EodRuntimeState:
    """Return the single shared EOD state, creating it on first access. Stored on the services
    container so the scheduler thread, the reflector callback, and the live-fetch guard all see the
    same object."""
    state = getattr(services, "_eod_runtime_state", None)
    if state is None:
        state = _EodRuntimeState()
        setattr(services, "_eod_runtime_state", state)
    return state


def _prune_retention(services: RuntimeServices, *, now: datetime | None = None) -> str:
    """Prune market.db (the rolling 7-day cache) and VACUUM it. snapshot.db is the permanent record
    (options_data + historical_metrics + collection_status) and is **never pruned**. Returns a
    human-readable summary line."""
    reference = now or datetime.now(IST).replace(tzinfo=None)
    cutoff_live = (reference - timedelta(days=RETENTION_DAYS)).isoformat()
    market = services.market_data_repository.prune_older_than(cutoff_live)
    saved = services.saved_moment_repository.prune_older_than(cutoff_live)
    services.market_data_repository.vacuum()
    total = sum(market.values()) + saved
    return f"Retention prune: {total} row(s) removed."


def should_run_eod_now(
    now_ist: datetime,
    *,
    already_ran_today: bool,
    holidays: frozenset[date],
) -> bool:
    """Pure trigger decision: a weekday that isn't a market holiday, inside the 15:30–15:35 window,
    and not already run today. The dedup (``already_ran_today``) and DB-authority checks are supplied
    by the loop; this stays pure for unit tests."""
    if already_ran_today:
        return False
    if now_ist.weekday() >= 5:
        return False
    if now_ist.date() in holidays:
        return False
    return _WINDOW_START <= now_ist.time() < _WINDOW_END


def run_eod_pipeline(
    services: RuntimeServices,
    state: _EodRuntimeState,
    *,
    now: datetime | None = None,
) -> None:
    """Run the EOD pipeline once: snapshot collection → market.db prune → Dhan token renewal.

    Sets the dedup latch (``last_run_date``) and the ``eod_in_progress`` guard flag **before** any
    work, logs each step to the EOD logger, and never raises (each step is individually guarded so one
    failure doesn't abort the rest or kill the scheduler)."""
    today = (now or datetime.now(IST)).date().isoformat()
    with state.lock:
        state.last_run_date = today   # latch BEFORE work so in-window re-polls can't double-run
        state.eod_in_progress = True

    if services.snapshot_collection_service is None:
        summary = "15:30 EOD skipped: snapshot service unavailable (missing credentials)."
        _EOD_LOGGER.info(summary)
        with state.lock:
            state.eod_in_progress = False
            state.last_status = summary
        return

    try:
        try:
            result = services.snapshot_collection_service.collect_once(log=_EOD_LOGGER.info)
            summary = (
                f"15:30 snapshot run {result.status}: {result.pairs_processed} pair(s), "
                f"{result.rows_written} row(s)."
            )
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            summary = f"15:30 snapshot run failed: {exc}"
            _EOD_LOGGER.info(summary)
        try:
            prune_summary = _prune_retention(services, now=now.replace(tzinfo=None) if now else None)
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            prune_summary = f"Retention prune failed: {exc}"
        _EOD_LOGGER.info(prune_summary)
        summary = f"{summary} {prune_summary}"
        # After the snapshot + prune, renew the Dhan token for the next day's manual start.
        try:
            token = renew_access_token(services.api_service, services.env_file_path)
            token_summary = "Token renewed." if token else "Token renewal failed (see logs)."
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            token_summary = f"Token renewal error: {exc}"
        _EOD_LOGGER.info(token_summary)
        summary = f"{summary} {token_summary}"
    finally:
        # One closing line at completion — snapshot + prune + token statuses in a single "done"
        # message (each step also logs its own line above; the success-path snapshot summary appears
        # only here). Guaranteed set by now (the no-credentials path returns before the try).
        _EOD_LOGGER.info("EOD run complete — %s", summary)
        with state.lock:
            state.eod_in_progress = False
            state.last_status = summary


def _eod_loop(
    services: RuntimeServices,
    state: _EodRuntimeState,
    stop_event: threading.Event,
    *,
    poll_seconds: float = 60.0,
) -> None:
    """The scheduler loop. Polls every ``poll_seconds``; runs the pipeline **inline** when due (the
    loop *is* the background thread, so an inline run gives single-flight for free). Each iteration is
    fully guarded so a transient error (DB locked, API hiccup) never kills the scheduler — the exact
    silent-death failure mode this whole change exists to fix."""
    while not stop_event.is_set():
        try:
            now_ist = datetime.now(IST)
            today = now_ist.date().isoformat()
            with state.lock:
                latched = state.last_run_date == today
            # Only consult snapshot.db (the cross-restart authority) when inside the window — avoids a
            # per-minute query all day; outside the window should_run_eod_now short-circuits anyway.
            in_window = _WINDOW_START <= now_ist.time() < _WINDOW_END
            already = latched or (in_window and services.snapshot_repository.has_collection_for_date(today))
            if should_run_eod_now(now_ist, already_ran_today=already, holidays=services.holidays):
                run_eod_pipeline(services, state, now=now_ist)
        except Exception:  # pragma: no cover - the loop must never die
            _EOD_LOGGER.exception("EOD scheduler loop iteration failed; continuing")
        stop_event.wait(poll_seconds)


def start_eod_scheduler(
    services: RuntimeServices,
    state: _EodRuntimeState | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Start the daemon scheduler thread. Called once from ``app.main()`` (never ``create_app``, so
    tests / validate.py don't spawn it). Returns the thread + its stop Event."""
    state = state or get_or_create_eod_state(services)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_eod_loop,
        args=(services, state, stop_event),
        name="onechain-eod-scheduler",
        daemon=True,
    )
    thread.start()
    return thread, stop_event
