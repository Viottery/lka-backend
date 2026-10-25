"""Bounded retries for deterministic SQLite persistence, never external effects."""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing


def is_sqlite_busy(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(exc, sqlite3.OperationalError) and code is not None and code & 0xff == sqlite3.SQLITE_BUSY


def begin_sqlite_write(conn: sqlite3.Connection) -> None:
    """Retry only write-lock admission, before the caller can produce effects."""
    for attempt in range(2):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if attempt or conn.in_transaction or not is_sqlite_busy(exc):
                raise
            time.sleep(0.05)


def persist_sqlite[T](connect: Callable[[], sqlite3.Connection], operation: Callable[[sqlite3.Connection], T]) -> T:
    """Run only deterministic SQL on fresh, rolled-back connections (two attempts).

    Callers must not include tools, model calls, workflows or external callbacks.
    Existing connection busy timeouts remain unchanged; exhausted errors propagate.
    """
    for attempt in range(2):
        try:
            with closing(connect()) as conn, conn:
                return operation(conn)
        except sqlite3.OperationalError as exc:
            if attempt or not is_sqlite_busy(exc):
                raise
            time.sleep(0.05)
    raise AssertionError("Unreachable SQLite retry state")
