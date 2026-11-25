"""Collapsed market-data repository.

One store for the intraday option chain: `option_rows` (fully enriched, lot-scaled
per-contract rows — the single substrate that the chain table, charts, OA and
playback all read) and `pair_reports` (the per-pair chain-wide summary computed
once at ingest). Also holds the VIX series. Replaces the old split between
`live.db` (raw) and `enhanced.db` (enriched): rows are enriched once and stored
once, so nothing downstream re-derives them.

Counts are stored already in lots and exposures on the unified scale, so readers
fetch and display — they never divide by lot or recompute. The schema is defined
once here in full (no migrations: the DB is recreated fresh).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from repositories.base import SQLiteRepository

IST = ZoneInfo("Asia/Kolkata")


# Per-contract columns the Options Analysis metric charts may plot. Validated against
# this allow-list before being interpolated into SQL (never user-controlled free text).
_OA_METRIC_COLUMNS = {
    "last_price",
    "oi_lots",
    "volume_lots",
    "oi_change_lots",
    "straddle_oi_lots",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "bs_price",
    "volatility_exposure",
    "gamma_exposure",
}

# The full stored per-contract column set (identity + market + lots + exposures + analytics).
_OPTION_ROW_COLUMNS = (
    "timestamp", "fetch_group_id",
    "underlying_asset_id", "segment_code", "expiry", "strike", "option_type",
    "last_price", "bid_price", "ask_price", "underlying_price",
    "iv", "delta", "gamma", "theta", "vega",
    "oi_lots", "previous_oi_lots", "oi_change_lots", "volume_lots", "straddle_oi_lots",
    "gamma_exposure", "volatility_exposure",
    "bs_price", "moneyness_category", "risk_reward_ratio", "pain_point",
)

# Nullable per-contract analytics columns (default to NULL when a row omits them).
_OPTION_ROW_NULLABLE = ("bs_price", "moneyness_category", "risk_reward_ratio", "pain_point")

# The per-pair summary column set, in order. Identity first, then the scalar aggregates.
_PAIR_REPORT_COLUMNS = (
    "timestamp", "fetch_group_id",
    "underlying_asset_id", "segment_code", "expiry",
    "underlying_price", "lot_size", "strike_size", "row_count",
    "total_call_oi_lots", "total_put_oi_lots", "total_oi_lots",
    "total_call_volume_lots", "total_put_volume_lots", "total_volume_lots",
    "total_call_oi_change_lots", "total_put_oi_change_lots", "put_call_ratio",
    "total_gamma_exposure", "call_gamma_exposure", "put_gamma_exposure",
    "total_volatility_exposure", "call_volatility_exposure", "put_volatility_exposure",
    "gex_market_regime", "vol_regime",
    "avg_call_delta", "avg_put_delta", "delta_skew", "avg_theta", "max_vega_strike", "vega_skew",
    "max_call_oi_strike", "max_put_oi_strike", "max_pain_strike", "time_to_expiry",
    "risk_reversal_25", "call_iv_25d", "put_iv_25d",
)


def _aggregate_gex_standardized(rows: list[dict], strike_range: int = 10) -> list[dict]:
    """Per-snapshot aggregate GEX over a standardized ±strike_range window around spot.

    A "snapshot" is one pair's chain at one timestamp, so rows are grouped by timestamp
    (the query is already scoped to one asset/segment/expiry). For each snapshot, take
    the nearest ``strike_range`` strikes below and above spot, drop snapshots without
    that many on each side, and sum CE/PE gamma_exposure.
    """
    by_snapshot: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_snapshot[str(row["timestamp"])].append(row)

    results: list[dict] = []
    for snapshot_rows in by_snapshot.values():
        spot = float(snapshot_rows[0].get("underlying_price", 0) or 0)
        if spot <= 0:
            continue
        unique_strikes = sorted({float(row["strike"]) for row in snapshot_rows})
        below = [s for s in unique_strikes if s <= spot]
        above = [s for s in unique_strikes if s > spot]
        if len(below) < strike_range or len(above) < strike_range:
            continue
        selected = set(below[-strike_range:] + above[:strike_range])
        call_gex = sum(
            float(row.get("gamma_exposure", 0) or 0)
            for row in snapshot_rows
            if float(row["strike"]) in selected and row["option_type"] == "CE"
        )
        put_gex = sum(
            float(row.get("gamma_exposure", 0) or 0)
            for row in snapshot_rows
            if float(row["strike"]) in selected and row["option_type"] == "PE"
        )
        results.append(
            {
                "timestamp": snapshot_rows[0]["timestamp"],
                "total_gex": call_gex + put_gex,
                "call_gex": call_gex,
                "put_gex": put_gex,
            }
        )
    results.sort(key=lambda item: str(item["timestamp"]))
    return results


class MarketDataRepository(SQLiteRepository):
    """The single intraday option-chain store (enriched rows + per-pair summaries + VIX)."""

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    fetch_group_id TEXT,
                    underlying_asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    strike REAL NOT NULL,
                    option_type TEXT NOT NULL,
                    last_price REAL DEFAULT 0,
                    bid_price REAL DEFAULT 0,
                    ask_price REAL DEFAULT 0,
                    underlying_price REAL DEFAULT 0,
                    iv REAL DEFAULT 0,
                    delta REAL DEFAULT 0,
                    gamma REAL DEFAULT 0,
                    theta REAL DEFAULT 0,
                    vega REAL DEFAULT 0,
                    oi_lots REAL DEFAULT 0,
                    previous_oi_lots REAL DEFAULT 0,
                    oi_change_lots REAL DEFAULT 0,
                    volume_lots REAL DEFAULT 0,
                    straddle_oi_lots REAL DEFAULT 0,
                    gamma_exposure REAL DEFAULT 0,
                    volatility_exposure REAL DEFAULT 0,
                    bs_price REAL,
                    moneyness_category TEXT,
                    risk_reward_ratio REAL,
                    pain_point REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_rows_oa "
                "ON option_rows (underlying_asset_id, segment_code, expiry, strike, option_type, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_rows_pair "
                "ON option_rows (underlying_asset_id, segment_code, expiry, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_rows_fetch_group "
                "ON option_rows (fetch_group_id, timestamp)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pair_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    fetch_group_id TEXT,
                    underlying_asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    underlying_price REAL DEFAULT 0,
                    lot_size INTEGER DEFAULT 1,
                    strike_size REAL DEFAULT 1,
                    row_count INTEGER DEFAULT 0,
                    total_call_oi_lots REAL, total_put_oi_lots REAL, total_oi_lots REAL,
                    total_call_volume_lots REAL, total_put_volume_lots REAL, total_volume_lots REAL,
                    total_call_oi_change_lots REAL, total_put_oi_change_lots REAL, put_call_ratio REAL,
                    total_gamma_exposure REAL, call_gamma_exposure REAL, put_gamma_exposure REAL,
                    total_volatility_exposure REAL, call_volatility_exposure REAL, put_volatility_exposure REAL,
                    gex_market_regime TEXT, vol_regime TEXT,
                    avg_call_delta REAL, avg_put_delta REAL, delta_skew REAL,
                    avg_theta REAL, max_vega_strike REAL, vega_skew REAL,
                    max_call_oi_strike REAL, max_put_oi_strike REAL, max_pain_strike REAL, time_to_expiry REAL,
                    risk_reversal_25 REAL, call_iv_25d REAL, put_iv_25d REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_reports_pair "
                "ON pair_reports (underlying_asset_id, segment_code, expiry, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_reports_fetch_group "
                "ON pair_reports (fetch_group_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_volatility (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    value REAL NOT NULL,
                    previous_close REAL DEFAULT 0,
                    day_high REAL DEFAULT 0,
                    day_low REAL DEFAULT 0,
                    net_change REAL DEFAULT 0,
                    last_update_time TEXT
                )
                """
            )

    # --- option_rows -------------------------------------------------------------- #

    def insert_option_rows(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert enriched per-contract rows. Missing nullable analytics default to NULL."""
        if not rows:
            return 0
        self.init_schema()
        columns_sql = ", ".join(_OPTION_ROW_COLUMNS)
        values_sql = ", ".join(f":{name}" for name in _OPTION_ROW_COLUMNS)
        defaults = {name: None for name in _OPTION_ROW_NULLABLE}
        with self.connection() as conn:
            conn.executemany(
                f"INSERT INTO option_rows ({columns_sql}) VALUES ({values_sql})",
                [{**defaults, **row} for row in rows],
            )
        return len(rows)

    def list_rows_for_fetch_group(self, fetch_group_id: str) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM option_rows
                WHERE fetch_group_id = ?
                ORDER BY underlying_asset_id, segment_code, expiry, strike, option_type
                """,
                (fetch_group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_underlying_price_in_range(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str | None = None,
        *,
        since_iso: str,
        until_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Per-fetch spot series, ascending, over ``(since_iso, until_iso]``.

        Reads the spot straight from ``pair_reports`` — which already holds **one row per pair per
        fetch** with ``underlying_price`` (a pair-level scalar) — instead of scanning every strike in
        ``option_rows`` and averaging. ``since_iso`` is an **exclusive** lower bound (``timestamp >``)
        so the incremental cache append never double-counts the last cached point; ``until_iso`` is an
        **inclusive** upper bound (``timestamp <=``) so playback keeps the captured-moment candle.
        ``expiry=None`` (aggregate) drops the expiry filter; the ``GROUP BY timestamp, fetch_group_id``
        with a bare ``underlying_price`` yields 1 row per fetch (per-expiry) / 1 point per wave
        (aggregate — the instrument's spot is identical across expiries). Index-bounded on the
        ``(asset, segment, expiry, timestamp)`` ``pair_reports`` index.
        """
        self.init_schema()
        query = """
            SELECT timestamp, fetch_group_id, underlying_price
            FROM pair_reports
            WHERE underlying_asset_id = ? AND segment_code = ?
        """
        params: list[Any] = [asset_id, segment_code]
        if expiry is not None:
            query += " AND expiry = ?"
            params.append(expiry)
        query += " AND timestamp > ?"
        params.append(since_iso)
        if until_iso is not None:
            query += " AND timestamp <= ?"
            params.append(until_iso)
        query += " GROUP BY timestamp, fetch_group_id ORDER BY timestamp ASC"
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def list_fetch_group_summaries(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT fetch_group_id, MIN(timestamp) AS start_ts, MAX(timestamp) AS end_ts,
                       COUNT(DISTINCT underlying_asset_id || '|' || segment_code || '|' || expiry) AS pair_count
                FROM option_rows
                WHERE fetch_group_id IS NOT NULL AND fetch_group_id != ''
                GROUP BY fetch_group_id
                ORDER BY start_ts DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_fetch_group_pair_summaries(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                WITH pair_rows AS (
                    SELECT fetch_group_id, underlying_asset_id, segment_code, expiry,
                           MIN(timestamp) AS start_ts, MAX(timestamp) AS end_ts
                    FROM option_rows
                    WHERE fetch_group_id IS NOT NULL AND fetch_group_id != ''
                    GROUP BY fetch_group_id, underlying_asset_id, segment_code, expiry
                ),
                pair_counts AS (
                    SELECT fetch_group_id, COUNT(*) AS pair_count FROM pair_rows GROUP BY fetch_group_id
                )
                SELECT pair_rows.fetch_group_id, pair_rows.underlying_asset_id, pair_rows.segment_code,
                       pair_rows.expiry, pair_rows.start_ts, pair_rows.end_ts, pair_counts.pair_count
                FROM pair_rows
                INNER JOIN pair_counts ON pair_counts.fetch_group_id = pair_rows.fetch_group_id
                ORDER BY pair_rows.start_ts DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_group_id_bounds(self) -> tuple[int, int]:
        """O(1) (MIN(id), MAX(id)) over option_rows — a cheap catalog-change signature.

        Each subquery is optimized to a b-tree edge lookup (no scan). MAX(id) advances on
        any new write; MIN(id) advances on a prune. Unchanged bounds ⇒ the playback catalog
        is identical, so the caller can serve its cache without a full GROUP BY scan.
        """
        self.init_schema()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT (SELECT MIN(id) FROM option_rows) AS min_id, "
                "(SELECT MAX(id) FROM option_rows) AS max_id"
            ).fetchone()
        min_id = int(row["min_id"]) if row and row["min_id"] is not None else 0
        max_id = int(row["max_id"]) if row and row["max_id"] is not None else 0
        return (min_id, max_id)

    def list_fetch_group_pair_summaries_since(self, min_row_id: int) -> list[dict[str, Any]]:
        """Per-(fetch_group, pair) summaries for rows with ``id > min_row_id``.

        Mirrors ``list_fetch_group_pair_summaries`` but scoped to new rows, plus a
        ``max_row_id`` per entry. A fetch group is written in one wave (contiguous ids), so
        with ``min_row_id`` set to the previously-seen MAX(id) this returns exactly the new
        groups — letting the catalog append incrementally. ``min_row_id=0`` returns everything
        (the cold-build path). ``max_row_id`` lets the caller drop entries a prune deleted
        (``max_row_id < MIN(id)``).
        """
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                WITH pair_rows AS (
                    SELECT fetch_group_id, underlying_asset_id, segment_code, expiry,
                           MIN(timestamp) AS start_ts, MAX(timestamp) AS end_ts, MAX(id) AS max_row_id
                    FROM option_rows
                    WHERE fetch_group_id IS NOT NULL AND fetch_group_id != '' AND id > ?
                    GROUP BY fetch_group_id, underlying_asset_id, segment_code, expiry
                ),
                pair_counts AS (
                    SELECT fetch_group_id, COUNT(*) AS pair_count FROM pair_rows GROUP BY fetch_group_id
                )
                SELECT pair_rows.fetch_group_id, pair_rows.underlying_asset_id, pair_rows.segment_code,
                       pair_rows.expiry, pair_rows.start_ts, pair_rows.end_ts, pair_rows.max_row_id,
                       pair_counts.pair_count
                FROM pair_rows
                INNER JOIN pair_counts ON pair_counts.fetch_group_id = pair_rows.fetch_group_id
                ORDER BY pair_rows.start_ts DESC
                """,
                (min_row_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Options Analysis reads --------------------------------------------------- #

    def list_available_contracts(
        self, asset_id: int, segment_code: str, expiry: str, *, since_iso: str
    ) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT strike, option_type, expiry
                FROM option_rows
                WHERE underlying_asset_id = ? AND segment_code = ? AND expiry = ? AND timestamp >= ?
                ORDER BY strike, option_type
                """,
                (asset_id, segment_code, expiry, since_iso),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_option_metric_timeseries(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str,
        contracts: list[tuple[float, str]],
        metric: str,
        *,
        since_iso: str,
    ) -> list[dict[str, Any]]:
        """Per-contract time-series of one metric. ``metric`` validated against the allow-list."""
        if metric not in _OA_METRIC_COLUMNS or not contracts:
            return []
        self.init_schema()
        clauses: list[str] = []
        params: list[Any] = [asset_id, segment_code, expiry, since_iso]
        for strike, option_type in contracts:
            clauses.append("(strike = ? AND option_type = ?)")
            params.extend([strike, option_type])
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, strike, option_type, {metric} AS value
                FROM option_rows
                WHERE underlying_asset_id = ? AND segment_code = ? AND expiry = ? AND timestamp >= ?
                  AND ({' OR '.join(clauses)})
                ORDER BY timestamp, strike, option_type
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_aggregate_gex_in_range(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str,
        *,
        since_iso: str,
        until_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Standardized per-snapshot aggregate GEX for one pair over ``(since_iso, until_iso)``.

        ``since_iso`` is an **exclusive** lower bound (``timestamp > since_iso``) so the same
        method serves two callers without double-counting a snapshot: a fresh whole day
        (``since='<date>T00:00:00'``, ``until='<next-date>T00:00:00'``) and the live-day delta
        (``since=<last cached timestamp>``, ``until=None``). Lets ``ViewDataService`` cache each
        day's points once and append only newer snapshots per tick, instead of re-aggregating
        the whole window every refresh.
        """
        self.init_schema()
        query = """
            SELECT timestamp, strike, option_type, gamma_exposure, underlying_price
            FROM option_rows
            WHERE underlying_asset_id = ? AND segment_code = ? AND expiry = ? AND timestamp > ?
        """
        params: list[Any] = [asset_id, segment_code, expiry, since_iso]
        if until_iso is not None:
            query += " AND timestamp < ?"
            params.append(until_iso)
        query += " ORDER BY timestamp, strike"
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]
        return _aggregate_gex_standardized(rows, strike_range=10)

    # --- pair_reports ------------------------------------------------------------- #

    def insert_pair_report(self, record: Mapping[str, Any]) -> int:
        """Insert one per-pair summary row (the flattened ``build_pair_report`` output)."""
        self.init_schema()
        columns_sql = ", ".join(_PAIR_REPORT_COLUMNS)
        values_sql = ", ".join(f":{name}" for name in _PAIR_REPORT_COLUMNS)
        payload = {name: record.get(name) for name in _PAIR_REPORT_COLUMNS}
        with self.connection() as conn:
            cursor = conn.execute(
                f"INSERT INTO pair_reports ({columns_sql}) VALUES ({values_sql})", payload
            )
            return int(cursor.lastrowid)

    def get_pair_report(
        self, fetch_group_id: str, asset_id: int, segment_code: str, expiry: str
    ) -> dict[str, Any] | None:
        self.init_schema()
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM pair_reports
                WHERE fetch_group_id = ? AND underlying_asset_id = ? AND segment_code = ? AND expiry = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (fetch_group_id, asset_id, segment_code, expiry),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_risk_reversal_series(
        self, asset_id: int, segment_code: str, expiry: str
    ) -> list[dict[str, Any]]:
        """Intraday 25Δ risk-reversal series for one pair — **today's session only** (from 09:00 IST).

        Returns the stored per-fetch ``risk_reversal_25`` + the two 25Δ leg IVs, ascending by time.
        Index-bounded on the ``(asset, segment, expiry, timestamp)`` index.
        """
        self.init_schema()
        cutoff = f"{datetime.now(IST).date().isoformat()}T09:00:00"
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, risk_reversal_25, call_iv_25d, put_iv_25d
                FROM pair_reports
                WHERE underlying_asset_id = ? AND segment_code = ? AND expiry = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (asset_id, segment_code, expiry, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- VIX ---------------------------------------------------------------------- #

    def save_vix_row(self, value: float, metadata: Mapping[str, Any] | None = None) -> int:
        self.init_schema()
        payload = {
            "timestamp": datetime.now(IST).replace(tzinfo=None).isoformat(),
            "value": value,
            "previous_close": float((metadata or {}).get("previous_close", 0)),
            "day_high": float((metadata or {}).get("day_high", 0)),
            "day_low": float((metadata or {}).get("day_low", 0)),
            "net_change": float((metadata or {}).get("net_change", 0)),
            "last_update_time": str((metadata or {}).get("last_update_time", "")),
        }
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO market_volatility (
                    timestamp, value, previous_close, day_high, day_low, net_change, last_update_time
                )
                VALUES (:timestamp, :value, :previous_close, :day_high, :day_low, :net_change, :last_update_time)
                """,
                payload,
            )
            return int(cursor.lastrowid)

    def list_vix_rows(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM market_volatility ORDER BY timestamp DESC").fetchall()
        return [dict(row) for row in rows]

    def latest_vix_row(self) -> dict[str, Any] | None:
        """The newest VIX row only — the O(1) read ``vix_status`` needs on every fetch wave
        (``list_vix_rows`` materializes the whole retention window just to use its first row)."""
        self.init_schema()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM market_volatility ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    # --- retention ---------------------------------------------------------------- #

    def prune_older_than(self, cutoff_iso: str) -> dict[str, int]:
        """Delete option_rows, pair_reports and VIX rows older than cutoff. Caller vacuums."""
        self.init_schema()
        with self.connection() as conn:
            options_deleted = conn.execute(
                "DELETE FROM option_rows WHERE timestamp < ?", (cutoff_iso,)
            ).rowcount
            reports_deleted = conn.execute(
                "DELETE FROM pair_reports WHERE timestamp < ?", (cutoff_iso,)
            ).rowcount
            vix_deleted = conn.execute(
                "DELETE FROM market_volatility WHERE timestamp < ?", (cutoff_iso,)
            ).rowcount
        return {
            "option_rows": options_deleted,
            "pair_reports": reports_deleted,
            "market_volatility": vix_deleted,
        }
