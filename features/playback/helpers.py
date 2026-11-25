"""Pure helpers for the playback feature (unit-testable, no Dash/I/O)."""

from __future__ import annotations

from datetime import datetime, time as _dtime
from typing import Iterable, Mapping, Sequence

_BOOKMARK_COLOR = "#ffd700"
_HALF_HOUR_COLOR = "#8a97b5"  # muted
# Half-hour reference ticks under the scrubber: 9:30 → 3:30 (every 30 min).
_HALF_HOUR_TARGETS = [
    _dtime(hour, minute)
    for hour in range(9, 16)
    for minute in (0, 30)
    if _dtime(9, 30) <= _dtime(hour, minute) <= _dtime(15, 30)
]


def _ts_minutes(ts_iso: str) -> int | None:
    """Minutes-since-midnight of a naive-IST ISO timestamp, or ``None`` on parse failure."""
    try:
        parsed = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.hour * 60 + parsed.minute


def _fmt_hhmm(target: _dtime) -> str:
    """12-hour ``h:MM`` (e.g. 15:30 → ``3:30``, 12:00 → ``12:00``) — the format the scrubber labels use."""
    hour_12 = target.hour % 12 or 12
    return f"{hour_12}:{target.minute:02d}"


def build_half_hour_marks(
    available_moments: Sequence,
    base_marks: Mapping[int, dict],
    *,
    threshold_min: int = 20,
) -> dict[int, dict]:
    """Label the slider mark nearest each half-hour (9:30 → 3:30) with the time.

    Gives an at-a-glance sense of where in the session the scrubber sits. For each half-hour target the
    nearest moment is labelled, but only when it's within ``threshold_min`` of the target (so session
    ends without data aren't mislabelled). Requires moments in ascending time order.
    """
    marks: dict[int, dict] = {index: dict(base_marks.get(index, {})) for index in range(len(available_moments))}
    minutes = [_ts_minutes(getattr(moment, "start_ts", "")) for moment in available_moments]
    for target in _HALF_HOUR_TARGETS:
        target_min = target.hour * 60 + target.minute
        best_index, best_dist = None, None
        for index, moment_min in enumerate(minutes):
            if moment_min is None:
                continue
            dist = abs(moment_min - target_min)
            if best_dist is None or dist < best_dist:
                best_index, best_dist = index, dist
        if best_index is not None and best_dist is not None and best_dist <= threshold_min:
            mark = marks.get(best_index, {})
            mark["label"] = _fmt_hhmm(target)
            mark["style"] = {"color": _HALF_HOUR_COLOR, "fontSize": "10px"}
            marks[best_index] = mark
    return marks


def build_bookmark_marks(
    available_moments: Sequence,
    bookmarked_fetch_group_ids: Iterable[str],
    base_marks: Mapping[int, dict],
) -> dict[int, dict]:
    """Return slider marks with bookmarked moments highlighted (gold ★).

    A moment is bookmarked when its ``fetch_group_id`` is in
    ``bookmarked_fetch_group_ids``. Highlighted marks get a leading ★ and a gold
    bold style; non-bookmarked marks are returned unchanged.
    """
    bookmarked = {str(value) for value in (bookmarked_fetch_group_ids or ())}
    marks: dict[int, dict] = {}
    for index, moment in enumerate(available_moments):
        mark = dict(base_marks.get(index, {}))
        if str(getattr(moment, "fetch_group_id", "")) in bookmarked:
            label = str(mark.get("label", "")).lstrip("★").strip()
            mark["label"] = f"★ {label}" if label else "★"
            mark["style"] = {"color": _BOOKMARK_COLOR, "fontWeight": "bold"}
        marks[index] = mark
    return marks
