"""Postgres access: one connection pool plus a tiny forward-only migration runner."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("okiedoke.db")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATE_LOCK = 7_204_001   # advisory-lock keys (arbitrary, app-unique)
SYNC_LOCK = 7_204_002


class Database:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 8):
        self.url = url
        self.pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": True},
            # Reconnect transparently after a Postgres restart instead of
            # handing out a dead connection.
            check=ConnectionPool.check_connection,
            open=False,
        )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=30)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        """Autocommit connection; wrap writes in `with conn.transaction():`."""
        with self.pool.connection() as conn:
            yield conn

    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def migrate(self) -> list[str]:
        """Apply any unapplied migrations/NNN_*.sql in order, each in its own
        transaction. Safe to call from several processes at once."""
        applied: list[str] = []
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATE_LOCK,))
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
                done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    if path.stem in done:
                        continue
                    log.info("applying migration %s", path.stem)
                    with conn.transaction():
                        conn.execute(path.read_text())
                        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                    applied.append(path.stem)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATE_LOCK,))
        return applied
