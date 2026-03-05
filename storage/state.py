from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    channel_key TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (channel_key, message_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_cursor (
                    channel_key TEXT PRIMARY KEY,
                    last_message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def is_processed(self, channel_key: str, message_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE channel_key = ? AND message_id = ?",
                (channel_key, message_id),
            ).fetchone()
            return row is not None

    def mark_processed(self, channel_key: str, message_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages(channel_key, message_id, processed_at)
                VALUES (?, ?, ?)
                """,
                (channel_key, message_id, now),
            )

    def get_last_message_id(self, channel_key: str) -> Optional[int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_message_id FROM channel_cursor WHERE channel_key = ?",
                (channel_key,),
            ).fetchone()
            return int(row[0]) if row else None

    def update_cursor(self, channel_key: str, message_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO channel_cursor(channel_key, last_message_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_key)
                DO UPDATE SET last_message_id=excluded.last_message_id, updated_at=excluded.updated_at
                """,
                (channel_key, message_id, now),
            )

    def cleanup_dedup(self, window_days: int) -> int:
        threshold = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM processed_messages WHERE processed_at < ?",
                (threshold,),
            )
            return int(cur.rowcount)
