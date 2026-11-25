"""VIX policy helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Callable, Mapping

from repositories.market_data_repository import MarketDataRepository

IST = ZoneInfo("Asia/Kolkata")


def resolve_bs_volatility(
    segment_code: str,
    *,
    vix_value: float | None,
    mcx_volatility: float | None,
) -> float | None:
    """The single Black-Scholes volatility (decimal) for a pair's contracts.

    NSE uses India VIX (a percentage, so ÷100); MCX uses the configured commodity vol.
    Returns ``None`` when no honest market volatility is available — the BS price is then
    not computed (never falls back to a contract's own implied volatility or a constant).
    """
    if segment_code == "MCX_COMM":
        return mcx_volatility
    if vix_value is None or vix_value <= 0:
        return None
    return float(vix_value) / 100.0


def _parse_vix_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


@dataclass
class VixSnapshot:
    value: float
    metadata: dict[str, Any]


# The NSE session open (India VIX is an NSE index): only a VIX fetched at/after this time
# today counts. How long the app waits (from the first fetch attempt of the day) before
# falling back to the configured default when no qualifying VIX has been fetched yet.
_SESSION_OPEN = (9, 15)  # 09:15 IST
_STARTUP_GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class VixStatus:
    """The market-volatility decision for live Black-Scholes + the tab-bar readout.

    state:
      ``live``        — the latest stored VIX fetched **today at/after 09:15 IST** (any age);
                        ``value`` is that VIX (percentage). A real same-day value is used for the
                        whole session — it never expires in favour of the default, and the first
                        post-09:15 success carries even if all later fetches fail.
      ``default``     — no VIX fetched today at/after 09:15, and ≥5min since the first attempt of
                        the day; ``value`` is the configured DEFAULT_VIX (percentage).
      ``unavailable`` — no qualifying VIX yet and <5min since the first attempt (or no honest
                        default); ``value`` is ``None`` so BS computes nothing.
    """

    state: str
    value: float | None
    timestamp: str | None
    is_default: bool


class VixService:
    """Owns VIX persistence policy, storage, and the freshness decision."""

    def __init__(
        self,
        market_data_repository: MarketDataRepository,
        fetcher: Callable[[], tuple[float, Mapping[str, Any]] | None] | None = None,
        default_vix: float | None = None,
    ) -> None:
        self.market_data_repository = market_data_repository
        self.fetcher = fetcher
        self.default_vix = default_vix
        # In-memory anchor for the 5-minute startup grace: the first fetch attempt
        # of the current IST date. Reset when the date rolls over.
        self._first_attempt_date: str | None = None
        self._first_attempt_time: datetime | None = None

    def _mark_attempt(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._first_attempt_date != today:
            self._first_attempt_date = today
            self._first_attempt_time = now

    def fetch_and_store(self) -> VixSnapshot | None:
        if self.fetcher is None:
            return None
        self._mark_attempt(datetime.now(IST).replace(tzinfo=None))
        payload = self.fetcher()
        if not payload:
            return None
        value, metadata = payload
        self.market_data_repository.save_vix_row(float(value), dict(metadata))
        return VixSnapshot(value=float(value), metadata=dict(metadata))

    def vix_status(self, now: datetime | None = None) -> VixStatus:
        """Resolve the live/default/unavailable VIX state (see ``VixStatus``)."""
        now = now or datetime.now(IST).replace(tzinfo=None)
        today = now.date()
        session_open = now.replace(hour=_SESSION_OPEN[0], minute=_SESSION_OPEN[1], second=0, microsecond=0)

        latest = self.market_data_repository.latest_vix_row()  # newest row only (LIMIT 1)
        latest_ts = _parse_vix_timestamp(latest.get("timestamp")) if latest else None
        # The newest row is the max timestamp, so a single check decides it: if it was fetched today
        # at/after the 09:15 open we use it for the whole session regardless of age (a real value
        # always beats the static default); if it predates the open, no row qualifies. Pre-09:15 and
        # prior-day rows are discarded.
        if latest is not None and latest_ts is not None:
            latest_naive = latest_ts.replace(tzinfo=None)
            latest_value = latest.get("value")
            if latest_naive >= session_open and latest_value is not None:
                return VixStatus("live", float(latest_value), latest_naive.isoformat(), False)

        # No qualifying post-09:15 VIX today — apply the 5-minute startup grace from the first attempt.
        elapsed = (
            now - self._first_attempt_time
            if self._first_attempt_date == today.isoformat() and self._first_attempt_time is not None
            else timedelta(0)
        )
        if elapsed >= _STARTUP_GRACE:
            return self._default_status()
        return VixStatus("unavailable", None, None, False)

    def _default_status(self) -> VixStatus:
        if self.default_vix is None or self.default_vix <= 0:
            return VixStatus("unavailable", None, None, False)
        return VixStatus("default", float(self.default_vix), None, True)

    def latest_vix_value(self) -> float | None:
        """Most recently stored India VIX value (percentage), or ``None`` if none."""
        latest = self.market_data_repository.latest_vix_row()
        if latest is None:
            return None
        value = latest.get("value")
        return float(value) if value is not None else None
