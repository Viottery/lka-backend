"""Durable, server-owned effect constraints, independent of model context text."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable


class ToolConstraintStore:
    """Monotonic source constraints survive summaries, restarts and child runs.

    This is a controlled-runtime boundary, not protection against the OS owner
    reading or editing its SQLite files.
    """

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection] | None = None):
        self._factory = conn_factory
        self._lock = threading.RLock()
        self._memory: dict[tuple[str, str], set[str]] = {}
        if conn_factory:
            conn = conn_factory()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS tool_origin_constraints (kind TEXT NOT NULL, subject_id TEXT NOT NULL, constraint_id TEXT NOT NULL, PRIMARY KEY(kind,subject_id,constraint_id))")
                conn.commit()
            finally:
                conn.close()

    def add(self, subjects: list[tuple[str, str]], constraints: set[str]) -> None:
        with self._lock:
            if not self._factory:
                for subject in subjects:
                    self._memory.setdefault(subject, set()).update(constraints)
                return
            conn = self._factory()
            try:
                conn.executemany("INSERT OR IGNORE INTO tool_origin_constraints VALUES(?,?,?)",
                                 [(kind, key, value) for kind, key in subjects for value in constraints])
                conn.commit()
            finally:
                conn.close()

    def get(self, subjects: list[tuple[str, str]]) -> set[str]:
        with self._lock:
            if not self._factory:
                return set().union(*(self._memory.get(subject, set()) for subject in subjects))
            conn = self._factory()
            try:
                values: set[str] = set()
                for kind, key in subjects:
                    values.update(row[0] for row in conn.execute(
                        "SELECT constraint_id FROM tool_origin_constraints WHERE kind=? AND subject_id=?", (kind, key)))
                return values
            finally:
                conn.close()
