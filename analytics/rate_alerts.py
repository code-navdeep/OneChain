"""Tier 2 rate-of-change alert compute — pure, no I/O.

Per-contract rate rules (rapid OI build/unwind, IV spike/crush, volume surge) over three time
windows (1 / 5 / 30 min), on **percentage change** (scale-free, no distribution). The rolling history
is a **per-minute sampled ring** (≤30 slots per contract) fed server-side by the collector; this module
holds the pure helpers that update the ring, read a window's lookback, and turn the current-vs-lookback
% into a **ternary crossing state** (`"up"` / `"down"` / `None`) per contract × rule × window.

Three consumers share one `compute_rate_crossings` output (one shared source, so the three layers can
never disagree):
- Layer 1 — the in-chain thin marker columns (`marker_state` → the shortest crossed window's state,
  coloured natively via `style_data_conditional` in the chain).
- Layer 2 — the sound gate (`detect_rate_clusters` → 3-consecutive-strike clusters).
- Layer 3 — the full heatmap-chain view (reads the crossings directly).

Gates: **Gate A** — a minimum absolute base on the *lookback* value (illiquid strikes excluded);
**Gate B** — tracking starts at 09:45 IST (the ring is not sampled before then).

**Volume is cumulative intraday → the volume rule is UP-only** (a "crush" is impossible for a
monotonically-rising cumulative). OI and IV are bidirectional.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime
from math import isnan
from typing import Any, Deque, Mapping

from config import (
    RATE_ALERT_CLUSTER_SIZE,
    RATE_ALERT_FLOORS,
    RATE_ALERT_THRESHOLDS,
    RATE_ALERT_TRACKING_START,
    RATE_ALERT_WINDOWS,
)

# Tunables come from config.py (the single edit point); the rest here is structural mechanics.
WINDOWS: tuple[int, ...] = RATE_ALERT_WINDOWS
RULES: tuple[str, ...] = ("oi", "iv", "vol")
RING_MAXLEN = max(WINDOWS) + 1  # one sample/min; +1 so a bucket ≥ the longest window old is retained
TRACKING_START = dtime(*RATE_ALERT_TRACKING_START)  # Gate B — first ring sample at/after 09:45 IST
DEFAULT_RATE_THRESHOLDS = RATE_ALERT_THRESHOLDS
DEFAULT_RATE_FLOORS = RATE_ALERT_FLOORS
DEFAULT_CLUSTER_SIZE = RATE_ALERT_CLUSTER_SIZE

# Which enriched-row field each rule reads (all already lot-scaled where applicable).
RULE_FIELD = {"oi": "oi_lots", "iv": "iv", "vol": "volume_lots"}

_STRIKE_CAP = 32  # ±32 strikes around spot (fixed; readers window further to the user's range)


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if isnan(result) else result


def contract_id(strike: float, option_type: str) -> str:
    """Ring / crossing key for one contract: ``"{strike}|{CE|PE}"``."""
    return f"{float(strike):g}|{option_type}"


def _minute_key(now: datetime) -> datetime:
    return now.replace(second=0, microsecond=0)


def _strikes_within_cap(strikes: list[float], spot: float, cap: int) -> set[float]:
    """The ``cap`` strikes strictly below spot + the ``cap`` strictly above (+ an exact-on-strike spot)."""
    uniq = sorted(set(strikes))
    below = [s for s in uniq if s < spot][-cap:]
    above = [s for s in uniq if s > spot][:cap]
    exact = [s for s in uniq if s == spot]
    return set(below) | set(above) | set(exact)


# --- Ring update + lookback (mutating helpers; the ring dict itself is owned server-side) -------


def update_ring(
    pair_ring: dict[str, Deque[tuple[datetime, float | None, float | None, float | None]]],
    rows: list[Mapping[str, Any]],
    *,
    now: datetime,
    spot: float | None,
    strike_cap: int = _STRIKE_CAP,
) -> None:
    """Write the current minute's sample for each in-window contract into ``pair_ring`` (in place).

    Gate B: does nothing before 09:45 IST. Caps to ``±strike_cap`` strikes around ``spot``. Within a
    minute the **latest** value wins (one bucket per minute); the deque is capped at ``RING_MAXLEN``.
    ``pair_ring`` maps ``contract_id -> deque[(minute, oi_lots, iv, volume_lots)]``. The IV slot is
    ``None`` when the contract has **no IV data** (a non-positive value — parse coerces a missing IV to
    ``0.0`` via ``_as_number``, and a live contract never has a genuine IV of 0): this stops a strike
    whose IV goes dark from reading as a −100% "crush" in ``compute_rate_crossings`` (which already skips
    a ``None`` current/lookback value). ``oi_lots``/``volume_lots`` keep ``0.0`` — a real zero for them.
    """
    if now.time() < TRACKING_START:
        return
    spot_f = _as_float(spot)
    if spot_f is None or spot_f <= 0:
        return
    minute = _minute_key(now)
    keep = _strikes_within_cap([_as_float(r.get("strike")) or 0.0 for r in rows], spot_f, strike_cap)
    for row in rows:
        strike = _as_float(row.get("strike"))
        option_type = str(row.get("option_type", ""))
        if strike is None or strike not in keep or option_type not in ("CE", "PE"):
            continue
        key = contract_id(strike, option_type)
        iv_val = _as_float(row.get("iv"))
        if not iv_val or iv_val <= 0:  # no-data IV (0.0 / negative / None) → None, never a real live IV
            iv_val = None
        sample = (minute, _as_float(row.get("oi_lots")), iv_val, _as_float(row.get("volume_lots")))
        ring = pair_ring.get(key)
        if ring is None:
            ring = deque(maxlen=RING_MAXLEN)
            pair_ring[key] = ring
        if ring and ring[-1][0] == minute:
            ring[-1] = sample  # latest value this minute wins
        else:
            ring.append(sample)


_RULE_SLOT = {"oi": 1, "iv": 2, "vol": 3}  # index of the metric in a ring sample tuple


def lookback(
    ring: Deque[tuple[datetime, float | None, float | None, float | None]],
    window_min: int,
    rule: str,
    *,
    now: datetime,
) -> float | None:
    """The rule's value at the **older-side** bucket ≥ ``window_min`` old (conservative), or ``None``.

    Walks newest→oldest and returns the first bucket whose age ≥ the window; ``None`` when no bucket is
    old enough (warm-up) or that bucket's value is missing."""
    slot = _RULE_SLOT[rule]
    cutoff = _minute_key(now)
    for sample in reversed(ring):
        age_min = (cutoff - sample[0]).total_seconds() / 60.0
        if age_min >= window_min:
            return sample[slot]
    return None


