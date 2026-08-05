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

CURRENT_SESSION_CONTEXT_WINDOW_COLUMNS = {
    "session_id",
    "token_budget",
    "summary",
    "recent_messages",
    "token_estimate",
    "updated_at",
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

    if _table_exists(conn, "traces") and not CURRENT_TRACE_COLUMNS.issubset(
        _table_columns(conn, "traces")
    ):
        legacy_name = _next_legacy_table_name(conn, "traces")
        conn.execute(f"ALTER TABLE traces RENAME TO {legacy_name}")

    _migrate_agent_session_context_windows(conn)


def _migrate_agent_session_context_windows(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "agent_session_context_windows"):
        return

    columns = _table_columns(conn, "agent_session_context_windows")
    if "recent_messages" not in columns:
        conn.execute(
            """
            ALTER TABLE agent_session_context_windows
            ADD COLUMN recent_messages TEXT NOT NULL DEFAULT '[]'
            """
        )
    if "core_messages" in columns:
        conn.execute(
            """
            UPDATE agent_session_context_windows
            SET recent_messages = core_messages
            WHERE recent_messages = '[]' AND core_messages != '[]'
            """
        )

    columns = _table_columns(conn, "agent_session_context_windows")
    if columns == CURRENT_SESSION_CONTEXT_WINDOW_COLUMNS:
        return

    legacy_name = _next_legacy_table_name(conn, "agent_session_context_windows")
    conn.execute(f"ALTER TABLE agent_session_context_windows RENAME TO {legacy_name}")
    conn.execute(
        """
        CREATE TABLE agent_session_context_windows (
            session_id TEXT PRIMARY KEY,
            token_budget INTEGER NOT NULL,
            summary TEXT NOT NULL,
            recent_messages TEXT NOT NULL,
            token_estimate INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES agent_sessions(session_id)
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO agent_session_context_windows(
            session_id, token_budget, summary, recent_messages, token_estimate, updated_at
        )
        SELECT session_id, token_budget, summary, recent_messages, token_estimate, updated_at
        FROM {legacy_name}
        """
    )


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

            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_session_messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES agent_sessions(session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated_at
                ON agent_sessions(updated_at);

            CREATE INDEX IF NOT EXISTS idx_agent_session_messages_session_id
                ON agent_session_messages(session_id, created_at);

            CREATE TABLE IF NOT EXISTS agent_session_context_windows (
                session_id TEXT PRIMARY KEY,
                token_budget INTEGER NOT NULL,
                summary TEXT NOT NULL,
                recent_messages TEXT NOT NULL,
                token_estimate INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES agent_sessions(session_id)
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

            CREATE TABLE IF NOT EXISTS mail_sync_state (
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                folder TEXT NOT NULL,
                status TEXT NOT NULL,
                last_sync_at TEXT NOT NULL,
                next_link TEXT,
                delta_link TEXT,
                last_result_payload TEXT NOT NULL,
                PRIMARY KEY(provider, account_id, folder),
                FOREIGN KEY(account_id) REFERENCES mail_accounts(account_id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
