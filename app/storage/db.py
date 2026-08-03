"""SQLite helpers for the backend runtime."""

from __future__ import annotations

import sqlite3
from pathlib import Path


CURRENT_TRACE_COLUMNS = {
    "trace_id",
    "session_id",
    "context_id",
    "status",
    "events_payload",
    "verification_clues",
    "created_at",
}


def get_db_path(data_dir: Path) -> Path:
    """Return the database path inside the configured data directory."""

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "lka.sqlite3"


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with row access by column name."""

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _next_legacy_table_name(conn: sqlite3.Connection, base_name: str) -> str:
    index = 1
    while True:
        candidate = f"{base_name}_legacy_{index}"
        if not _table_exists(conn, candidate):
            return candidate
        index += 1


def _prepare_schema_migrations(conn: sqlite3.Connection) -> None:
    """Preserve incompatible legacy tables before creating current schema."""

    if not _table_exists(conn, "traces"):
        return
    if CURRENT_TRACE_COLUMNS.issubset(_table_columns(conn, "traces")):
        return

    legacy_name = _next_legacy_table_name(conn, "traces")
    conn.execute(f"ALTER TABLE traces RENAME TO {legacy_name}")


def init_db(db_path: Path) -> None:
    """Create the tables required by the current scaffold."""

    conn = connect(db_path)
    try:
        _prepare_schema_migrations(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                workspace_id TEXT PRIMARY KEY,
                workspace_path TEXT NOT NULL,
                source_frontend TEXT,
                status TEXT NOT NULL,
                indexed_files INTEGER NOT NULL DEFAULT 0,
                indexed_chunks INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS runtime_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                context_id TEXT,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                context_id TEXT,
                status TEXT NOT NULL,
                events_payload TEXT NOT NULL,
                verification_clues TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
