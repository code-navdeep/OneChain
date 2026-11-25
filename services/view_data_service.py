"""View-data accessors for Phase 6 feature rendering."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from analytics.report_builder import compute_atm_iv_gap
from repositories.market_data_repository import MarketDataRepository
from repositories.snapshot_repository import SnapshotRepository

IST = ZoneInfo("Asia/Kolkata")


def _parse_ts_ist(value: object) -> datetime:
    """Parse a stored timestamp into a naive-IST datetime for chronological ordering.

    Stored timestamps are naive wall-clock IST ISO strings, but this tolerates an aware (``Z`` /
    ``+hh:mm``) suffix (converted to IST, tz dropped) so a stray non-uniform value can't break the
    sort. Unparseable input sorts to the epoch (kept, never raises)."""
    raw = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(IST).replace(tzinfo=None)
    return parsed


def _sort_dedup(points: list[dict]) -> list[dict]:
    """De-duplicate points by timestamp (one row per timestamp) and sort them chronologically.

    Makes the aggregate-GEX cache self-healing: even if an out-of-order or duplicate timestamp ever
    reaches the append-only cache, the returned/plotted series stays strictly monotonic in time, so
    the chart can't draw a chord across the session."""
    by_timestamp = {str(point["timestamp"]): point for point in points}
    return sorted(by_timestamp.values(), key=lambda point: _parse_ts_ist(point["timestamp"]))


