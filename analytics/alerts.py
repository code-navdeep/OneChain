"""Live state-change alert detection — pure compute, no I/O (Tier 1 rule engine).

Compares one pair's freshly-built report against the prior fetch's **baseline** and emits typed
``Alert``s (each with a ``severity``) for the changes worth surfacing. Reads **only scalar report
fields** (never the per-strike rows, never the DB), so detection stays cheap and never pauses — it
runs for every loaded pair off the always-on live feed, even while the user browses Playback.

The rules:
- **Spot crosses a wall** (``critical``) — spot moving across the strong resistance / support wall
  (the pain point of the highest-OI call / put strike, full-chain), past a ``strike_size/2`` dead-band.
- **GEX-regime flip** — → Destabilizing = ``critical``, → Stabilizing = ``info`` — confirmed over 2
  consecutive waves (debounce) so a net-GEX hovering on zero doesn't flap.
- **RR-skew flip** (``warning``) — Put↔Call skew, with a ±``rr_deadband`` vol-point dead-band.
- **PCR band exit** (``warning``) — put/call ratio leaving [0.80, 1.30], re-arming only past a margin.
- **Highest/2nd-highest OI/Vol strike moves** (``info``) — the original flip behaviour, now typed.

Thresholds are **global** named constants (``DEFAULT_THRESHOLDS``); each threshold rule carries its
own hysteresis state in the baseline so it fires once on a cross and re-arms only past the dead-band.
Switching the focused instrument resets the baseline (a new instrument is not a "flip"). The Dash
layer owns the rolling list, the journal, the ticker, and the critical sound.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Alert:
    """One typed alert. Lives in ``dcc.Store`` JSON + the journal file, so it round-trips."""

    time: str
    category: str
    severity: str
    body: str

    def to_dict(self) -> dict[str, str]:
        return {"time": self.time, "category": self.category, "severity": self.severity, "body": self.body}


# Global thresholds — ONE set for every instrument (a known, accepted limitation). Named so a future
# per-instrument layer can swap this dict without touching any rule body.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "pcr_high": 1.30,
    "pcr_high_rearm": 1.25,
    "pcr_low": 0.80,
    "pcr_low_rearm": 0.85,
    "rr_deadband": 0.2,          # vol points around 0 before an RR label commits
    "gex_confirm_waves": 2,      # consecutive waves a GEX flip must hold before it fires
    "wall_margin_mult": 0.5,     # spot-cross re-arm margin = wall_margin_mult × strike_size
}


# Single source of truth for the per-category accent colour, shared by the ticker
# (``features/alerts/rendering``) and the Changes feed (``ui/shell.render_details_feed``) so the two
# can't drift. Mirrors the chain-table / theme semantics.
ALERT_CATEGORY_COLORS = {
    "rr": "#2dd4a0",     # risk-reversal flip — teal
    "gex": "#ff6b6b",    # GEX-regime flip — coral
    "oi": "#f5b544",     # Max OI strike move — amber
    "vol": "#7c9cff",    # Max Volume strike move — blue
    "cross": "#ff4d4d",  # spot crossed a wall — bright red (critical)
    "pcr": "#c792ea",    # PCR band exit — violet
}


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if isnan(result) else result


def _fmt_strike(value: Any) -> str:
    """A strike in an alert body, thousands-grouped and never rounded to a neighbouring strike: a whole
    strike has no decimals (``22,500``), a half strike keeps its decimal (``402.5``)."""
    number = _as_float(value)
    if number is None:
        return str(value)
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _fmt_price(value: Any) -> str:
    """A spot price or pain-point level in an alert body, rounded to whole rupees (``22,512``)."""
    number = _as_float(value)
    return f"{number:,.0f}" if number is not None else str(value)


def _strikes_differ(old: Any, new: Any) -> bool:
    old_f, new_f = _as_float(old), _as_float(new)
    if old_f is None or new_f is None:
        return False
    return old_f != new_f


_STRIKE_FIELDS = (
    ("max_call_oi_strike", "Max Call OI"),
    ("max_put_oi_strike", "Max Put OI"),
    ("max_call_volume_strike", "Max Call Vol"),
    ("max_put_volume_strike", "Max Put Vol"),
    ("second_max_call_oi_strike", "2nd Max Call OI"),
    ("second_max_put_oi_strike", "2nd Max Put OI"),
    ("second_max_call_volume_strike", "2nd Max Call Vol"),
    ("second_max_put_volume_strike", "2nd Max Put Vol"),
)

# The two scalar walls the spot-cross rule watches — the **pain-point walls**: `resistance_strong` =
# pain point of the highest-call-OI strike, `support_strong` = highest-put-OI (both **full-chain** —
# unlike the price-chart and reversal-drawer walls, which use the strike-range window: detection runs
# server-side for every pair and never sees the browser's strike-range selector, so do not "unify"
# them). Report scalars, so detection stays meta-only. label → shown in the alert body.
_WALL_LEVELS = (
    ("resistance_strong", "Resistance"),
    ("support_strong", "Support"),
)


def _snapshot_from_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Raw observable scalars pulled off one pair's report (all present in the meta store)."""
    levels = report.get("levels", {}) or {}
    gex = report.get("gex_analysis", {}) or {}
    skew = report.get("skew", {}) or {}
    totals = report.get("totals", {}) or {}
    return {
        "instrument_key": report.get("instrument_key"),
        "rr_25": _as_float(skew.get("rr_25")),
        "gex_regime": gex.get("market_regime"),
        "put_call_ratio": _as_float(totals.get("put_call_ratio")),
        "underlying_price": _as_float(report.get("underlying_price")),
        "strike_size": _as_float(report.get("strike_size")),
        "max_call_oi_strike": levels.get("max_call_oi_strike"),
        "max_put_oi_strike": levels.get("max_put_oi_strike"),
        "max_call_volume_strike": levels.get("max_call_volume_strike"),
        "max_put_volume_strike": levels.get("max_put_volume_strike"),
        "second_max_call_oi_strike": levels.get("second_max_call_oi_strike"),
        "second_max_put_oi_strike": levels.get("second_max_put_oi_strike"),
        "second_max_call_volume_strike": levels.get("second_max_call_volume_strike"),
        "second_max_put_volume_strike": levels.get("second_max_put_volume_strike"),
        "resistance_strong": _as_float(levels.get("resistance_strong")),
        "support_strong": _as_float(levels.get("support_strong")),
    }


