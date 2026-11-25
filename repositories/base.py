"""Shared SQLite repository helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping


class SQLiteRepository:
    """Base repository with a simple SQLite connection helper."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL lets readers run concurrently with the single writer, so live-fetch writes
        # no longer block the chain/bottom-panel/OA read callbacks (the tab-switch lag).
        # busy_timeout retries brief lock waits instead of erroring; synchronous=NORMAL
        # is durable under WAL (loses only on power loss). Pragmas are idempotent.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
        """Idempotently add any missing columns to ``table``.

        ``columns`` maps column name -> column type/DDL fragment (e.g. ``{"source_mode": "TEXT"}``).
        This is the one safe schema-evolution pattern for the repositories: existing
        rows keep their values; newly added columns are NULL until next written. Column
        DDL must NOT use a non-constant default (SQLite forbids ``ALTER TABLE ADD COLUMN``
        with ``CURRENT_TIMESTAMP`` and friends)."""
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, column_ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_ddl}")

    def vacuum(self) -> None:
        """Reclaim free pages after deletes. VACUUM cannot run inside a transaction,
        so it uses its own autocommit connection."""
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
