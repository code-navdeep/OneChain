"""Server-side live collector — the throttle-proof live option-chain fetch loop.

Mirrors ``services/eod_scheduler.py``. This moves the live fetch loop out of the browser
``fetch_live_data`` callback (a ``dcc.Interval``-driven JS timer that browsers throttle when a tab is
backgrounded and stop when it is closed) into a process-resident daemon thread — so once the user
*commits* a selection, collection continues regardless of tab state. The browser keeps only a thin
**commit** callback (writes the request set into the shared state) and a **render poll** (rebuilds
the dashboard from the newest stored fetch group).

Selection is a discrete **commit**, not a continuously-tracked value: the committed request set +
cadence live in ``_LiveCollectorState`` on the services container; the thread fetches that set until
the user stops it or the 15:30 IST EOD trigger (== NSE close; the universe is NSE-only) stops it.

The thread owns nothing UI: it reads the committed set + writes the newest ``fetch_group_id`` into
the shared state. The browser commit callback (writer) and render poll (reader) touch that same
object in-process (Dash callbacks run in the Flask process), so there is no IPC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
import logging
import threading
import time as _time
from zoneinfo import ZoneInfo

from adapters.json_state_cache import read_json_state, write_json_state
from analytics.alerts import apply_live_wave
from analytics.hover_snapshot import build_hover_snapshot
from analytics.interpretor import interpret_pair
from analytics.rate_alerts import compute_rate_crossings, detect_rate_clusters, update_ring
from config import (
    INTERPRETOR_CONFIG,
    INTERPRETOR_ENABLED,
    RATE_ALERT_FLOORS,
    RATE_ALERT_SOUND_COOLDOWN_S,
    TELEGRAM_FORWARD_CONFIG,
)
from domain.requests import RequestSpec
from services.eod_scheduler import get_or_create_eod_state
from services.runtime import RuntimeServices
from services.telegram_format import format_tier1_message, format_tier2_message


IST = ZoneInfo("Asia/Kolkata")

# Live collector logger — promoted to INFO by app._configure_logging (root stays WARNING), so any
# INFO progress lines added here surface in the terminal and the daily-rotated log file
# (data/logs/onechain.log). Currently only guarded-failure `.exception` lines are emitted.
_LOGGER = logging.getLogger("onechain.live")

# NSE market close == the 15:30 IST EOD trigger. The universe is NSE-only (verified: no MCX rows in
# assets_select.csv), so a single global stop at 15:30 is market close; past it the collector latches
# off (the EOD pipeline runs + the token is rotated, so post-close fetching is pointless).
_MARKET_CLOSE = dtime(15, 30)

# The A2 hover snapshot is captured within the 09:20 IST window [09:20:00, 09:21:00) — the earliest wave
# in that minute wins (frozen), for whatever pairs are fetched during it. 09:20 means 09:20: if no fetch
# lands in the window for a pair, nothing is captured (never a later chain masquerading as 09:20).
_HOVER_0920 = dtime(9, 20)
_HOVER_0920_END = dtime(9, 21)

# Default continuous cadence when a commit carries none (auto-refresh "off" → a single fetch).
_DEFAULT_CADENCE_MS = 5000
# VIX sub-fetch cadence during continuous collection (seconds) — a wave with fetch_vix=True at most
# this often, so VIX is never fetched on an idle thread nor on every chain tick.
_DEFAULT_VIX_SECONDS = 60
# Loop poll granularity (seconds) — how often the thread re-checks cadence when not woken early.
_POLL_SECONDS = 1.0


# --- Shared daily-JSON state helpers ------------------------------------------------------------- #
# Both the interpretor (`interpretor.json`) and the alert journal (`alerts_cache.json`) keep a per-day
# working copy on the services container, reloaded once on the first wave and wiped at the IST date
# rollover. These three helpers factor out that shared "{date, <payload>}" persistence dance so each
# `_update_*` wires its own payload in/out (adapters/json_state_cache does the atomic file I/O).

def load_daily_payload(path, today: str) -> dict:
    """The persisted payload (all keys except ``date``) from a daily-JSON file, but only when its stored
    ``date`` is ``today`` — else ``{}`` (a stale/absent file starts the day fresh). Mid-day-restart
    reload of today's state."""
    stored = read_json_state(path)
    if stored.get("date") != today:
        return {}
    return {key: value for key, value in stored.items() if key != "date"}