# --- Rule functions: each (prev_baseline, current, thresholds, now) -> (list[Alert], baseline_fields).
# `current` is a raw snapshot; the returned baseline_fields carry that rule's hysteresis state forward.


def _rule_strike_moves(prev: Mapping[str, Any], current: Mapping[str, Any], now: str) -> list[Alert]:
    """Highest / 2nd-highest OI/Vol strike moves — info, no hysteresis (the raw strikes self-diff)."""
    alerts: list[Alert] = []
    for key, label in _STRIKE_FIELDS:
        old, new = prev.get(key), current.get(key)
        if _strikes_differ(old, new):
            category = "vol" if "Vol" in label else "oi"
            alerts.append(Alert(now, category, "info", f"{label} {_fmt_strike(old)} → {_fmt_strike(new)}"))
    return alerts


def _rule_rr_flip(
    prev: Mapping[str, Any], current: Mapping[str, Any], thresholds: Mapping[str, float], now: str
) -> tuple[list[Alert], dict[str, Any]]:
    """RR-skew flip with a ±``rr_deadband`` dead-band on ``rr_25`` — warning."""
    rr = current.get("rr_25")
    prev_committed = prev.get("rr_committed")
    deadband = thresholds["rr_deadband"]
    committed = prev_committed
    if rr is not None:
        if rr > deadband:
            committed = "Call skew"
        elif rr < -deadband:
            committed = "Put skew"
        elif prev_committed is None:
            committed = "Flat"  # seed inside the band
        # else: within the dead-band → keep the committed label (this is the hysteresis)
    fields = {"rr_committed": committed}
    if prev_committed and committed and prev_committed != committed:
        return [Alert(now, "rr", "warning", f"RR flipped {prev_committed} → {committed}")], fields
    return [], fields


