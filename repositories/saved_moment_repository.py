"""Pair-based saved-moment persistence."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from repositories.base import SQLiteRepository

IST = ZoneInfo("Asia/Kolkata")


class SavedMomentRepository(SQLiteRepository):
    """Repository for pair-based Mark This Moment persistence."""

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_moments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    label TEXT,
                    save_batch_id TEXT,
                    fetch_group_id TEXT,
                    source_mode TEXT,
                    view_kind TEXT,
                    underlying_asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    underlying_price REAL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_saved_moments_pair
                ON saved_moments (underlying_asset_id, segment_code, expiry, timestamp DESC)
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(saved_moments)").fetchall()
            }
            if "fetch_group_id" not in columns:
                conn.execute("ALTER TABLE saved_moments ADD COLUMN fetch_group_id TEXT")
            if "source_mode" not in columns:
                conn.execute("ALTER TABLE saved_moments ADD COLUMN source_mode TEXT")
            if "view_kind" not in columns:
                conn.execute("ALTER TABLE saved_moments ADD COLUMN view_kind TEXT")

    def save_moment(
        self,
        asset_id: int,
        segment_code: str,
        expiry: str,
        label: str | None = None,
        underlying_price: float = 0,
        timestamp: str | None = None,
        save_batch_id: str | None = None,
        fetch_group_id: str | None = None,
        source_mode: str | None = None,
        view_kind: str | None = None,
    ) -> int:
        self.init_schema()
        ts = timestamp or datetime.now(IST).replace(tzinfo=None).isoformat()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO saved_moments (
                    timestamp, label, save_batch_id, fetch_group_id, source_mode, view_kind,
                    underlying_asset_id, segment_code, expiry, underlying_price
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    label,
                    save_batch_id,
                    fetch_group_id,
                    source_mode,
                    view_kind,
                    asset_id,
                    segment_code,
                    expiry,
                    underlying_price,
                ),
            )
            return int(cursor.lastrowid)

    def list_assets(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT underlying_asset_id, segment_code
                FROM saved_moments
                ORDER BY segment_code, underlying_asset_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_expiries(self, asset_id: int, segment_code: str) -> list[str]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT expiry
                FROM saved_moments
                WHERE underlying_asset_id = ? AND segment_code = ?
                ORDER BY expiry
                """,
                (asset_id, segment_code),
            ).fetchall()
        return [str(row["expiry"]) for row in rows]

    def list_moments(self, asset_id: int, segment_code: str, expiry: str) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM saved_moments
                WHERE underlying_asset_id = ? AND segment_code = ? AND expiry = ?
                ORDER BY timestamp DESC, id DESC
                """,
                (asset_id, segment_code, expiry),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_moment(self, moment_id: int) -> dict[str, Any] | None:
        self.init_schema()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM saved_moments WHERE id = ?",
                (moment_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def delete_moment(self, moment_id: int) -> None:
        self.init_schema()
        with self.connection() as conn:
            conn.execute("DELETE FROM saved_moments WHERE id = ?", (moment_id,))

    def prune_older_than(self, cutoff_iso: str) -> int:
        """Delete saved_moments with timestamp < cutoff_iso (naive wall-clock IST).
        Shares market.db with MarketDataRepository; caller runs MarketDataRepository.vacuum() after."""
        self.init_schema()
        with self.connection() as conn:
            return conn.execute(
                "DELETE FROM saved_moments WHERE timestamp < ?", (cutoff_iso,)
            ).rowcount
