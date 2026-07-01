"""Shared PostgreSQL connection-pool lifecycle for the server services.

Both the ingest service and the watchdog open one psycopg pool at startup and
reuse it. They run as separate processes, so each owns its own DbPool instance;
this module holds the shared open/close/get logic (DSN resolution and the
"not initialized" guard) so the behaviour lives in one place.
"""
from __future__ import annotations

import os

from psycopg_pool import ConnectionPool


class DbPool:
    """One psycopg connection pool, opened once per process."""

    def __init__(self) -> None:
        self._pool: ConnectionPool | None = None

    def open(self, dsn: str | None = None, *, max_size: int) -> ConnectionPool:
        # Guard against a double open() overwriting (and thus leaking) a live
        # pool. The pool is meant to be opened exactly once per process at
        # startup, so a second call is a bug — fail loud rather than strand the
        # first pool's connections.
        if self._pool is not None:
            raise RuntimeError("connection pool already open; call close() first")
        dsn = dsn or os.environ["CRYO_DB_DSN"]
        self._pool = ConnectionPool(dsn, min_size=1, max_size=max_size, open=True)
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def get(self) -> ConnectionPool:
        if self._pool is None:
            raise RuntimeError("connection pool not initialized; call init_pool() first")
        return self._pool
