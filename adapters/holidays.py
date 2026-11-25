"""Read-only adapter for the market-holiday list.

The list lives at ``data/holidays.txt`` — one date per line in ``dd/mm/yyyy`` format; lines
starting with ``#`` and blank lines are ignored. It is loaded **once** at the composition root
(``services/runtime.py``) and threaded (as a ``frozenset[date]``) into every Black-Scholes
time-to-expiry computation so the trading-day count subtracts holidays as well as weekends.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path


def load_holidays(path: Path | str) -> frozenset[date]:
    """Parse the holidays file into a set of dates.

    Returns an empty set on a missing/unreadable file (the app then falls back to weekends-only,
    its previous behavior). Unparseable lines are skipped, never fabricated.
    """
    resolved_path = Path(path)
    if not resolved_path.exists():
        return frozenset()

    try:
        text = resolved_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()

    holidays: set[date] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            holidays.add(datetime.strptime(line, "%d/%m/%Y").date())
        except ValueError:
            continue
    return frozenset(holidays)
