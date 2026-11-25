"""Shared display formatting helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


def format_mark_time_label(now: datetime | None = None) -> str:
    """Human-readable IST wall-clock time for the Mark bookmark default — e.g. ``12:35pm``.

    No date, no leading zero on the hour, lowercase meridiem. Defaults to the current IST time;
    ``now`` is injectable for tests.
    """
    moment = now or datetime.now(_IST)
    return moment.strftime("%I:%M%p").lstrip("0").lower()


def format_expiry_label(expiry: str) -> str:
    """Return a readable expiry label."""
    try:
        parsed = datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        return expiry
    return parsed.strftime("%d %b %Y")


def format_expiry_label_short(expiry: str) -> str:
    """Return a compact expiry label with no year — e.g. ``9 Jun`` (no leading zero)."""
    try:
        parsed = datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        return expiry
    return f"{parsed.day} {parsed.strftime('%b')}"


def format_playback_date_label(date_str: str) -> str:
    """Return a human-readable playback date with a 2-digit year — e.g. ``30 Jun '26``
    (no leading zero on the day). The stored value stays the raw ``YYYY-MM-DD``."""
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str
    return f"{parsed.day} {parsed.strftime('%b')} '{parsed.strftime('%y')}"


def format_strike(value: object, *, grouped: bool = False) -> str:
    """A strike as display text, never rounded to a neighbouring strike: a whole strike has no
    decimals (``22500``), a half strike keeps its decimal (``402.5`` — some stocks trade on a
    2.5-wide strike grid). ``grouped`` adds thousands separators (``22,500``) for the surfaces that
    show them."""
    text = f"{float(value):,.2f}" if grouped else f"{float(value):.2f}"
    return text.rstrip("0").rstrip(".")


# --- Interpretor display formatters (shared by the bottom-panel pinned block in ui/shell and the A3
# per-side interpretor-cell hover in features/chain/table_model) ---------------------------------- #


def _interp_streak_text(side: dict) -> str:
    """The signed shift-streak for one side: ``+N`` when the primary wall has stepped to higher strikes
    N times in a row (``shift`` bullish), ``-N`` when to lower strikes (bearish), an em-dash when no
    shift has confirmed yet (streak 0 / degenerate side — no direction to sign)."""
    n = int(side.get("streak", 0) or 0)
    shift = side.get("shift")
    if n <= 0 or shift not in ("bullish", "bearish"):
        return "—"
    return f"+{n}" if shift == "bullish" else f"-{n}"


def _interp_strike_text(value: object) -> str:
    """A 09:20 wall strike: integer when whole (22600), compact float otherwise (2.5 grids), a hyphen
    when missing (a side with no qualifying primary/2nd within a *present* 09:20 stamp)."""
    if value is None:
        return "-"
    fv = float(value)
    return str(int(fv)) if fv == int(fv) else f"{fv:g}"


def interp_hover_line(side: dict, stamp_side: dict | None, *, stamp_present: bool) -> str:
    """A3 — the per-side interpretor-cell hover string: the live ``streak {±N}`` (same as the pinned
    block) plus the frozen 09:20 walls ``| 09:20 {primary} (2nd {second})`` **only when a 09:20 stamp exists**.
    On a late-start day (no stamp — ``stamp_present`` False) the 09:20 half is **dropped entirely** (just
    ``streak {±N}``), never a fabricated ``-`` — and when there is **also no shift** (streak ``—``) the whole
    line is **empty** so no tooltip renders (nothing real to show). Within a present stamp, a ``None``
    primary/2nd shows ``-`` (a real "no wall at 09:20"), and the line always renders (the walls are info)."""
    streak = _interp_streak_text(side)
    if not stamp_present:
        return "" if streak == "—" else f"streak {streak}"
    ss = stamp_side or {}
    return f"streak {streak} | 09:20 {_interp_strike_text(ss.get('primary'))} (2nd {_interp_strike_text(ss.get('second'))})"
