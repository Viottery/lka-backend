"""Versioned, local-only overrides for memory/background configuration."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime


class SettingsConflictError(ValueError):
    pass


class MemorySettingsStore:
    def __init__(self, conn_factory):
        self.conn_factory = conn_factory
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS memory_background_settings(
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                overrides TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("INSERT OR IGNORE INTO memory_background_settings VALUES(1,0,'{}',?)",
                         (datetime.now(UTC).isoformat(),))

    @contextmanager
    def _connect(self):
        conn = self.conn_factory()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def read(self):
        with self._connect() as conn:
            row = conn.execute("SELECT revision,overrides,updated_at FROM memory_background_settings WHERE id=1").fetchone()
        invalid = False
        try:
            overrides = json.loads(row[1])
            if not isinstance(overrides, dict):
                overrides, invalid = {}, True
        except (TypeError, ValueError):
            overrides, invalid = {}, True
        return {"revision": row[0], "overrides": overrides, "updated_at": row[2], "invalid": invalid}

    def save(self, overrides: dict, *, expected_revision: int):
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE memory_background_settings SET revision=revision+1,overrides=?,updated_at=? WHERE id=1 AND revision=?",
                (json.dumps(overrides, ensure_ascii=False), datetime.now(UTC).isoformat(), expected_revision),
            )
            if cursor.rowcount != 1:
                raise SettingsConflictError("settings_revision_conflict")
        return self.read()