def _rule_gex_flip(
    prev: Mapping[str, Any], current: Mapping[str, Any], thresholds: Mapping[str, float], now: str
) -> tuple[list[Alert], dict[str, Any]]:
    """GEX-regime flip confirmed over ``gex_confirm_waves`` consecutive waves.

    → Destabilizing = critical, → Stabilizing = info. A raw regime that differs from the committed
    one must persist for the full confirm count before it commits + fires; a return to the committed
    regime clears the pending counter (debounce around a net-GEX hovering on zero)."""
    raw = current.get("gex_regime")
    committed = prev.get("gex_committed")
    pending = prev.get("gex_pending")
    pending_count = int(prev.get("gex_pending_count") or 0)
    confirm = int(thresholds["gex_confirm_waves"])

    if raw is None:
        return [], {"gex_committed": committed, "gex_pending": pending, "gex_pending_count": pending_count}
    if committed is None:
        return [], {"gex_committed": raw, "gex_pending": None, "gex_pending_count": 0}  # seed
    if raw == committed:
        return [], {"gex_committed": committed, "gex_pending": None, "gex_pending_count": 0}

    # raw differs from committed — accumulate confirmation
    if pending == raw:
        pending_count += 1
    else:
        pending, pending_count = raw, 1
    if pending_count >= confirm:
        severity = "critical" if raw == "Destabilizing" else "info"
        return (
            [Alert(now, "gex", severity, f"GEX regime → {raw}")],
            {"gex_committed": raw, "gex_pending": None, "gex_pending_count": 0},
        )
    return [], {"gex_committed": committed, "gex_pending": pending, "gex_pending_count": pending_count}


def _rule_pcr_cross(
    prev: Mapping[str, Any], current: Mapping[str, Any], thresholds: Mapping[str, float], now: str
) -> tuple[list[Alert], dict[str, Any]]:
    """PCR leaving the normal band [pcr_low, pcr_high] — warning; re-arms silently past the margin."""
    pcr = current.get("put_call_ratio")
    zone = prev.get("pcr_zone")
    if pcr is None:
        return [], {"pcr_zone": zone}
    if zone is None:  # seed
        seeded = "high" if pcr > thresholds["pcr_high"] else ("low" if pcr < thresholds["pcr_low"] else "normal")
        return [], {"pcr_zone": seeded}

    alerts: list[Alert] = []
    new_zone = zone
    if zone == "normal":
        if pcr > thresholds["pcr_high"]:
            new_zone = "high"
            alerts.append(Alert(now, "pcr", "warning", f"PCR {pcr:.2f} above {thresholds['pcr_high']:.2f}"))
        elif pcr < thresholds["pcr_low"]:
            new_zone = "low"
            alerts.append(Alert(now, "pcr", "warning", f"PCR {pcr:.2f} below {thresholds['pcr_low']:.2f}"))
    elif zone == "high":
        if pcr < thresholds["pcr_high_rearm"]:
            new_zone = "normal"  # retreated back into the band — re-arm silently
    elif zone == "low":
        if pcr > thresholds["pcr_low_rearm"]:
            new_zone = "normal"
    return alerts, {"pcr_zone": new_zone}


