from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SharedZones:
    def __init__(
        self,
        path: Path | None,
        rate_limit_per_minute: int,
        rate_limit_burst: int,
        connection_limit: int,
        connection_limit_per_client: int,
    ) -> None:
        self.path = path
        self.rate_limit_per_minute = rate_limit_per_minute
        self.rate_limit_burst = rate_limit_burst or rate_limit_per_minute
        self.connection_limit = connection_limit
        self.connection_limit_per_client = connection_limit_per_client
        self.enabled = path is not None
        if self.enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._session() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS rate_buckets "
                    "(client TEXT PRIMARY KEY, tokens REAL NOT NULL, updated_at REAL NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS connection_counts "
                    "(client TEXT PRIMARY KEY, count INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS counters "
                    "(name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
                )
                db.execute("INSERT OR IGNORE INTO counters(name, value) VALUES ('total_connections', 0)")

    def _connect(self) -> sqlite3.Connection:
        if self.path is None:
            raise RuntimeError("shared zone is disabled")
        return sqlite3.connect(self.path, timeout=2.0, isolation_level="IMMEDIATE")

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def allow_request(self, client: str) -> bool:
        if not self.enabled or self.rate_limit_per_minute <= 0:
            return True
        now = time.time()
        refill_per_second = self.rate_limit_per_minute / 60
        with self._session() as db:
            row = db.execute(
                "SELECT tokens, updated_at FROM rate_buckets WHERE client = ?",
                (client,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO rate_buckets(client, tokens, updated_at) VALUES (?, ?, ?)",
                    (client, max(0, self.rate_limit_burst - 1), now),
                )
                return True
            tokens, updated_at = float(row[0]), float(row[1])
            tokens = min(self.rate_limit_burst, tokens + max(0.0, now - updated_at) * refill_per_second)
            if tokens < 1:
                db.execute(
                    "UPDATE rate_buckets SET tokens = ?, updated_at = ? WHERE client = ?",
                    (tokens, now, client),
                )
                return False
            db.execute(
                "UPDATE rate_buckets SET tokens = ?, updated_at = ? WHERE client = ?",
                (tokens - 1, now, client),
            )
            return True

    def acquire_connection(self, client: str) -> bool:
        if not self.enabled:
            return True
        with self._session() as db:
            total = int(
                db.execute("SELECT value FROM counters WHERE name = 'total_connections'").fetchone()[0]
            )
            client_count_row = db.execute(
                "SELECT count FROM connection_counts WHERE client = ?",
                (client,),
            ).fetchone()
            client_count = int(client_count_row[0]) if client_count_row else 0
            if self.connection_limit > 0 and total >= self.connection_limit:
                return False
            if self.connection_limit_per_client > 0 and client_count >= self.connection_limit_per_client:
                return False
            db.execute(
                "UPDATE counters SET value = value + 1 WHERE name = 'total_connections'"
            )
            db.execute(
                "INSERT INTO connection_counts(client, count) VALUES (?, 1) "
                "ON CONFLICT(client) DO UPDATE SET count = count + 1",
                (client,),
            )
            return True

    def release_connection(self, client: str) -> None:
        if not self.enabled:
            return
        with self._session() as db:
            db.execute(
                "UPDATE counters SET value = MAX(value - 1, 0) WHERE name = 'total_connections'"
            )
            db.execute(
                "UPDATE connection_counts SET count = MAX(count - 1, 0) WHERE client = ?",
                (client,),
            )
            db.execute("DELETE FROM connection_counts WHERE count <= 0")
