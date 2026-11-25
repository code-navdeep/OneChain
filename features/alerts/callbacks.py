"""Browser alert display — render-only mirror of the server journal + the active-pair Tier-2 beep.

Detection, journaling, and Telegram forwarding for BOTH tiers now run **server-side** in the live
collector (``services/live_collector._update_alerts`` / ``_update_rate_clusters``), so they are
tab-independent. This module keeps only the two browser-side pieces:
- ``update_alerts`` — mirrors the server Tier-1 journal (``get_or_create_alert_state``) into
  ``alerts-state-store`` (preserving this tab's in-memory display trackers), then renders the active
  pair's marquee (``_render_ticker``) and bumps the critical-beep signal (``_critical_signal``).
  Active-pair only, blank in Playback / Aggregate.
- ``rate_cluster_sound`` — the Tier-2 beep: detects clusters for the **active** pair only (off the
  server ring) and bumps ``rate-sound-signal-store`` past the browser 15 s throttle + 60 s cooldown.
  Active-pair-scoped (a beep can't say which pair fired), symmetric with the Tier-1 critical beep.
"""

from __future__ import annotations

import threading
import time as _time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from dash import Dash, Input, Output, State, no_update

from analytics.rate_alerts import compute_rate_crossings, detect_rate_clusters
from config import RATE_ALERT_SOUND_COOLDOWN_S, RATE_ALERT_SOUND_THROTTLE_S
from features.alerts.rendering import build_ticker
from services.live_collector import get_or_create_alert_state, get_or_create_rate_ring_state
from services.runtime import RuntimeServices
from state.models import ActiveViewContext, LoadedReportState


IST = ZoneInfo("Asia/Kolkata")
DISPLAY_CAP = 25

# Tier-2 rate-cluster sound throttle state (monotonic timestamps). Process-level + lock-guarded — the
# global 15s throttle + the per-(view_key, rule, direction) 60s cooldown live here across waves.
_RATE_SOUND_LOCK = threading.Lock()
_RATE_SOUND_STATE: dict = {"global_last": 0.0, "seq": 0, "keys": {}}


def _critical_signal(working: dict, active_context) -> Any:
    """Bump signal for a **new** critical alert on the ACTIVE pair (drives the clientside sound).

    Race-free, mirroring ``_render_ticker``'s "did the intended thing change" discipline: track the
    count of critical alerts already signalled per key in ``working['_signalled']`` and a monotonic
    ``working['_signal_seq']``. First sight of a pair records its current critical count as a baseline
    (no beep — so a post-restart seed or a first pair switch is silent); a later rise in the active
    pair's critical count bumps + returns the sequence. Both trackers live only in the in-memory store
    (never the file), like ``_shown``. Returns the new sequence int, or ``no_update``."""
    blank = (
        active_context is None
        or active_context.source != "live"
        or active_context.kind == "aggregate"
    )
    active_key = None if blank else active_context.pair_key
    if active_key is None:
        return no_update
    full = (working.get("alerts", {}) or {}).get(active_key, []) or []
    crit_count = sum(1 for a in full if a.get("severity") == "critical")
    signalled = working.setdefault("_signalled", {})
    if active_key not in signalled:
        signalled[active_key] = crit_count  # baseline first sight — no beep
        return no_update
    if crit_count <= int(signalled[active_key]):
        return no_update
    signalled[active_key] = crit_count
    seq = int(working.get("_signal_seq") or 0) + 1
    working["_signal_seq"] = seq
    return seq


def _render_ticker(working: dict, active_context) -> tuple[Any, bool]:
    """Children for the bottom-panel ticker — active pair only, blank in playback/aggregate.

    Deterministic + race-free: the intended display is the active pair's journal (or blank in
    playback / aggregate / non-live). We re-render only when that intended content **changes** —
    tracked by ``working['_shown'] = {key, n}`` where ``n`` is the active pair's **full** journal
    count (not the capped slice). So a pair switch, a new alert, or a daily wipe re-renders, while
    an unchanged tick returns ``no_update`` (no scroll restart). ``_shown`` lives only in the
    in-memory store (never the file), so a post-restart seed always renders once.

    Returns ``(children, rendered)`` and sets ``working['_shown']`` when it renders.
    """
    blank = (
        active_context is None
        or active_context.source != "live"
        or active_context.kind == "aggregate"
    )
    active_key = None if blank else active_context.pair_key
    full = [] if blank else ((working.get("alerts", {}) or {}).get(active_key, []) or [])
    target = {"key": active_key, "n": len(full)}
    if (working.get("_shown") or {}) == target:
        return no_update, False
    working["_shown"] = target
    # Display oldest-first so the marquee plays forward in time. Slice THEN reverse: keep the 25 most
    # recent (journal is newest-first), then flip to oldest-first — the marquee reveals DOM item 0
    # first, so item 0 must be the earliest of the shown set. Composes with the `new_alerts[::-1]`
    # prepend in apply_live_wave, which makes a single reversal yield exact chronological order.
    return build_ticker(full[:DISPLAY_CAP][::-1]), True


