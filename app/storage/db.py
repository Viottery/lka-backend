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

            CREATE TABLE IF NOT EXISTS mail_accounts (
                account_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                email_address TEXT NOT NULL,
                display_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider, email_address)
            );

            CREATE TABLE IF NOT EXISTS mail_messages (
                message_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                folder TEXT NOT NULL,
                subject TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipients TEXT NOT NULL,
                cc TEXT NOT NULL,
                received_at TEXT,
                body_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES mail_accounts(account_id),
                UNIQUE(account_id, external_id)
            );

            CREATE TABLE IF NOT EXISTS mail_attachments (
                attachment_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                name TEXT NOT NULL,
                content_type TEXT,
                size INTEGER,
                is_downloaded INTEGER NOT NULL DEFAULT 0,
                local_path TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(message_id) REFERENCES mail_messages(message_id),
                UNIQUE(message_id, external_id)
            );

            CREATE TABLE IF NOT EXISTS mail_chunks (
                chunk_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(message_id) REFERENCES mail_messages(message_id),
                UNIQUE(message_id, chunk_index)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS mail_messages_fts USING fts5(
                message_id UNINDEXED,
                subject,
                sender,
                folder,
                body_text
            );

            CREATE TABLE IF NOT EXISTS mail_matters (
                matter_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mail_matter_links (
                matter_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(matter_id, message_id),
                FOREIGN KEY(matter_id) REFERENCES mail_matters(matter_id),
                FOREIGN KEY(message_id) REFERENCES mail_messages(message_id)
            );

            CREATE TABLE IF NOT EXISTS mail_processing_runs (
                run_id TEXT PRIMARY KEY,
                query TEXT,
                status TEXT NOT NULL,
                processed_messages INTEGER NOT NULL,
                matters_created INTEGER NOT NULL,
                provider TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
