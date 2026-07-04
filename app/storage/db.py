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

            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_text TEXT NOT NULL,
                workspace_path TEXT,
                frontend TEXT,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                trace_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                user_goal TEXT NOT NULL,
                intent TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                context_summary TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS confirmations (
                confirmation_id TEXT PRIMARY KEY,
                decision TEXT,
                status TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