def register_alerts_callbacks(app: Dash, services: RuntimeServices) -> None:
    """Register the browser render-only alert mirror + the active-pair Tier-2 beep."""

    @app.callback(
        Output("bottom-panel-ticker", "children"),
        Output("alerts-state-store", "data"),
        Output("critical-alert-signal-store", "data"),
        Input("live-loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        State("alerts-state-store", "data"),
        prevent_initial_call=True,
    )
    def update_alerts(live_report_state, active_view_context, alerts_state):
        # Render-only: the collector is the single detector + writer of the journal. Mirror its
        # server-side {date, alerts} into this tab's store — PRESERVING the in-memory display trackers
        # (_shown / _signalled / _signal_seq), which are browser-only and never come from the server —
        # then render the active pair's marquee + the critical-beep signal. The live-report Input is
        # just the ~1s trigger that pulls a fresh mirror each wave.
        active_context = ActiveViewContext.from_dict(active_view_context) if active_view_context else None
        working = dict(alerts_state) if alerts_state else {}
        alert_state = get_or_create_alert_state(services)
        with alert_state.lock:
            working["date"] = alert_state.journal.get("date")
            # Shallow-copy the alerts map under the lock: Dash JSON-serializes the returned store
            # AFTER this lock is released, while the collector may concurrently assign a new
            # view_key into its own dict. Shallow suffices — the collector only ever assigns
            # fresh lists/dicts into the journal, never mutates an inner object in place.
            working["alerts"] = dict(alert_state.journal.get("alerts", {}))

        ticker_out, rendered = _render_ticker(working, active_context)
        signal_out = _critical_signal(working, active_context)
        # Write the store only when the display actually changed (a re-render or a new critical), so an
        # unchanged tick doesn't resend the whole journal or restart the marquee scroll. The mirror
        # self-corrects next tick regardless (it re-reads the store, then overlays the server journal).
        store_out = working if (rendered or signal_out is not no_update) else no_update
        return ticker_out, store_out, signal_out

    @app.callback(
        Output("rate-sound-signal-store", "data"),
        Input("live-loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        prevent_initial_call=True,
    )
    def rate_cluster_sound(live_report_state, active_view_context):
        """Tier-2 beep — ACTIVE-PAIR only (symmetric with the Tier-1 critical beep; a beep can't say
        which pair fired). Detection + Telegram forwarding are server-side (``_update_rate_clusters``);
        here the browser reads only the **active** pair's ring → `compute_rate_crossings` →
        `detect_rate_clusters` and bumps `rate-sound-signal-store` past the 15 s global throttle + the
        per-(rule, direction) 60 s cooldown. Blank in aggregate / playback / non-live."""
        live_state = LoadedReportState.from_dict(live_report_state) if live_report_state else None
        active_context = ActiveViewContext.from_dict(active_view_context) if active_view_context else None
        if (
            live_state is None or live_state.source != "live"
            or active_context is None or active_context.kind != "pair"
        ):
            return no_update
        active_key = active_context.pair_key
        pair = next((p for p in live_state.loaded_pairs if p.resolved_view_key == active_key), None)
        if pair is None:
            return no_update
        strike_size = (pair.report or {}).get("strike_size")
        if not strike_size:
            return no_update

        now_mono = _time.monotonic()
        now_dt = datetime.now(IST).replace(tzinfo=None)
        ring_state = get_or_create_rate_ring_state(services)
        with ring_state.lock:
            pair_ring = ring_state.rings.get(active_key)
            snapshot = {cid: list(dq) for cid, dq in pair_ring.items()} if pair_ring else {}
        if not snapshot:
            return no_update
        clusters = detect_rate_clusters(
            compute_rate_crossings(snapshot, now=now_dt), strike_size=strike_size
        )
        if not clusters:
            return no_update

        # Sound decision — 15 s global throttle → per-(rule, direction) 60 s cooldown → bump the seq.
        with _RATE_SOUND_LOCK:
            if now_mono - _RATE_SOUND_STATE["global_last"] < RATE_ALERT_SOUND_THROTTLE_S:
                return no_update  # global throttle — nothing within 15 s of the last sound
            fired = False
            for cluster in clusters:
                key = (active_key, cluster.rule, cluster.direction)
                if now_mono - _RATE_SOUND_STATE["keys"].get(key, 0.0) < RATE_ALERT_SOUND_COOLDOWN_S:
                    continue  # this rule+direction sounded within its 60 s cooldown
                _RATE_SOUND_STATE["keys"][key] = now_mono
                fired = True
            if not fired:
                return no_update
            _RATE_SOUND_STATE["global_last"] = now_mono
            _RATE_SOUND_STATE["seq"] += 1
            return _RATE_SOUND_STATE["seq"]