def _rule_spot_crosses_wall(
    prev: Mapping[str, Any], current: Mapping[str, Any], thresholds: Mapping[str, float], now: str
) -> tuple[list[Alert], dict[str, Any]]:
    """Spot crossing a wall level past a ``strike_size/2`` dead-band — critical.

    Per level we track the committed side ``sign(spot − level)``; it flips (and fires) only when spot
    reaches the opposite side by more than the margin, so a spot ticking on the wall doesn't flap.
    First sight seeds the side silently; a ``None`` level is skipped."""
    spot = current.get("underlying_price")
    prev_sides = prev.get("wall_sides") or {}
    if spot is None or spot <= 0:
        return [], {"wall_sides": prev_sides}
    margin = (current.get("strike_size") or 0.0) * thresholds["wall_margin_mult"]
    new_sides = dict(prev_sides)
    alerts: list[Alert] = []
    for level_key, label in _WALL_LEVELS:
        level = _as_float(current.get(level_key))
        if level is None:
            continue
        prev_side = prev_sides.get(level_key)
        if prev_side is None:  # seed
            new_sides[level_key] = 1 if spot >= level else -1
            continue
        if prev_side > 0 and spot < level - margin:
            alerts.append(Alert(now, "cross", "critical", f"Spot {_fmt_price(spot)} crossed below {label} {_fmt_price(level)}"))
            new_sides[level_key] = -1
        elif prev_side < 0 and spot > level + margin:
            alerts.append(Alert(now, "cross", "critical", f"Spot {_fmt_price(spot)} crossed above {label} {_fmt_price(level)}"))
            new_sides[level_key] = 1
        # else: within the dead-band or unchanged side → keep the committed side
    return alerts, {"wall_sides": new_sides}


_THRESHOLD_RULES = (_rule_spot_crosses_wall, _rule_gex_flip, _rule_rr_flip, _rule_pcr_cross)


def detect_alerts(
    prev_baseline: Mapping[str, Any] | None,
    report: Mapping[str, Any],
    *,
    now_hhmmss: str,
    thresholds: Mapping[str, float] = DEFAULT_THRESHOLDS,
) -> tuple[list[Alert], dict[str, Any]]:
    """Return ``(new_alerts, next_baseline)`` for one pair's report.

    A missing baseline or an instrument change resets and **seeds** (runs every threshold rule against
    an empty prior so their hysteresis state initializes) and emits nothing. Otherwise every rule runs
    against the prior baseline; the returned baseline is the raw snapshot merged with each rule's
    carried-forward hysteresis state (JSON-safe)."""
    current = _snapshot_from_report(report)

    if prev_baseline is None or prev_baseline.get("instrument_key") != current.get("instrument_key"):
        next_baseline = dict(current)
        for rule in _THRESHOLD_RULES:
            _alerts, fields = rule({}, current, thresholds, now_hhmmss)
            next_baseline.update(fields)
        return [], next_baseline

    alerts: list[Alert] = []
    next_baseline = dict(current)
    for rule in _THRESHOLD_RULES:
        rule_alerts, fields = rule(prev_baseline, current, thresholds, now_hhmmss)
        alerts.extend(rule_alerts)
        next_baseline.update(fields)
    alerts.extend(_rule_strike_moves(prev_baseline, current, now_hhmmss))
    return alerts, next_baseline


def _empty_state(today: str) -> dict:
    return {"date": today, "baselines": {}, "alerts": {}}


def apply_live_wave(
    working: dict,
    pairs: Iterable[tuple[str, Mapping[str, Any], str]],
    *,
    today: str,
    now_hhmmss: str,
) -> tuple[dict, list[tuple[str, dict]], bool]:
    """Fold one live wave into the per-pair alert journal (pure — a fold over ``detect_alerts``).

    ``pairs`` is an iterable of ``(view_key, report, label)`` — the server collector feeds it from
    ``result.pair_results`` (the browser, if ever needed, from ``loaded_pairs``); the **caller** passes
    only live pairs (the source gate lives there, not here). Returns ``(working, new_wave_alerts,
    dirty)`` where ``new_wave_alerts`` is ``[(label, alert_dict), …]`` for everything newly detected this
    wave — the Telegram forward payload (``detect_alerts`` is diff-based, so exactly once-only, no
    high-water mark). A data-driven daily wipe resets the whole journal when ``working['date']`` differs
    from ``today``. New alerts prepend (newest-first, uncapped) under each pair's ``view_key``; ``dirty``
    is ``True`` when anything changed and the file should be re-written.
    """
    dirty = False
    if working.get("date") != today:
        working = _empty_state(today)
        dirty = True
    baselines = working.setdefault("baselines", {})
    alerts = working.setdefault("alerts", {})
    new_wave_alerts: list[tuple[str, dict]] = []

    for view_key, report, label in pairs:
        new_alerts, baseline = detect_alerts(baselines.get(view_key), report, now_hhmmss=now_hhmmss)
        if baselines.get(view_key) != baseline:
            dirty = True
        baselines[view_key] = baseline
        if new_alerts:
            new_dicts = [a.to_dict() for a in new_alerts]
            alerts[view_key] = new_dicts[::-1] + alerts.get(view_key, [])  # newest-first, uncapped
            new_wave_alerts.extend((label, alert_dict) for alert_dict in new_dicts)

    return working, new_wave_alerts, dirty