@dataclass
class ViewDataService:
    """Centralized read-side service for charts, OA, advanced, and bottom panels."""

    market_data_repository: MarketDataRepository
    snapshot_repository: SnapshotRepository
    # Per-(IST date, instrument, expiry) caches for the yesterday/trophy snapshot reads —
    # that data is fixed for the whole trading day, so we read it from snapshot.db once
    # per instrument and serve every later render from memory.
    _yesterday_cache: dict = field(default_factory=dict, repr=False, compare=False)
    _trophy_cache: dict = field(default_factory=dict, repr=False, compare=False)
    # Per-(asset, segment, expiry, date) cache of standardized aggregate-GEX points. Today's day
    # appends only snapshots newer than the last cached timestamp. Stored rows are append-only, so
    # cached history never goes stale; the entry is evicted when the IST date rolls over.
    _gex_cache: dict = field(default_factory=dict, repr=False, compare=False)
    # Same shape/convention for the price chart's per-fetch spot series (read from pair_reports).
    _price_cache: dict = field(default_factory=dict, repr=False, compare=False)

    def get_underlying_price_series(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str | None = None,
        *,
        end_timestamp: str | None = None,
    ) -> list[dict]:
        """Per-fetch spot series for the price chart, sourced from ``pair_reports`` (one row per
        fetch — see ``list_underlying_price_in_range``).

        **Live** (``end_timestamp=None``): today's session, floored at midnight, served from
        ``_price_cache`` — the first read floors at the session start, every later tick appends only
        snapshots newer than the last cached timestamp (re-reads only new fetches, never the whole
        session). **Playback** (``end_timestamp`` set): a one-off bounded read of the captured session
        (inclusive of the captured candle), **not cached**. The cached entry is evicted at IST
        date rollover.
        """
        anchor = end_timestamp or datetime.now(IST).replace(tzinfo=None).isoformat()
        session_start = f"{str(anchor)[:10]}T00:00:00"
        if end_timestamp is not None:
            return self.market_data_repository.list_underlying_price_in_range(
                asset_id, segment_code, expiry, since_iso=session_start, until_iso=end_timestamp
            )
        today = str(anchor)[:10]
        self._evict_stale_dates(self._price_cache, asset_id, segment_code, expiry, keep_dates={today})
        key = (asset_id, segment_code, expiry, today)
        entry = self._price_cache.get(key)
        if entry is None:
            points = self.market_data_repository.list_underlying_price_in_range(
                asset_id, segment_code, expiry, since_iso=session_start
            )
            last_ts = str(points[-1]["timestamp"]) if points else session_start
            self._price_cache[key] = {"points": points, "last_ts": last_ts}
            return list(points)
        new_points = self.market_data_repository.list_underlying_price_in_range(
            asset_id, segment_code, expiry, since_iso=entry["last_ts"]
        )
        if new_points:
            entry["points"].extend(new_points)
            entry["last_ts"] = str(new_points[-1]["timestamp"])
        return list(entry["points"])

    def _session_start_iso(self) -> str:
        """Today's session-start boundary (the current IST date at 09:00) as a naive wall-clock IST
        ISO string. Bounds the per-contract OA reads to today's session only — so a late-night study
        session can't drag the prior day's stale points into the next morning's live view. Before
        09:00 this is a future time → an empty read (intended). Index-bounded on the timestamp index."""
        now = datetime.now(IST).replace(tzinfo=None)
        return now.replace(hour=9, minute=0, second=0, microsecond=0).isoformat()

    def get_oa_available_contracts(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str,
    ) -> list[dict]:
        return self.market_data_repository.list_available_contracts(
            asset_id, segment_code, expiry, since_iso=self._session_start_iso()
        )

    def get_oa_option_timeseries(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str,
        contracts: list[tuple[float, str]],
        metric: str,
    ) -> list[dict]:
        return self.market_data_repository.list_option_metric_timeseries(
            asset_id, segment_code, expiry, contracts, metric, since_iso=self._session_start_iso()
        )

    def get_oa_aggregate_gex(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str,
    ) -> list[dict]:
        """Standardized aggregate-GEX points for **today's session** (from 09:00 IST).

        Today only — the live tail is appended incrementally (see ``_gex_cache``): the first read
        floors at 09:00, every later tick reads only snapshots newer than the last cached timestamp,
        so a refresh re-aggregates only new rows, never the whole day (keeps the hot read
        index-bounded). The cached entry is evicted when the IST date rolls over.
        """
        today = datetime.now(IST).date().isoformat()
        self._evict_stale_dates(self._gex_cache, asset_id, segment_code, expiry, keep_dates={today})
        key = (asset_id, segment_code, expiry, today)
        session_start = f"{today}T09:00:00"
        entry = self._gex_cache.get(key)
        if entry is None:
            points = _sort_dedup(
                self.market_data_repository.list_aggregate_gex_in_range(
                    asset_id, segment_code, expiry, since_iso=session_start
                )
            )
            last_ts = str(points[-1]["timestamp"]) if points else session_start
            self._gex_cache[key] = {"points": points, "last_ts": last_ts}
            return list(points)
        new_points = self.market_data_repository.list_aggregate_gex_in_range(
            asset_id, segment_code, expiry, since_iso=entry["last_ts"]
        )
        if new_points:
            # Sort+dedup the accumulated list (not a bare extend), so an out-of-order or duplicate
            # timestamp can never leave the plotted series non-monotonic; last_ts tracks the true max.
            entry["points"] = _sort_dedup(entry["points"] + new_points)
            entry["last_ts"] = str(entry["points"][-1]["timestamp"])
        return list(entry["points"])

    @staticmethod
    def _evict_stale_dates(
        cache: dict, asset_id: int, segment_code: str, expiry: str | None, *, keep_dates: set[str]
    ) -> None:
        """Drop cached ``(asset, segment, expiry, date)`` entries whose date isn't in ``keep_dates``
        (the IST date rolled over, or the day was pruned by retention). Shared by the GEX and price
        incremental caches — both key per pair-and-date and freeze append-only history."""
        stale = [
            key
            for key in cache
            if key[:3] == (asset_id, segment_code, expiry) and key[3] not in keep_dates
        ]
        for key in stale:
            del cache[key]

    def get_risk_reversal_series(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str,
    ) -> list[dict]:
        """Today's session 25Δ risk-reversal series (from 09:00 IST) for the RR chart."""
        return self.market_data_repository.list_risk_reversal_series(asset_id, segment_code, expiry)

    def get_instrument_historical_metrics(
        self,
        asset_id: int,
        segment_code: str,
        end_date: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        return self.snapshot_repository.list_historical_metrics(
            asset_id=asset_id,
            segment_code=segment_code,
            end_date=end_date,
            limit=limit,
        )

    def get_latest_historical_metric(
        self,
        asset_id: int,
        segment_code: str,
        end_date: str | None = None,
    ) -> dict | None:
        return self.snapshot_repository.get_latest_historical_metric(
            asset_id=asset_id,
            segment_code=segment_code,
            end_date=end_date,
        )

    def _daily_cache_key(self, asset_id, segment_code, expiry):
        """Key the daily snapshot caches by IST date + instrument + expiry.

        The date component auto-invalidates the cache at the next day's rollover so the
        comparison rolls forward to the new "yesterday".
        """
        return (
            datetime.now(IST).date().isoformat(),
            asset_id,
            segment_code,
            expiry,
        )

    def get_yesterday_totals(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str | None = None,
    ) -> dict | None:
        cache_key = self._daily_cache_key(asset_id, segment_code, expiry)
        if cache_key in self._yesterday_cache:
            return self._yesterday_cache[cache_key]
        result = self._compute_yesterday_totals(asset_id, segment_code, expiry)
        self._yesterday_cache[cache_key] = result
        return result

    def _compute_yesterday_totals(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str | None = None,
    ) -> dict | None:
        rows = self.snapshot_repository.list_latest_snapshot_rows(
            asset_id=asset_id,
            segment_code=segment_code,
            expiry=expiry,
        )
        if not rows:
            return None

        # IV is averaged only over the ±10 strikes around spot (the standardized ATM window,
        # mirroring _aggregate_gex_standardized) — the far-OTM wings of the smile carry
        # meaningless/extreme IV that would drag a flat all-strikes mean. The count/exposure
        # totals below stay full-chain (they sum real activity, never spuriously extreme).
        spot = max((float(row.get("underlying_price", 0) or 0) for row in rows), default=0.0)
        iv_window_strikes: set[float] = set()
        if spot > 0:
            unique_strikes = sorted({float(row["strike"]) for row in rows})
            below = [strike for strike in unique_strikes if strike <= spot]
            above = [strike for strike in unique_strikes if strike > spot]
            iv_window_strikes = set(below[-10:] + above[:10])  # graceful: ≤10 each side

        total_call_oi = 0.0
        total_put_oi = 0.0
        total_call_volume = 0.0
        total_put_volume = 0.0
        total_call_oi_change = 0.0
        total_put_oi_change = 0.0
        total_gex = 0.0
        call_iv_values: list[float] = []
        put_iv_values: list[float] = []

        for row in rows:
            option_type = str(row.get("option_type", ""))
            # All counts are stored in lots; gamma_exposure is the stored dealer-signed GEX.
            oi_change = float(row.get("oi_change_lots", 0) or 0)
            oi = float(row.get("oi_lots", 0) or 0)
            volume = float(row.get("volume_lots", 0) or 0)
            iv = float(row.get("iv", 0) or 0)
            in_iv_window = float(row.get("strike", 0) or 0) in iv_window_strikes
            total_gex += float(row.get("gamma_exposure", 0) or 0)

            if option_type == "CE":
                total_call_oi += oi
                total_call_volume += volume
                total_call_oi_change += oi_change
                if iv > 0 and in_iv_window:
                    call_iv_values.append(iv)
            elif option_type == "PE":
                total_put_oi += oi
                total_put_volume += volume
                total_put_oi_change += oi_change
                if iv > 0 and in_iv_window:
                    put_iv_values.append(iv)

        # Bracketing-ATM IV skew for yesterday — the same shared primitive that computes today's
        # live atm_gap (report["skew"]), so the two figures are never derived differently. None
        # when a leg is unreachable (e.g. spot <= 0 / one-sided chain) — never fabricated.
        atm = compute_atm_iv_gap(rows, underlying_price=spot) if spot > 0 else {
            "atm_call_iv": None, "atm_put_iv": None, "atm_gap": None,
        }

        return {
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "total_call_volume": total_call_volume,
            "total_put_volume": total_put_volume,
            "total_call_oi_change": total_call_oi_change,
            "total_put_oi_change": total_put_oi_change,
            "total_gex": total_gex,
            "pcr_oi": (total_put_oi / total_call_oi) if total_call_oi else 0,
            "pcr_volume": (total_put_volume / total_call_volume) if total_call_volume else 0,
            "avg_call_iv": (sum(call_iv_values) / len(call_iv_values)) if call_iv_values else 0,
            "avg_put_iv": (sum(put_iv_values) / len(put_iv_values)) if put_iv_values else 0,
            "atm_call_iv": atm["atm_call_iv"],
            "atm_put_iv": atm["atm_put_iv"],
            "atm_gap": atm["atm_gap"],
        }

    def get_oi_change_trophy_data(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str | None = None,
    ) -> dict | None:
        cache_key = self._daily_cache_key(asset_id, segment_code, expiry)
        if cache_key in self._trophy_cache:
            return self._trophy_cache[cache_key]
        result = self._compute_oi_change_trophy_data(asset_id, segment_code, expiry)
        self._trophy_cache[cache_key] = result
        return result

    def _compute_oi_change_trophy_data(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str | None = None,
    ) -> dict | None:
        rows = self.snapshot_repository.list_latest_snapshot_rows(
            asset_id=asset_id,
            segment_code=segment_code,
            expiry=expiry,
        )
        if not rows:
            return None

        def build_rankings(option_type: str) -> dict[float, str]:
            aggregated_changes: dict[float, float] = defaultdict(float)
            for row in rows:
                if str(row.get("option_type", "")) != option_type:
                    continue
                strike = float(row["strike"])
                aggregated_changes[strike] += float(row.get("oi_change_lots", 0) or 0)
            option_rows = list(aggregated_changes.items())
            positive_changes = [(strike, change) for strike, change in option_rows if change > 0]
            negative_changes = [(strike, change) for strike, change in option_rows if change < 0]
            positive_changes.sort(key=lambda item: item[1], reverse=True)
            negative_changes.sort(key=lambda item: item[1])

            # Each glyph carries its own +/- = the sign of YESTERDAY's OI change for that strike
            # (🥇+ = top positive, 🥇- = top negative). Rank is the trophy colour (🥇/🥈). This is
            # yesterday's snapshot data and is INDEPENDENT of today's green/red OI-change border,
            # which ranks today's live movers.
            rankings: dict[float, str] = {}
            for index, (strike, _) in enumerate(positive_changes[:2]):
                rankings[strike] = ("🥇" if index == 0 else "🥈") + "+"
            for index, (strike, _) in enumerate(negative_changes[:2]):
                rankings[strike] = ("🥇" if index == 0 else "🥈") + "-"
            return rankings

        return {
            "call_rankings": build_rankings("CE"),
            "put_rankings": build_rankings("PE"),
        }
