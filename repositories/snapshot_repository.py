"""Snapshot repository for offline collection and historical metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from analytics.historical_metrics import historical_metric_columns
from repositories.base import SQLiteRepository
from repositories.market_data_repository import _OPTION_ROW_COLUMNS, _OPTION_ROW_NULLABLE


IST = ZoneInfo("Asia/Kolkata")
_METRIC_KEY_COLUMNS = ("date", "underlying_asset_id", "segment_code")


def _parse_snapshot_timestamp(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    normalized = raw.replace("Z", "+00:00")
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # Older snapshot rows may be stored as naive local exchange time.
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


class SnapshotRepository(SQLiteRepository):
    """Repository for the snapshot DB."""

    def init_schema(self) -> None:
        with self.connection() as conn:
            # Same enriched per-contract shape as the live store, so daily EOD rows carry
            # gamma_exposure / lots and yesterday-totals reads them instead of recomputing.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS options_data (
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
                """
                CREATE INDEX IF NOT EXISTS idx_snapshot_options_lookup
                ON options_data (underlying_asset_id, segment_code, expiry, timestamp)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshot_options_fetch_group
                ON options_data (fetch_group_id, timestamp)
                """
            )
            metric_columns = historical_metric_columns()
            metric_ddl = ",\n                    ".join(
                f"{name} {column_ddl}" for name, column_ddl in metric_columns.items()
            )
            # Constant-maturity (CM-30) metrics are per-instrument, not per-expiry — one row
            # per (date, asset, segment). No expiry column, no migration (dev DBs are wiped clean).
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS historical_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    underlying_asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    {metric_ddl}
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshot_historical_unique
                ON historical_metrics (date, underlying_asset_id, segment_code)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collection_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    fetch_group_id TEXT,
                    underlying_asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    expiry TEXT,
                    status TEXT NOT NULL,
                    message TEXT
                )
                """
            )

    def insert_option_rows(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert enriched per-contract rows (same shape as the live store)."""
        if not rows:
            return 0
        self.init_schema()
        columns_sql = ", ".join(_OPTION_ROW_COLUMNS)
        values_sql = ", ".join(f":{name}" for name in _OPTION_ROW_COLUMNS)
        defaults = {name: None for name in _OPTION_ROW_NULLABLE}
        with self.connection() as conn:
            conn.executemany(
                f"INSERT INTO options_data ({columns_sql}) VALUES ({values_sql})",
                [{**defaults, **row} for row in rows],
            )
        return len(rows)

    def list_option_rows(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM options_data ORDER BY timestamp DESC, underlying_asset_id, segment_code, expiry, strike"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_daily_underlying_closes(
        self,
        asset_id: int,
        segment_code: str,
        *,
        limit_days: int,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                WITH grouped_ticks AS (
                    SELECT
                        DATE(timestamp) AS trade_date,
                        timestamp,
                        AVG(underlying_price) AS underlying_price
                    FROM options_data
                    WHERE underlying_asset_id = ?
                      AND segment_code = ?
                      AND underlying_price > 0
                    GROUP BY DATE(timestamp), timestamp
                ),
                ranked_ticks AS (
                    SELECT
                        trade_date,
                        underlying_price,
                        ROW_NUMBER() OVER (
                            PARTITION BY trade_date
                            ORDER BY timestamp DESC
                        ) AS rn
                    FROM grouped_ticks
                ),
                latest_per_day AS (
                    SELECT trade_date, underlying_price
                    FROM ranked_ticks
                    WHERE rn = 1
                    ORDER BY trade_date DESC
                    LIMIT ?
                )
                SELECT trade_date, underlying_price
                FROM latest_per_day
                ORDER BY trade_date ASC
                """,
                (asset_id, segment_code, int(max(1, limit_days))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_daily_atm_context_rows(
        self,
        asset_id: int,
        segment_code: str,
        *,
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        """Per-date ATM-context option rows across ALL collected expiries, for the CM-IV series.

        For each of the most recent ``lookback_days`` trade dates, returns every option row
        (all strikes, CE + PE) from **every active expiry** (``expiry >= DATE(timestamp)``) at
        that ``(date, expiry)``'s latest timestamp. The analytics layer reduces each
        ``(date, expiry)`` to a bracketing-strike ATM IV and variance-interpolates across
        expiries to a constant maturity. Returned columns:
        ``date, expiry, option_type, strike, iv, underlying_price``."""
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                WITH active AS (
                    SELECT
                        DATE(timestamp) AS d,
                        timestamp,
                        expiry,
                        strike,
                        iv,
                        underlying_price,
                        option_type
                    FROM options_data
                    WHERE underlying_asset_id = ?
                      AND segment_code = ?
                      AND expiry >= DATE(timestamp)
                      AND iv > 0
                      AND underlying_price > 0
                ),
                latest_ts AS (
                    SELECT d, expiry, MAX(timestamp) AS timestamp
                    FROM active
                    GROUP BY d, expiry
                ),
                recent_dates AS (
                    SELECT DISTINCT d FROM active ORDER BY d DESC LIMIT ?
                )
                SELECT a.d AS date, a.expiry AS expiry, a.option_type AS option_type,
                       a.strike AS strike, a.iv AS iv, a.underlying_price AS underlying_price
                FROM active a
                JOIN latest_ts lt ON a.d = lt.d AND a.expiry = lt.expiry AND a.timestamp = lt.timestamp
                JOIN recent_dates rd ON a.d = rd.d
                ORDER BY a.d ASC, a.expiry ASC, a.option_type ASC, a.strike ASC
                """,
                (asset_id, segment_code, int(max(1, lookback_days))),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_historical_metric(self, record: Mapping[str, Any]) -> int:
        self.init_schema()
        payload = dict(record)
        columns = list(payload.keys())
        update_columns = [name for name in columns if name not in _METRIC_KEY_COLUMNS]
        columns_sql = ", ".join(columns)
        values_sql = ", ".join(f":{name}" for name in columns)
        if update_columns:
            conflict_sql = "DO UPDATE SET " + ", ".join(f"{name} = excluded.{name}" for name in update_columns)
        else:
            conflict_sql = "DO NOTHING"
        with self.connection() as conn:
            conn.execute(
                f"""
                INSERT INTO historical_metrics ({columns_sql})
                VALUES ({values_sql})
                ON CONFLICT(date, underlying_asset_id, segment_code) {conflict_sql}
                """,
                payload,
            )
            row = conn.execute(
                """
                SELECT id
                FROM historical_metrics
                WHERE date = :date
                  AND underlying_asset_id = :underlying_asset_id
                  AND segment_code = :segment_code
                """,
                payload,
            ).fetchone()
        return int(row["id"]) if row is not None else 0

    def save_historical_metric(self, record: Mapping[str, Any]) -> int:
        """Backward-compatible alias for existing tests/callers."""
        return self.upsert_historical_metric(record)

    def record_collection_status(
        self,
        *,
        asset_id: int,
        segment_code: str,
        status: str,
        message: str,
        fetch_group_id: str | None = None,
        expiry: str | None = None,
        timestamp: str | None = None,
    ) -> int:
        self.init_schema()
        payload = {
            "timestamp": timestamp or datetime.now(IST).replace(tzinfo=None).isoformat(),
            "fetch_group_id": fetch_group_id,
            "underlying_asset_id": asset_id,
            "segment_code": segment_code,
            "expiry": expiry,
            "status": status,
            "message": message,
        }
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO collection_status (
                    timestamp, fetch_group_id, underlying_asset_id, segment_code, expiry, status, message
                )
                VALUES (
                    :timestamp, :fetch_group_id, :underlying_asset_id, :segment_code, :expiry, :status, :message
                )
                """,
                payload,
            )
        return int(cursor.lastrowid)

    def has_collection_for_date(self, date_iso: str) -> bool:
        """True if a successful collection_status row exists for the given IST date (``YYYY-MM-DD``).

        The cross-restart dedup authority for the EOD scheduler — a process restart after a completed
        run must not re-trigger. ``timestamp`` is naive IST ISO (e.g. ``2026-06-29T15:30:..``), so
        ``substr(...,1,10)`` is the date for both the ``T`` and legacy space-separated forms."""
        self.init_schema()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM collection_status "
                "WHERE substr(timestamp, 1, 10) = ? AND status = 'success' LIMIT 1",
                (date_iso,),
            ).fetchone()
        return row is not None

    def list_historical_metrics(
        self,
        asset_id: int,
        segment_code: str,
        end_date: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        query = """
            SELECT * FROM historical_metrics
            WHERE underlying_asset_id = ? AND segment_code = ?
        """
        params: list[Any] = [asset_id, segment_code]
        if end_date is not None:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_latest_historical_metric(
        self,
        asset_id: int,
        segment_code: str,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        rows = self.list_historical_metrics(asset_id, segment_code, end_date=end_date, limit=1)
        return rows[0] if rows else None

    def list_latest_snapshot_rows(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str | None = None,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        timestamp_query = """
            SELECT DISTINCT timestamp
            FROM options_data
            WHERE underlying_asset_id = ? AND segment_code = ?
        """
        timestamp_params: list[Any] = [asset_id, segment_code]
        if expiry is not None:
            timestamp_query += " AND expiry = ?"
            timestamp_params.append(expiry)

        cutoff_ist = datetime.now(IST).replace(hour=9, minute=0, second=0, microsecond=0)
        with self.connection() as conn:
            timestamp_rows = conn.execute(timestamp_query, tuple(timestamp_params)).fetchall()
            latest_timestamp: str | None = None
            latest_timestamp_dt: datetime | None = None
            for timestamp_row in timestamp_rows:
                raw_timestamp = str(timestamp_row["timestamp"] or "")
                parsed_timestamp = _parse_snapshot_timestamp(raw_timestamp)
                if parsed_timestamp is None or parsed_timestamp >= cutoff_ist:
                    continue
                if latest_timestamp_dt is None or parsed_timestamp > latest_timestamp_dt:
                    latest_timestamp = raw_timestamp
                    latest_timestamp_dt = parsed_timestamp
            if not latest_timestamp:
                return []

            row_query = """
                SELECT strike, option_type, oi_change_lots, oi_lots, volume_lots,
                       iv, gamma_exposure, underlying_price, expiry
                FROM options_data
                WHERE timestamp = ?
                  AND underlying_asset_id = ?
                  AND segment_code = ?
            """
            row_params: list[Any] = [latest_timestamp, asset_id, segment_code]
            if expiry is not None:
                row_query += " AND expiry = ?"
                row_params.append(expiry)
            rows = conn.execute(row_query, tuple(row_params)).fetchall()
        return [dict(row) for row in rows]

    def prune_older_than(self, cutoff_iso: str, *, cutoff_status_iso: str | None = None) -> dict[str, int]:
        """Delete snapshot options_data and collection_status rows older than the given cutoffs.

        historical_metrics is never touched — it is the permanent daily-aggregate record.
        Caller should run vacuum() after to reclaim disk space.
        """
        self.init_schema()
        status_cutoff = cutoff_status_iso or cutoff_iso
        with self.connection() as conn:
            options_deleted = conn.execute(
                "DELETE FROM options_data WHERE timestamp < ?", (cutoff_iso,)
            ).rowcount
            status_deleted = conn.execute(
                "DELETE FROM collection_status WHERE timestamp < ?", (status_cutoff,)
            ).rowcount
        return {"options_data": options_deleted, "collection_status": status_deleted}