def daily_is_stale(state_date, today: str) -> bool:
    """Has the IST trading day rolled over since ``state_date`` was set?"""
    return state_date != today


def persist_daily_state(path, today: str, payload: dict) -> None:
    """Write ``{date: today, **payload}`` atomically (the daily-JSON convention)."""
    write_json_state(path, {"date": today, **payload})


@dataclass
class _LiveCollectorState:
    """Shared state: the browser commit callback (writer) + the render poll (reader) + the thread."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    wake: threading.Event = field(default_factory=threading.Event)  # set by commit → fetch instantly
    active: bool = False                       # continuous collection on/off
    request_specs: list[RequestSpec] = field(default_factory=list)  # the committed set (frozen, thread-safe)
    cadence_ms: int = _DEFAULT_CADENCE_MS
    pending_one_shot: bool = False             # a single Fetch wave is queued
    last_fetch_group_id: str | None = None     # newest wave's id — the render poll's change signal
    last_fetch_ts: str | None = None           # newest wave's IST wall-clock ISO
    last_status: str | None = None
    vix_last_ts: str | None = None             # last VIX sub-fetch — bumps the browser VIX heartbeat


def get_or_create_live_collector_state(services: RuntimeServices) -> _LiveCollectorState:
    """Return the single shared collector state, creating it on first access (mirrors
    ``get_or_create_eod_state``). Materialized single-threaded in ``create_app`` so the thread + the
    commit/render callbacks never race to lazily create two copies."""
    state = getattr(services, "_live_collector_state", None)
    if state is None:
        state = _LiveCollectorState()
        setattr(services, "_live_collector_state", state)
    return state


@dataclass
class _RateRingState:
    """Server-side Tier-2 rate ring — per-minute OI/IV/volume samples per contract, keyed by
    ``view_key``. Written by the collector (``_run_wave`` → ``_update_rate_rings``), read under the
    lock by the chain-marker / sound / heatmap callbacks via ``analytics.rate_alerts``. In-memory only
    (re-warms over ≤30 min on restart); ±32-capped per pair so it stays small."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    rings: dict = field(default_factory=dict)  # view_key -> {contract_id: deque[(minute, oi, iv, vol)]}
    forward_keys: dict = field(default_factory=dict)  # (view_key,rule,direction) -> last Telegram-forward monotonic ts


def get_or_create_rate_ring_state(services: RuntimeServices) -> _RateRingState:
    """The single shared rate ring, created on first access (mirrors the collector-state pattern).
    Materialized single-threaded in ``create_app``."""
    state = getattr(services, "_rate_ring_state", None)
    if state is None:
        state = _RateRingState()
        setattr(services, "_rate_ring_state", state)
    return state


@dataclass
class _InterpretorState:
    """Server-side intraday OI-structure interpretor state — per-pair, keyed by ``view_key``. Written by
    the collector (``_run_wave`` → ``_update_interpretor``), read under the lock by the chain/panel
    display callbacks. Mirrors ``interpretor.json`` (a top-level trading ``date`` + a ``pairs`` map),
    loaded once on the first wave so a mid-day restart reloads today's state; wiped fresh each day."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    date: str | None = None                     # the trading day `pairs` belongs to (IST YYYY-MM-DD)
    pairs: dict = field(default_factory=dict)   # view_key -> per-pair interpretor state (JSON-safe, persisted)
    reads: dict = field(default_factory=dict)   # view_key -> latest display read (in-memory only, NOT persisted)
    loaded: bool = False                        # interpretor.json loaded into `pairs` on the first wave


def get_or_create_interpretor_state(services: RuntimeServices) -> _InterpretorState:
    """The single shared interpretor state, created on first access (mirrors the rate-ring pattern).
    Materialized single-threaded in ``create_app``."""
    state = getattr(services, "_interpretor_state", None)
    if state is None:
        state = _InterpretorState()
        setattr(services, "_interpretor_state", state)
    return state


@dataclass
class _AlertState:
    """Server-side Tier-1 alert journal — the working ``{date, baselines, alerts}`` copy, mirrored to
    ``alerts_cache.json``. Written by the collector (``_run_wave`` → ``_update_alerts``), read under the
    lock by the browser render-only mirror. Loaded once on the first wave (mid-day restart reload) and
    wiped fresh each trading day via the shared daily-state helpers. Mirrors ``_InterpretorState``."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    journal: dict = field(default_factory=dict)  # {date, baselines, alerts} — persisted to alerts_cache.json
    loaded: bool = False                         # alerts_cache.json loaded into `journal` on the first wave


