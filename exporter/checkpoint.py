"""SQLite checkpoint so restarts do not double-count usage events."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Checkpoint:
    def __init__(self, path: str | Path, max_events: int = 200_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_events = max_events
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def healthy(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def seen(self, event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM seen WHERE id = ?", (event_id,)).fetchone()
        return row is not None

    def mark(self, event_id: str, ts: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen (id, ts) VALUES (?, ?)",
                (event_id, int(ts)),
            )
            self._conn.commit()
            count = self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            if count > self.max_events:
                extra = count - self.max_events
                self._conn.execute(
                    "DELETE FROM seen WHERE id IN (SELECT id FROM seen ORDER BY ts ASC, id ASC LIMIT ?)",
                    (extra,),
                )
                self._conn.commit()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        if row is None:
            return default
        return str(row[0])

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )
            self._conn.commit()
