"""Shared rolling-window geometry for the intraday time-series charts.

Every intraday chart (price, per-option metric, aggregate-GEX, risk-reversal, the reversal
sparkline) shows a fixed-width *default view* that is first-anchored: early in the session the
first data point sits a small pad in from the left edge, and once the right anchor (the newest
data point, or `now`) advances past that, the window rolls to keep the latest data near the right
edge. The full series stays in the trace (this only sets the default `range`, not a cap), so it
remains pannable.

Pure datetime math, no I/O — lives in `analytics/` so both `analytics/required_move.py` and the
`features/*/rendering.py` chart builders can import it without a layering violation.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def rolling_window_range(
    *,
    first_t: datetime,
    right_anchor: datetime,
    window_minutes: float,
    left_pad_minutes: float,
    right_pad_minutes: float,
) -> tuple[datetime, datetime]:
    """First-anchored rolling default-view ``(left_edge, right_edge)``.

    Early-session the first point sits ``left_pad_minutes`` from the left edge; once
    ``right_anchor`` advances past ``first_t + window - left_pad``, the window rolls to keep
    ``right_anchor + right_pad`` at the right edge. The window is always ``window_minutes`` wide.

    Parameter choices reproduce every caller:
    - GEX / RR / metric: ``window=60, left_pad=3, right_pad=6``, ``right_anchor`` = newest point.
    - Price: ``window=180, left_pad=90, right_pad=15`` → ``max(first+1.5h, newest+15min)``.
    - Reversal: ``window=60, left_pad=0, right_pad=5``, ``right_anchor`` = ``now``.
    """
    window = timedelta(minutes=window_minutes)
    right_edge = max(
        first_t + window - timedelta(minutes=left_pad_minutes),
        right_anchor + timedelta(minutes=right_pad_minutes),
    )
    return right_edge - window, right_edge