def get_or_create_alert_state(services: RuntimeServices) -> _AlertState:
    """The single shared alert journal state, created on first access (mirrors the rate-ring pattern).
    Materialized single-threaded in ``create_app``."""
    state = getattr(services, "_alert_state", None)
    if state is None:
        state = _AlertState()
        setattr(services, "_alert_state", state)
    return state


@dataclass
class _HoverState:
    """Server-side per-pair 09:20 hover snapshot (A2) — ``pairs[view_key] = {strike_key: {…}}`` mirrored
    to ``hover.json`` via the shared daily-state helpers. Captured once within the 09:20 window
    ``[09:20, 09:21)`` (earliest wave in that minute wins, frozen), for whatever pairs are fetched during
    it; a pair with no fetch in the window gets no snapshot (absent, never a later chain). Read under the
    lock by the chain display. Mirrors ``_AlertState`` / ``_InterpretorState``."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    date: str | None = None                        # the trading day `pairs` belongs to (IST YYYY-MM-DD)
    pairs: dict = field(default_factory=dict)      # view_key -> {strike_key: {call_oi,put_oi,call_iv,put_iv,pcr,straddle_oi}} (persisted)
    loaded: bool = False                           # hover.json loaded into `pairs` on the first wave


def get_or_create_hover_state(services: RuntimeServices) -> _HoverState:
    """The single shared hover-snapshot state, created on first access (mirrors the rate-ring pattern).
    Materialized single-threaded in ``create_app``."""
    state = getattr(services, "_hover_state", None)
    if state is None:
        state = _HoverState()
        setattr(services, "_hover_state", state)
    return state


def _interpretor_config() -> dict:
    """The interpretor tunables (config.json) plus the OI floor (reused from the rate-alert floor)."""
    return {**INTERPRETOR_CONFIG, "oi_floor": float(RATE_ALERT_FLOORS.get("oi", 0.0))}


def _update_interpretor(services: RuntimeServices, result, now_dt: datetime) -> None:
    """Advance the per-pair interpretor state from each fetched pair's IN-MEMORY report (no DB read),
    under the state lock. Loads today's ``interpretor.json`` on the first wave (mid-day restart reload),
    wipes on the IST date rollover, and rewrites the whole file each wave (trivially cheap at ≤5 pairs).
    Guarded so an interpretor error never breaks the fetch wave (like ``_update_rate_rings``).

    No-op when the interpretor is disabled (``config.json`` ``interpretor.enabled`` false/absent →
    ``INTERPRETOR_ENABLED`` False): the wave computes nothing, ``state.reads`` stays empty, and no
    ``interpretor.json`` is written — so every display surface goes dark via its existing null-safety."""
    if not INTERPRETOR_ENABLED:
        return
    try:
        state = get_or_create_interpretor_state(services)
        config = _interpretor_config()
        today = now_dt.date().isoformat()
        with state.lock:
            if not state.loaded:
                state.pairs = dict(load_daily_payload(services.interpretor_json_path, today).get("pairs", {}))
                state.date = today
                state.loaded = True
            if daily_is_stale(state.date, today):  # crossed midnight while running → a fresh day
                state.pairs = {}
                state.reads = {}
                state.date = today
            for item in result.pair_results:
                report = item.report or {}
                snapshot = {
                    "rows": report.get("rows", []) or [],
                    "underlying_price": report.get("underlying_price"),
                    "strike_size": report.get("strike_size"),
                }
                view_key = item.request.pair_key.value
                next_state, read = interpret_pair(state.pairs.get(view_key), snapshot, now=now_dt, config=config)
                state.pairs[view_key] = next_state
                state.reads[view_key] = read
            persist_daily_state(services.interpretor_json_path, state.date, {"pairs": state.pairs})
    except Exception:
        _LOGGER.exception("Interpretor update failed; continuing")


def _update_alerts(services: RuntimeServices, result, now_dt: datetime) -> None:
    """Advance the Tier-1 alert journal from each fetched pair's IN-MEMORY report (meta-only, no DB read)
    under the state lock, then forward the wave's new alerts to Telegram (category-filtered, best-effort).
    Loads today's ``alerts_cache.json`` on the first wave (mid-day restart reload); ``apply_live_wave``
    itself does the daily wipe on the IST date rollover. The collector is the single writer of that file.
    Guarded so an alert error never breaks the fetch wave (like ``_update_rate_rings``)."""
    try:
        state = get_or_create_alert_state(services)
        today = now_dt.date().isoformat()
        now_hhmmss = now_dt.strftime("%H:%M:%S")
        with state.lock:
            if not state.loaded:
                state.journal = {"date": today, **load_daily_payload(services.alerts_cache_path, today)}
                state.loaded = True
            pairs = [
                (item.request.pair_key.value, item.report or {},
                 f"{item.request.display_name} | {item.request.expiry}")
                for item in result.pair_results
            ]
            state.journal, new_wave_alerts, dirty = apply_live_wave(
                state.journal, pairs, today=today, now_hhmmss=now_hhmmss,
            )
            if dirty:
                persist_daily_state(
                    services.alerts_cache_path, today,
                    {"baselines": state.journal.get("baselines", {}), "alerts": state.journal.get("alerts", {})},
                )
        # Forward the wave's new alerts (all pairs), filtered by the telegram_forward.tier1 categories
        # (decision D — default-deny). Best-effort + non-blocking; outside the lock.
        allowed = TELEGRAM_FORWARD_CONFIG.get("tier1", {})
        to_forward = [(label, alert) for label, alert in new_wave_alerts if allowed.get(alert.get("category"), False)]
        if to_forward and services.telegram_notifier is not None:
            services.telegram_notifier.send(format_tier1_message(to_forward, now_hhmmss=now_hhmmss))
    except Exception:
        _LOGGER.exception("Alert update failed; continuing")


def _update_hover(services: RuntimeServices, result, now_dt: datetime) -> None:
    """Capture the per-strike 09:20 reference snapshot (±32 band) for each fetched pair — **once**, within
    the 09:20 window ``[09:20, 09:21)`` (the earliest wave in that minute wins, frozen); a pair with no
    fetch in the window gets no snapshot (absent, never a later chain). Stored in ``_HoverState`` +
    ``hover.json`` via the shared daily-state helpers; computed from ``report['rows']`` (no DB read).
    Guarded like ``_update_rate_rings``."""
    try:
        state = get_or_create_hover_state(services)
        today = now_dt.date().isoformat()
        in_window = _HOVER_0920 <= now_dt.time() < _HOVER_0920_END
        with state.lock:
            if not state.loaded:
                state.pairs = dict(load_daily_payload(services.hover_json_path, today).get("pairs", {}))
                state.date = today
                state.loaded = True
            if daily_is_stale(state.date, today):
                state.pairs = {}
                state.date = today
            if not in_window:
                return  # outside the 09:20 window → nothing to capture this wave
            dirty = False
            for item in result.pair_results:
                view_key = item.request.pair_key.value
                if view_key in state.pairs:
                    continue  # already captured in the window → frozen
                report = item.report or {}
                snapshot = build_hover_snapshot(report.get("rows", []) or [], report.get("underlying_price"))
                if snapshot:
                    state.pairs[view_key] = snapshot
                    dirty = True
            if dirty:
                persist_daily_state(services.hover_json_path, state.date, {"pairs": state.pairs})
    except Exception:
        _LOGGER.exception("Hover snapshot update failed; continuing")


def _update_rate_rings(services: RuntimeServices, result, now_dt: datetime) -> None:
    """Feed each fetched pair's rows into the server-side rate ring, under its lock. Guarded so a ring
    error never breaks the fetch wave. ``update_ring`` itself no-ops before 09:45 IST (Gate B)."""
    try:
        ring_state = get_or_create_rate_ring_state(services)
        with ring_state.lock:
            for item in result.pair_results:
                report = item.report or {}
                pair_ring = ring_state.rings.setdefault(item.request.pair_key.value, {})
                update_ring(pair_ring, report.get("rows", []) or [], now=now_dt, spot=report.get("underlying_price"))
    except Exception:
        _LOGGER.exception("Rate-ring update failed; continuing")


def _update_rate_clusters(services: RuntimeServices, result, now_dt: datetime) -> None:
    """Detect Tier-2 rate clusters for every fetched pair (all-pairs) from the freshly-filled server ring
    and forward each to Telegram — rule-filtered (telegram_forward.tier2, decision D) + gated by its own
    per-``(view_key, rule, direction)`` 60 s cooldown (``forward_keys`` on the ring state). Server-side,
    so forwarding is tab-independent. **No sound decision here** — the beep stays browser-side +
    active-pair (Phase 6). Guarded like ``_update_rate_rings``."""
    try:
        notifier = services.telegram_notifier
        if notifier is None:
            return  # forwarding is the only server-side consumer of clusters — nothing to do without it
        allowed = TELEGRAM_FORWARD_CONFIG.get("tier2", {})
        ring_state = get_or_create_rate_ring_state(services)
        now_mono = _time.monotonic()
        now_hhmmss = now_dt.strftime("%H:%M:%S")

        # Detect clusters for every pair (rule-filtered). Snapshot the ring under the lock, compute outside.
        detected: list[tuple[str, str, object]] = []  # (label, view_key, cluster)
        for item in result.pair_results:
            report = item.report or {}
            strike_size = report.get("strike_size")
            if not strike_size:
                continue
            view_key = item.request.pair_key.value
            label = f"{item.request.display_name} | {item.request.expiry}"
            with ring_state.lock:
                pair_ring = ring_state.rings.get(view_key)
                snapshot = {cid: list(dq) for cid, dq in pair_ring.items()} if pair_ring else {}
            if not snapshot:
                continue
            for cluster in detect_rate_clusters(
                compute_rate_crossings(snapshot, now=now_dt), strike_size=strike_size
            ):
                if allowed.get(cluster.rule, False):
                    detected.append((label, view_key, cluster))

        # Forward past the per-(view_key, rule, direction) 60 s cooldown (its own state on the ring).
        to_forward: list[tuple[str, object]] = []
        with ring_state.lock:
            for label, view_key, cluster in detected:
                key = (view_key, cluster.rule, cluster.direction)
                if now_mono - ring_state.forward_keys.get(key, 0.0) < RATE_ALERT_SOUND_COOLDOWN_S:
                    continue  # this cluster forwarded within its cooldown
                ring_state.forward_keys[key] = now_mono
                to_forward.append((label, cluster))
        if to_forward:
            notifier.send(format_tier2_message(to_forward, now_hhmmss=now_hhmmss))
    except Exception:
        _LOGGER.exception("Rate-cluster update failed; continuing")


def _market_open_for_collection(now_ist: datetime, holidays: frozenset[date]) -> bool:
    """NSE-only global gate: a weekday that isn't a holiday and before the 15:30 close. There is no
    lower bound — once committed the collector fetches until close (the Dhan request quota and the
    database size are not limiting)."""
    if now_ist.weekday() >= 5:
        return False
    if now_ist.date() in holidays:
        return False
    return now_ist.time() < _MARKET_CLOSE


def _run_wave(services: RuntimeServices, state: _LiveCollectorState, specs, *, fetch_vix: bool) -> None:
    """Run one fetch wave via the existing ``LiveFetchService`` (enrich + write ``market.db``), then
    record the new fetch-group id + status on the shared state under the lock. The report itself is
    the DB write; the browser render poll rebuilds from ``last_fetch_group_id``."""
    result = services.live_fetch_service.fetch_request_group(specs, source="live", fetch_vix=fetch_vix)
    now_dt = datetime.now(IST).replace(tzinfo=None)
    now_iso = now_dt.isoformat()
    # Feed the Tier-2 rate ring BEFORE publishing the new group id, so the render poll that reacts to
    # last_fetch_group_id sees a fresh ring when it paints the Layer-1 markers.
    _update_rate_rings(services, result, now_dt)
    # Detect + Telegram-forward Tier-2 rate clusters from the freshly-filled ring (all-pairs, server-side).
    _update_rate_clusters(services, result, now_dt)
    # Advance the interpretor from the same in-memory reports (no DB read), before publishing the group.
    _update_interpretor(services, result, now_dt)
    # Advance the Tier-1 alert journal + Telegram-forward from the same reports (meta-only, no DB read).
    _update_alerts(services, result, now_dt)
    # Capture the A2 09:20 per-strike hover snapshot from the same reports (once at 09:20; no DB read).
    _update_hover(services, result, now_dt)
    with state.lock:
        state.last_fetch_group_id = result.fetch_group_id
        state.last_fetch_ts = now_iso
        state.last_status = result.status_text
        if fetch_vix:
            state.vix_last_ts = now_iso


def _collector_iteration(
    services: RuntimeServices,
    state: _LiveCollectorState,
    *,
    last_wave: float,
    last_vix: float,
    now_ist: datetime,
    vix_seconds: float = _DEFAULT_VIX_SECONDS,
    monotonic=_time.monotonic,
) -> tuple[float, float]:
    """One loop iteration's decision (extracted from the loop so it is unit-testable). Returns the
    updated ``(last_wave, last_vix)`` monotonic marks. Pure of the wait; performs at most one wave.

    Order: nothing committed → no-op; EOD running → stand down (don't contend with the prune's
    VACUUM); market closed → latch ``active``/``pending`` off; a queued one-shot → one wave with VIX;
    else continuous & cadence elapsed → a wave (VIX only every ``vix_seconds``)."""
    with state.lock:
        active = state.active
        pending = state.pending_one_shot
        specs = list(state.request_specs)
        cadence_ms = state.cadence_ms

    if not ((active or pending) and specs and services.live_fetch_service is not None):
        return last_wave, last_vix
    if get_or_create_eod_state(services).eod_in_progress:
        return last_wave, last_vix

    # A manual one-shot Fetch ALWAYS runs — any day, any time. The app is started/stopped by hand each
    # day, and the user pulls the current market state at will (incl. off-session / weekends), so a
    # Fetch click must never be gated by market hours. (It still stands down during the ~5-min EOD
    # prune above, to avoid contending with the VACUUM — it stays queued and fires right after.) Only
    # continuous auto-refresh is market-hours-gated below.
    if pending:
        _run_wave(services, state, specs, fetch_vix=True)
        with state.lock:
            state.pending_one_shot = False
        mark = monotonic()
        return mark, mark

    # Continuous collection auto-stops at the 15:30 NSE close / skips non-trading days, so a left-on
    # auto-refresh doesn't poll a closed market pointlessly. (Only `active` is latched off here — a
    # queued one-shot was already handled above.)
    if not _market_open_for_collection(now_ist, services.holidays):
        with state.lock:
            state.active = False
            state.last_status = "Live collection stopped (market closed)."
        return last_wave, last_vix

    if active and (monotonic() - last_wave) * 1000.0 >= cadence_ms:
        want_vix = (monotonic() - last_vix) >= vix_seconds
        _run_wave(services, state, specs, fetch_vix=want_vix)
        mark = monotonic()
        return mark, (mark if want_vix else last_vix)
    return last_wave, last_vix


def _collector_loop(
    services: RuntimeServices,
    state: _LiveCollectorState,
    stop_event: threading.Event,
    *,
    vix_seconds: float = _DEFAULT_VIX_SECONDS,
    poll_seconds: float = _POLL_SECONDS,
) -> None:
    """The collector loop. Waits on ``state.wake`` (set by a manual Fetch commit → instant wave) with
    a ``poll_seconds`` timeout (cadence granularity when not woken). Each iteration is fully guarded
    so a transient error (DB locked, API hiccup) never kills the thread."""
    last_wave = 0.0
    last_vix = 0.0
    while not stop_event.is_set():
        try:
            last_wave, last_vix = _collector_iteration(
                services,
                state,
                last_wave=last_wave,
                last_vix=last_vix,
                now_ist=datetime.now(IST),
                vix_seconds=vix_seconds,
            )
        except Exception:  # pragma: no cover - the loop must never die
            _LOGGER.exception("Live collector loop iteration failed; continuing")
        state.wake.wait(timeout=poll_seconds)
        state.wake.clear()


def start_live_collector(
    services: RuntimeServices,
    *,
    vix_seconds: float = _DEFAULT_VIX_SECONDS,
) -> tuple[threading.Thread, threading.Event]:
    """Start the daemon collector thread. Called once from ``app.main()`` (never ``create_app``, so
    tests / validate.py don't spawn it). ``use_reloader=False`` in ``app.run`` ⇒ one process → one
    collector. Returns the thread + its stop Event."""
    state = get_or_create_live_collector_state(services)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_collector_loop,
        args=(services, state, stop_event),
        kwargs={"vix_seconds": vix_seconds},
        name="onechain-live-collector",
        daemon=True,
    )
    thread.start()
    return thread, stop_event