def build_details_feed(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Curate one pair's newest-first ``Alert`` dicts into rows for the Changes panel.

    Keeps **every** OI/Volume strike-move (``oi`` / ``vol``) alert, and only the **latest** of each
    other category (``rr`` / ``gex`` / ``cross`` / ``pcr``) — i.e. the first seen walking newest-first,
    so older superseded flips drop out. Order preserved (newest-first). Each kept row is the ``Alert``
    dict itself (``{time, category, severity, body}``) — the renderer reads it directly."""
    seen: set[str] = set()
    feed: list[dict[str, Any]] = []
    for alert in alerts:
        category = alert["category"]
        if category in ("oi", "vol"):
            feed.append(alert)
        elif category not in seen:
            seen.add(category)
            feed.append(alert)
    return feed


def _parse_level_body(body: str) -> dict[str, str] | None:
    """Parse a level alert body (``"Max Call OI 22,800 → 22,500"``) into its parts, or ``None``.

    Returns ``{rank ("max"|"second"), core ("Call OI"/"Put OI"/"Call Vol"/"Put Vol"), old, new}``.
    Used only to detect a highest ⇄ 2nd-highest positional swap (see ``group_swap_rows``); anything
    that isn't a recognized level body degrades to ``None`` (never raises)."""
    if " → " not in body:
        return None
    left, _, new = body.rpartition(" → ")
    for _field_key, label in _STRIKE_FIELDS:
        if left.startswith(label + " "):
            return {
                "rank": "second" if label.startswith("2nd ") else "max",
                "core": label.split("Max ", 1)[1],
                "old": left[len(label) + 1:].strip(),
                "new": new.strip(),
            }
    return None


def group_swap_rows(feed: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """Bind a highest ⇄ 2nd-highest *positional swap* into one render unit for the Changes feed.

    When a side's top-two OI/Vol strikes exchange places, ``detect_alerts`` emits two rows in the
    same wave — ``Max Call OI A → B`` and ``2nd Max Call OI B → A``. This pairs exactly that case
    (same timestamp, same side+metric, mirror-image strikes) so the renderer can tie the two. **Both
    rows are preserved** (nothing merged), the max row first; everything else passes through in order.

    Returns render units: ``("group", [max_row, second_row])`` or ``("row", row)``."""
    parsed = [
        _parse_level_body(item.get("body", "")) if item.get("category") in ("oi", "vol") else None
        for item in feed
    ]
    consumed: set[int] = set()
    units: list[tuple[str, Any]] = []
    for i, item in enumerate(feed):
        if i in consumed:
            continue
        first = parsed[i]
        partner_index = None
        if first is not None:
            for j in range(i + 1, len(feed)):
                if j in consumed or parsed[j] is None:
                    continue
                second = parsed[j]
                if (
                    feed[j].get("time") == item.get("time")
                    and second["core"] == first["core"]
                    and second["rank"] != first["rank"]
                    and first["old"] == second["new"]
                    and first["new"] == second["old"]
                ):
                    partner_index = j
                    break
        if partner_index is not None:
            partner = feed[partner_index]
            pair = [item, partner] if first["rank"] == "max" else [partner, item]
            units.append(("group", pair))
            consumed.add(partner_index)
        else:
            units.append(("row", item))
    return units
