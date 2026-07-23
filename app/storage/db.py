"""SQLite helpers for the backend runtime."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def get_db_path(data_dir: Path) -> Path:
    """Return the database path inside the configured data directory."""

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "lka.sqlite3"


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with row access by column name."""

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    """Create the tables required by the current scaffold."""

    conn = connect(db_path)
    try:
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