def _current_value(ring: Deque, rule: str) -> float | None:
    if not ring:
        return None
    return ring[-1][_RULE_SLOT[rule]]


# --- Crossings (the shared source of truth for all three layers) --------------------------------


def compute_rate_crossings(
    pair_ring: dict[str, Deque],
    *,
    now: datetime,
    thresholds: Mapping[str, Any] = DEFAULT_RATE_THRESHOLDS,
    floors: Mapping[str, float] = DEFAULT_RATE_FLOORS,
) -> dict[str, dict[str, dict[int, str]]]:
    """``{contract_id: {rule: {window: "up"|"down"}}}`` — only crossed (contract, rule, window) entries.

    Per rule × window: `pct = (now − then)/then` with `then` (the lookback) gated by the Gate-A floor;
    a missing lookback or sub-floor base is skipped (never a fabricated %). ``up`` when
    `pct ≥ +upper`, ``down`` when `pct ≤ −lower` (``vol`` has no lower). Neutral entries are omitted."""
    out: dict[str, dict[str, dict[int, str]]] = {}
    for cid, ring in pair_ring.items():
        for rule in RULES:
            now_val = _current_value(ring, rule)
            if now_val is None:
                continue
            floor = float(floors.get(rule, 0.0))
            for window in WINDOWS:
                then_val = lookback(ring, window, rule, now=now)
                if then_val is None or then_val < floor or then_val == 0:
                    continue
                pct = (now_val - then_val) / then_val
                band = thresholds.get(rule, {}).get(window, {})
                state = None
                if "upper" in band and pct >= band["upper"]:
                    state = "up"
                elif "lower" in band and pct <= -band["lower"]:
                    state = "down"
                if state is not None:
                    out.setdefault(cid, {}).setdefault(rule, {})[window] = state
    return out


def marker_state(window_states: Mapping[int, str] | None) -> str | None:
    """Layer-1 single state for one marker cell: the **shortest** crossed window's state.

    ``window_states`` maps window → ``"up"``/``"down"``. When multiple windows crossed, the shortest
    (most-recent-signal) window wins; returns ``None`` when nothing crossed. The colour itself is applied
    natively as a ``style_data_conditional`` background in the chain — Dash strips inline ``style`` from
    markdown, so the old gradient ``<div>`` never rendered."""
    states = window_states or {}
    for window in sorted(WINDOWS):
        state = states.get(window)
        if state in ("up", "down"):
            return state
    return None


# --- Clusters (the Layer-2 sound gate) ----------------------------------------------------------


@dataclass(frozen=True)
class Cluster:
    """A sound-worthy structural event: 3 consecutive same-side strikes crossing one (rule, direction)."""

    rule: str
    direction: str      # "up" | "down"
    option_type: str    # "CE" | "PE"
    strikes: tuple[float, ...]


def detect_rate_clusters(
    crossings: Mapping[str, Mapping[str, Mapping[int, str]]],
    *,
    strike_size: float,
    min_run: int = DEFAULT_CLUSTER_SIZE,
) -> list[Cluster]:
    """``min_run`` **grid-consecutive** strikes on the **same side** crossing the same
    **(rule, direction)** — window may be MIXED (a strike counts if *any* of its windows crossed that
    rule+direction). "Consecutive" means adjacent on the real grid (gap == ``strike_size``), so
    every-other crossed strikes do **not** count. One `Cluster` per maximal run of ≥ ``min_run``."""
    step = _as_float(strike_size) or 0.0
    if step <= 0:
        return []
    # crossed[(rule, direction, side)] = set of strikes that crossed that rule+direction on that side
    crossed: dict[tuple[str, str, str], set[float]] = {}
    for cid, by_rule in crossings.items():
        strike_str, _, side = cid.partition("|")
        strike = _as_float(strike_str)
        if strike is None or side not in ("CE", "PE"):
            continue
        for rule, by_window in by_rule.items():
            for direction in set(by_window.values()):  # {"up"} / {"down"} / {"up","down"} (mixed windows)
                crossed.setdefault((rule, direction, side), set()).add(strike)

    clusters: list[Cluster] = []
    for (rule, direction, side), strike_set in crossed.items():
        strikes = sorted(strike_set)
        run: list[float] = []
        for s in strikes:
            if run and abs((s - run[-1]) - step) > step * 0.1:  # not grid-adjacent → break the run
                if len(run) >= min_run:
                    clusters.append(Cluster(rule, direction, side, tuple(run)))
                run = []
            run.append(s)
        if len(run) >= min_run:
            clusters.append(Cluster(rule, direction, side, tuple(run)))
    return clusters
