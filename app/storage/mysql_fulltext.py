"""Native InnoDB full-text indexes and reviewed SQLite FTS query adaptation.

The application keeps using its existing FTS5-shaped SQL contract. MySQL
stores the derived index in ordinary InnoDB tables with ngram FULLTEXT indexes;
this module is the explicit adapter for MATCH, rank and snippet expressions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.storage.mysql import MySQLConnection, MySQLOperationalError


class MySQLFullTextError(MySQLOperationalError):
    category = "fulltext_compatibility"


_FIELDS: dict[str, tuple[str, ...]] = {
    "mail_messages_fts": ("subject", "sender", "folder", "body_text"),
    "matters_fts": ("title", "summary", "tags"),
    "knowledge_chunks_fts": ("title", "text", "source_type"),
    "memory_entries_fts": ("content", "memory_type"),
    "conversation_history_fts": ("content",),
}
_SNIPPET_FIELDS: dict[str, tuple[str, ...]] = {
    "mail_messages_fts": ("message_id", "subject", "sender", "folder", "body_text"),
    "matters_fts": ("matter_id", "title", "summary", "tags"),
    "knowledge_chunks_fts": ("chunk_id", "document_id", "title", "text", "source_type"),
}
_FTS_DDL = (
    """CREATE TABLE IF NOT EXISTS mail_messages_fts (
        rowid BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        message_id VARCHAR(768) COLLATE utf8mb4_0900_bin NOT NULL,
        subject LONGTEXT, sender LONGTEXT, folder LONGTEXT, body_text LONGTEXT,
        UNIQUE KEY mail_messages_fts_message_id (message_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin""",
    """CREATE TABLE IF NOT EXISTS matters_fts (
        rowid BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        matter_id VARCHAR(768) COLLATE utf8mb4_0900_bin NOT NULL,
        title LONGTEXT, summary LONGTEXT, tags LONGTEXT,
        UNIQUE KEY matters_fts_matter_id (matter_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin""",
    """CREATE TABLE IF NOT EXISTS knowledge_chunks_fts (
        rowid BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        chunk_id VARCHAR(768) COLLATE utf8mb4_0900_bin NOT NULL,
        document_id VARCHAR(768) COLLATE utf8mb4_0900_bin NOT NULL,
        title LONGTEXT, text LONGTEXT, source_type VARCHAR(191) COLLATE utf8mb4_0900_bin,
        UNIQUE KEY knowledge_chunks_fts_chunk_id (chunk_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin""",
    """CREATE TABLE IF NOT EXISTS memory_entries_fts (
        rowid BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        memory_id VARCHAR(768) COLLATE utf8mb4_0900_bin NOT NULL,
        content LONGTEXT, memory_type VARCHAR(191) COLLATE utf8mb4_0900_bin,
        UNIQUE KEY memory_entries_fts_memory_id (memory_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin""",
    """CREATE TABLE IF NOT EXISTS conversation_history_fts (
        rowid BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        content LONGTEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin""",
)
_FULLTEXT_INDEXES = (
    ("mail_messages_fts", "ft_mail_messages_fts", ("subject", "sender", "folder", "body_text")),
    ("matters_fts", "ft_matters_fts", ("title", "summary", "tags")),
    ("knowledge_chunks_fts", "ft_knowledge_chunks_fts", ("title", "text", "source_type")),
    ("memory_entries_fts", "ft_memory_entries_fts", ("content", "memory_type")),
    ("conversation_history_fts", "ft_conversation_history_fts", ("content",)),
)
_SOURCE_TRIGGER_SQL = (
    (
        "memory_fts_insert", "memory_entries",
        """CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory_entries BEGIN
            INSERT INTO memory_entries_fts(memory_id,content,memory_type)
            VALUES(new.memory_id,new.content,new.memory_type);
        END""",
    ),
    (
        "memory_fts_update", "memory_entries",
        """CREATE TRIGGER memory_fts_update AFTER UPDATE OF content,memory_type ON memory_entries BEGIN
            DELETE FROM memory_entries_fts WHERE memory_id=old.memory_id;
            INSERT INTO memory_entries_fts(memory_id,content,memory_type)
            VALUES(new.memory_id,new.content,new.memory_type);
        END""",
    ),
    (
        "memory_fts_delete", "memory_entries",
        """CREATE TRIGGER memory_fts_delete AFTER DELETE ON memory_entries BEGIN
            DELETE FROM memory_entries_fts WHERE memory_id=old.memory_id;
        END""",
    ),
    (
        "conversation_history_source_insert", "agent_session_context_messages",
        """CREATE TRIGGER conversation_history_source_insert AFTER INSERT ON agent_session_context_messages BEGIN
            INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
            VALUES(NEW.session_id,NEW.seq);
            INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
            SELECT session_id,seq FROM agent_session_context_messages
            WHERE session_id=NEW.session_id AND trace_id=NEW.trace_id AND role='agent';
        END""",
    ),
    (
        "conversation_history_source_update", "agent_session_context_messages",
        """CREATE TRIGGER conversation_history_source_update AFTER UPDATE OF role,content,created_at,trace_id ON agent_session_context_messages BEGIN
            INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
            VALUES(OLD.session_id,OLD.seq);
            INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
            SELECT session_id,seq FROM agent_session_context_messages
            WHERE session_id=OLD.session_id AND trace_id=OLD.trace_id AND role='agent';
            INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
            SELECT session_id,seq FROM agent_session_context_messages
            WHERE session_id=NEW.session_id AND trace_id=NEW.trace_id AND role='agent';
        END""",
    ),
    (
        "conversation_history_source_delete", "agent_session_context_messages",
        """CREATE TRIGGER conversation_history_source_delete AFTER DELETE ON agent_session_context_messages BEGIN
            DELETE FROM conversation_history_dirty WHERE session_id=OLD.session_id AND seq=OLD.seq;
            DELETE FROM conversation_history_fts
            WHERE rowid=(SELECT rowid FROM conversation_history_fts_map
                         WHERE session_id=OLD.session_id AND seq=OLD.seq);
            DELETE FROM conversation_history_fts_map
            WHERE session_id=OLD.session_id AND seq=OLD.seq;
            INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
            SELECT session_id,seq FROM agent_session_context_messages
            WHERE session_id=OLD.session_id AND trace_id=OLD.trace_id AND role='agent';
        END""",
    ),
)

_FULLTEXT_V8_ASSET = Path(__file__).with_name("mysql_schema_v8.json")
_FULLTEXT_V9_ASSET = Path(__file__).with_name("mysql_schema_v9.json")
_FULLTEXT_V8_NAME = "memory_fulltext_in_place_update"
_FULLTEXT_V8_TRIGGER = "memory_fts_update"
_FULLTEXT_V8_NATIVE_SQL = """CREATE TRIGGER memory_fts_update AFTER UPDATE ON memory_entries FOR EACH ROW
BEGIN
    IF NOT (OLD.memory_id <=> NEW.memory_id) THEN
        DELETE FROM memory_entries_fts WHERE memory_id=OLD.memory_id;
        INSERT INTO memory_entries_fts(memory_id,content,memory_type)
        VALUES(NEW.memory_id,NEW.content,NEW.memory_type);
    ELSEIF NOT (OLD.content <=> NEW.content) OR NOT (OLD.memory_type <=> NEW.memory_type) THEN
        INSERT INTO memory_entries_fts(memory_id,content,memory_type)
        VALUES(NEW.memory_id,NEW.content,NEW.memory_type)
        ON DUPLICATE KEY UPDATE content=VALUES(content),memory_type=VALUES(memory_type);
    END IF;
END"""
_FULLTEXT_V8_SOURCE_SQL = next(
    source_sql for name, _table, source_sql in _SOURCE_TRIGGER_SQL
    if name == _FULLTEXT_V8_TRIGGER
)
_FULLTEXT_V9_NAME = "memory_fulltext_update_without_duplicate_upsert"
_FULLTEXT_V9_NATIVE_SQL = """CREATE TRIGGER memory_fts_update AFTER UPDATE ON memory_entries FOR EACH ROW
BEGIN
    IF NOT (OLD.memory_id <=> NEW.memory_id) THEN
        DELETE FROM memory_entries_fts WHERE memory_id=OLD.memory_id;
        IF EXISTS (SELECT 1 FROM memory_entries_fts WHERE memory_id=NEW.memory_id) THEN
            UPDATE memory_entries_fts
            SET content=NEW.content,memory_type=NEW.memory_type WHERE memory_id=NEW.memory_id;
        ELSE
            INSERT INTO memory_entries_fts(memory_id,content,memory_type)
            VALUES(NEW.memory_id,NEW.content,NEW.memory_type);
        END IF;
    ELSEIF NOT (OLD.content <=> NEW.content) OR NOT (OLD.memory_type <=> NEW.memory_type) THEN
        IF EXISTS (SELECT 1 FROM memory_entries_fts WHERE memory_id=OLD.memory_id) THEN
            UPDATE memory_entries_fts
            SET content=NEW.content,memory_type=NEW.memory_type WHERE memory_id=OLD.memory_id;
        ELSE
            INSERT INTO memory_entries_fts(memory_id,content,memory_type)
            VALUES(NEW.memory_id,NEW.content,NEW.memory_type);
        END IF;
    END IF;
END"""


def _load_fulltext_v8() -> tuple[dict[str, Any], bytes]:
    try:
        raw = _FULLTEXT_V8_ASSET.read_bytes()
        asset = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise MySQLFullTextError("full-text V8 migration asset is unavailable or invalid") from exc
    expected = {
        "version": 8,
        "migration_name": _FULLTEXT_V8_NAME,
        "trigger_name": _FULLTEXT_V8_TRIGGER,
        "table_name": "memory_entries",
        "source_sql": _FULLTEXT_V8_SOURCE_SQL,
        "mysql_sql": _FULLTEXT_V8_NATIVE_SQL,
    }
    if asset != expected:
        raise MySQLFullTextError("full-text V8 migration asset differs from reviewed source")
    return asset, raw


def _load_fulltext_v9() -> tuple[dict[str, Any], bytes]:
    try:
        raw = _FULLTEXT_V9_ASSET.read_bytes()
        asset = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise MySQLFullTextError("full-text V9 migration asset is unavailable or invalid") from exc
    expected = {
        "version": 9,
        "migration_name": _FULLTEXT_V9_NAME,
        "trigger_name": _FULLTEXT_V8_TRIGGER,
        "table_name": "memory_entries",
        "source_sql": _FULLTEXT_V8_SOURCE_SQL,
        "mysql_sql": _FULLTEXT_V9_NATIVE_SQL,
    }
    if asset != expected:
        raise MySQLFullTextError("full-text V9 migration asset differs from reviewed source")
    return asset, raw


def _verify_migration_record(conn: MySQLConnection, asset: dict[str, Any], raw: bytes) -> None:
    row = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s",
        (int(asset["version"]),),
    ).fetchone()
    expected_checksum = hashlib.sha256(raw).hexdigest()
    if (row is None or row[0] != asset["migration_name"] or row[1] != expected_checksum):
        raise MySQLFullTextError(
            f"full-text V{asset['version']} migration checksum/name mismatch or missing"
        )


def _verify_source_metadata(schema, conn: MySQLConnection) -> None:
    rows = schema.master_objects(conn, object_type="external_trigger", names=[_FULLTEXT_V8_TRIGGER])
    if len(rows) != 1 or rows[0][1] != _FULLTEXT_V8_SOURCE_SQL:
        raise MySQLFullTextError("full-text memory trigger source metadata differs")


def _verify_v9_trigger(conn: MySQLConnection) -> None:
    if not _trigger_body_matches(_fulltext_v8_trigger(conn), _FULLTEXT_V9_NATIVE_SQL,
                                 table="memory_entries"):
        raise MySQLFullTextError("installed full-text V9 memory update trigger differs")


def _trigger_body_matches(actual, ddl: str, *, table: str) -> bool:
    if actual is None:
        return False
    table_name, timing, event, action = actual
    expected = ddl.split(" FOR EACH ROW", 1)[1].strip()
    body = (action or "").strip()
    wrapped = re.fullmatch(r"BEGIN\s+(.*)\s+END", body, flags=re.IGNORECASE | re.DOTALL)
    if wrapped:
        body = wrapped.group(1).strip()
    expected_wrapped = re.fullmatch(r"BEGIN\s+(.*)\s+END", expected,
                                    flags=re.IGNORECASE | re.DOTALL)
    if expected_wrapped:
        expected = expected_wrapped.group(1).strip()
    normalize = lambda value: re.sub(r"\s+", " ", value.replace("`", "")).strip().casefold()
    return (
        str(table_name).casefold() == table.casefold()
        and str(timing).casefold() == "after"
        and str(event).casefold() == "update"
        and normalize(body) == normalize(expected)
    )


def _fulltext_v8_trigger(conn: MySQLConnection):
    return conn.execute_native(
        "SELECT EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT "
        "FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
        "AND TRIGGER_NAME=%s LIMIT 1", (_FULLTEXT_V8_TRIGGER,),
    ).fetchone()


def install_fulltext_v8(conn: MySQLConnection, *, install: bool = False) -> None:
    """Install or verify the narrowly scoped memory FTS update-trigger repair.

    The installer is called only by the privileged schema migration path. It
    replaces the exact reviewed V1 trigger and records an immutable checksum;
    runtime startup verifies metadata and trigger body without executing DDL.
    """
    if conn.in_transaction:
        raise MySQLFullTextError("full-text V8 migration cannot run inside a business transaction")
    schema = getattr(conn, "_schema", None)
    if schema is None:
        database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
        if not database:
            raise MySQLFullTextError("full-text V8 migration needs database identity")
        from app.storage.mysql_schema import MySQLSchema, attach_schema_metadata

        schema = MySQLSchema(database)
        if install:
            attach_schema_metadata(conn, schema)
        else:
            attach_schema_metadata(conn, schema, ensure_metadata=False)
    asset, raw = _load_fulltext_v8()
    checksum = hashlib.sha256(raw).hexdigest()
    version = int(asset["version"])
    migration_name = str(asset["migration_name"])
    current = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (version,),
    ).fetchone()
    trigger = _fulltext_v8_trigger(conn)

    if current is not None:
        if current[0] != migration_name or current[1] != checksum:
            raise MySQLFullTextError("full-text V8 migration checksum/name mismatch")
        v9, raw_v9 = _load_fulltext_v9()
        v9_row = conn.execute_native(
            "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (int(v9["version"]),),
        ).fetchone()
        if v9_row is not None:
            _verify_migration_record(conn, v9, raw_v9)
            _verify_v9_trigger(conn)
            _verify_source_metadata(schema, conn)
            return
        if not _trigger_body_matches(trigger, _FULLTEXT_V8_NATIVE_SQL, table="memory_entries"):
            raise MySQLFullTextError("installed full-text V8 memory update trigger differs")
        _verify_source_metadata(schema, conn)
        return

    if not install:
        raise MySQLFullTextError("full-text V8 is not installed; run scripts/migrate_mysql.py")

    lock_name = "lka-fulltext-v8-" + hashlib.sha256(schema.database.encode()).hexdigest()[:32]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLFullTextError("timed out acquiring full-text V8 migration lock")
    try:
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (version,),
        ).fetchone()
        if current is not None:
            if current[0] != migration_name or current[1] != checksum:
                raise MySQLFullTextError("full-text V8 migration checksum/name mismatch")
            if not _trigger_body_matches(_fulltext_v8_trigger(conn), _FULLTEXT_V8_NATIVE_SQL,
                                         table="memory_entries"):
                raise MySQLFullTextError("installed full-text V8 memory update trigger differs")
            return

        trigger = _fulltext_v8_trigger(conn)
        if not _trigger_body_matches(trigger, _FULLTEXT_V8_NATIVE_SQL, table="memory_entries"):
            legacy_ddl = dict(_trigger_ddl())[_FULLTEXT_V8_TRIGGER]
            if not _trigger_body_matches(trigger, legacy_ddl, table="memory_entries"):
                raise MySQLFullTextError("refusing to replace an unrecognized memory FTS trigger")
            conn.execute_native(f"DROP TRIGGER `{_FULLTEXT_V8_TRIGGER}`")
            conn.execute_native(_FULLTEXT_V8_NATIVE_SQL)

        if not _trigger_body_matches(_fulltext_v8_trigger(conn), _FULLTEXT_V8_NATIVE_SQL,
                                     table="memory_entries"):
            raise MySQLFullTextError("full-text V8 trigger installation did not verify")
        _verify_source_metadata(schema, conn)
        conn.execute_native(
            "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
            (version, migration_name, checksum),
        )
        conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()


def install_fulltext_v9(conn: MySQLConnection, *, install: bool = False) -> None:
    """Replace V8's duplicate-key upsert with indexed-row UPDATE semantics."""
    if conn.in_transaction:
        raise MySQLFullTextError("full-text V9 migration cannot run inside a business transaction")
    schema = getattr(conn, "_schema", None)
    if schema is None:
        database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
        if not database:
            raise MySQLFullTextError("full-text V9 migration needs database identity")
        from app.storage.mysql_schema import MySQLSchema, attach_schema_metadata

        schema = MySQLSchema(database)
        if install:
            attach_schema_metadata(conn, schema)
        else:
            attach_schema_metadata(conn, schema, ensure_metadata=False)

    v8, raw_v8 = _load_fulltext_v8()
    _verify_migration_record(conn, v8, raw_v8)
    asset, raw = _load_fulltext_v9()
    version = int(asset["version"])
    name = str(asset["migration_name"])
    checksum = hashlib.sha256(raw).hexdigest()
    current = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (version,),
    ).fetchone()
    if current is not None:
        if current[0] != name or current[1] != checksum:
            raise MySQLFullTextError("full-text V9 migration checksum/name mismatch")
        _verify_v9_trigger(conn)
        _verify_source_metadata(schema, conn)
        return
    if not install:
        raise MySQLFullTextError("full-text V9 is not installed; run scripts/migrate_mysql.py")

    lock_name = "lka-fulltext-v9-" + hashlib.sha256(schema.database.encode()).hexdigest()[:32]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLFullTextError("timed out acquiring full-text V9 migration lock")
    try:
        _verify_migration_record(conn, v8, raw_v8)
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (version,),
        ).fetchone()
        if current is not None:
            if current[0] != name or current[1] != checksum:
                raise MySQLFullTextError("full-text V9 migration checksum/name mismatch")
            _verify_v9_trigger(conn)
            _verify_source_metadata(schema, conn)
            return

        trigger = _fulltext_v8_trigger(conn)
        if not _trigger_body_matches(trigger, _FULLTEXT_V9_NATIVE_SQL, table="memory_entries"):
            if not _trigger_body_matches(trigger, _FULLTEXT_V8_NATIVE_SQL, table="memory_entries"):
                raise MySQLFullTextError("refusing to replace an unrecognized memory FTS trigger")
            conn.execute_native(f"DROP TRIGGER `{_FULLTEXT_V8_TRIGGER}`")
            conn.execute_native(_FULLTEXT_V9_NATIVE_SQL)

        _verify_v9_trigger(conn)
        _verify_source_metadata(schema, conn)
        conn.execute_native(
            "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
            (version, name, checksum),
        )
        conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,190}", value):
        raise MySQLFullTextError("invalid identifier in full-text metadata")
    return f"`{value}`"


def _fulltext_index_exists(conn: MySQLConnection, table: str, index: str) -> bool:
    row = conn.execute_native(
        "SELECT INDEX_TYPE FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME=%s AND INDEX_NAME=%s LIMIT 1", (table, index),
    ).fetchone()
    return row is not None and str(row[0]).upper() == "FULLTEXT"


def _trigger_exists(conn: MySQLConnection, name: str) -> bool:
    return conn.execute_native(
        "SELECT 1 FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
        "AND TRIGGER_NAME=%s LIMIT 1", (name,),
    ).fetchone() is not None


def _create_trigger(conn: MySQLConnection, name: str, ddl: str) -> None:
    if not _trigger_exists(conn, name):
        conn.execute_native(ddl)


def _trigger_ddl() -> tuple[tuple[str, str], ...]:
    return (
        (
            "memory_fts_insert",
            """CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory_entries FOR EACH ROW
            INSERT INTO memory_entries_fts(memory_id,content,memory_type)
            VALUES(NEW.memory_id,NEW.content,NEW.memory_type)
            ON DUPLICATE KEY UPDATE content=VALUES(content),memory_type=VALUES(memory_type)""",
        ),
        (
            "memory_fts_update",
            """CREATE TRIGGER memory_fts_update AFTER UPDATE ON memory_entries FOR EACH ROW
            BEGIN
                IF NOT (OLD.content <=> NEW.content) OR NOT (OLD.memory_type <=> NEW.memory_type) THEN
                    DELETE FROM memory_entries_fts WHERE memory_id=OLD.memory_id;
                    INSERT INTO memory_entries_fts(memory_id,content,memory_type)
                    VALUES(NEW.memory_id,NEW.content,NEW.memory_type)
                    ON DUPLICATE KEY UPDATE content=VALUES(content),memory_type=VALUES(memory_type);
                END IF;
            END""",
        ),
        (
            "memory_fts_delete",
            """CREATE TRIGGER memory_fts_delete AFTER DELETE ON memory_entries FOR EACH ROW
            DELETE FROM memory_entries_fts WHERE memory_id=OLD.memory_id""",
        ),
        (
            "conversation_history_source_insert",
            """CREATE TRIGGER conversation_history_source_insert AFTER INSERT ON agent_session_context_messages FOR EACH ROW
            BEGIN
                INSERT INTO conversation_history_dirty(session_id,seq) VALUES(NEW.session_id,NEW.seq)
                ON DUPLICATE KEY UPDATE seq=VALUES(seq);
                INSERT INTO conversation_history_dirty(session_id,seq)
                SELECT session_id,seq FROM agent_session_context_messages
                WHERE session_id=NEW.session_id AND trace_id=NEW.trace_id AND role='agent'
                ON DUPLICATE KEY UPDATE seq=VALUES(seq);
            END""",
        ),
        (
            "conversation_history_source_update",
            """CREATE TRIGGER conversation_history_source_update AFTER UPDATE ON agent_session_context_messages FOR EACH ROW
            BEGIN
                IF NOT (OLD.role <=> NEW.role) OR NOT (OLD.content <=> NEW.content)
                    OR NOT (OLD.created_at <=> NEW.created_at) OR NOT (OLD.trace_id <=> NEW.trace_id) THEN
                    INSERT INTO conversation_history_dirty(session_id,seq) VALUES(OLD.session_id,OLD.seq)
                    ON DUPLICATE KEY UPDATE seq=VALUES(seq);
                    INSERT INTO conversation_history_dirty(session_id,seq)
                    SELECT session_id,seq FROM agent_session_context_messages
                    WHERE session_id=OLD.session_id AND trace_id=OLD.trace_id AND role='agent'
                    ON DUPLICATE KEY UPDATE seq=VALUES(seq);
                    INSERT INTO conversation_history_dirty(session_id,seq)
                    SELECT session_id,seq FROM agent_session_context_messages
                    WHERE session_id=NEW.session_id AND trace_id=NEW.trace_id AND role='agent'
                    ON DUPLICATE KEY UPDATE seq=VALUES(seq);
                END IF;
            END""",
        ),
        (
            "conversation_history_source_delete",
            """CREATE TRIGGER conversation_history_source_delete AFTER DELETE ON agent_session_context_messages FOR EACH ROW
            BEGIN
                DELETE FROM conversation_history_dirty WHERE session_id=OLD.session_id AND seq=OLD.seq;
                DELETE f FROM conversation_history_fts f JOIN conversation_history_fts_map m ON m.rowid=f.rowid
                WHERE m.session_id=OLD.session_id AND m.seq=OLD.seq;
                DELETE FROM conversation_history_fts_map WHERE session_id=OLD.session_id AND seq=OLD.seq;
                INSERT INTO conversation_history_dirty(session_id,seq)
                SELECT session_id,seq FROM agent_session_context_messages
                WHERE session_id=OLD.session_id AND trace_id=OLD.trace_id AND role='agent'
                ON DUPLICATE KEY UPDATE seq=VALUES(seq);
            END""",
        ),
    )


def bootstrap_fulltext(
    conn: MySQLConnection,
    source_objects: Sequence[tuple[str, str, str | None]] | None = None,
    *, install: bool = True,
) -> None:
    """Install native FTS storage and transactionally maintained dirty/index rows.

    `source_objects` is accepted for bootstrap orchestration compatibility;
    catalog source metadata is owned by MySQLSchema and is already seeded by
    the schema bootstrap before this function runs.

    This operation is DDL-only. Existing application rows are not scanned or
    copied during schema installation; call `backfill_fulltext_indexes`
    explicitly after authorized data import when a rebuild is required.
    """
    if conn.in_transaction:
        raise MySQLFullTextError("native full-text bootstrap cannot run inside a business transaction")
    schema = getattr(conn, "_schema", None)
    if not install:
        verify_fulltext_schema(conn, source_objects=source_objects, schema=schema)
        return
    del source_objects
    if schema is None:
        database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
        if database:
            from app.storage.mysql_schema import MySQLSchema, attach_schema_metadata

            schema = MySQLSchema(database)
            attach_schema_metadata(conn, schema)
    for ddl in _FTS_DDL:
        conn.execute_native(ddl)
    for table, index, fields in _FULLTEXT_INDEXES:
        if not _fulltext_index_exists(conn, table, index):
            field_list = ",".join(_identifier(field) for field in fields)
            conn.execute_native(
                f"CREATE FULLTEXT INDEX {_identifier(index)} ON {_identifier(table)} "
                f"({field_list}) WITH PARSER ngram"
            )
    for name, ddl in _trigger_ddl():
        _create_trigger(conn, name, ddl)

    if schema is not None:
        for table in _FIELDS:
            schema.remember_rowid(conn, table, "rowid", proxy_kind="integer_primary_key")
        for name, table, source_sql in _SOURCE_TRIGGER_SQL:
            schema.remember_object(
                conn, object_type="external_trigger", object_name=name,
                table_name=table, source_sql=source_sql,
            )
        conn.commit()
        attach_fulltext_metadata(conn)



def verify_fulltext_schema(
    conn: MySQLConnection, *,
    source_objects: Sequence[tuple[str, str, str | None]] | None = None,
    schema=None,
) -> None:
    """Read-only validation after the privileged schema migration has run."""
    del source_objects
    if schema is None:
        schema = getattr(conn, "_schema", None)
    if schema is None:
        database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
        if not database:
            raise MySQLFullTextError("full-text schema verification needs database identity")
        from app.storage.mysql_schema import MySQLSchema

        schema = MySQLSchema(database)
    missing_tables = [table for table in _FIELDS if not schema.table_exists(conn, table)]
    missing_indexes = [
        index for table, index, _ in _FULLTEXT_INDEXES
        if not _fulltext_index_exists(conn, table, index)
    ]
    missing_triggers = [name for name, _, _ in _SOURCE_TRIGGER_SQL if not _trigger_exists(conn, name)]
    missing_rowids = [table for table in _FIELDS if schema.rowid_column(conn, table) != "rowid"]
    if missing_tables or missing_indexes or missing_triggers or missing_rowids:
        raise MySQLFullTextError(
            "privileged full-text schema migration is incomplete "
            f"(tables={missing_tables}, indexes={missing_indexes}, triggers={missing_triggers}, "
            f"rowids={missing_rowids})"
        )
    for name, _, source_sql in _SOURCE_TRIGGER_SQL:
        rows = schema.master_objects(conn, object_type="external_trigger", names=[name])
        if len(rows) != 1 or rows[0][1] is None:
            raise MySQLFullTextError(f"source metadata for full-text trigger {name!r} is missing")
        if schema._normalize_probe_sql(rows[0][1]) != schema._normalize_probe_sql(source_sql):
            raise MySQLFullTextError(f"source metadata for full-text trigger {name!r} mismatches")


def attach_fulltext_metadata(conn: MySQLConnection) -> None:
    """Attach FTS capabilities to a connection already carrying MySQLSchema."""
    if getattr(conn, "_schema", None) is None:
        raise MySQLFullTextError("attach MySQLSchema metadata before native full-text metadata")
    original_resolver = conn._rowid_column

    def resolve(table_name: str) -> str | None:
        if table_name.casefold() in _FIELDS:
            return "rowid"
        return original_resolver(table_name) if original_resolver else None

    conn.set_rowid_resolver(resolve)
    conn._fulltext_fields = _FIELDS


def backfill_fulltext_indexes(conn: MySQLConnection) -> None:
    """Explicitly rebuild derived full-text copies from existing base rows.

    This reads application data and is intentionally separate from schema
    bootstrap. Call only in a data migration/reindex operation with the
    appropriate authorization and bounded operational window.
    """
    if conn.in_transaction:
        raise MySQLFullTextError("full-text backfill must start outside an existing transaction")
    statements = (
        """INSERT INTO mail_messages_fts(message_id,subject,sender,folder,body_text)
        SELECT message_id,subject,sender,folder,body_text FROM mail_messages
        ON DUPLICATE KEY UPDATE subject=VALUES(subject),sender=VALUES(sender),
        folder=VALUES(folder),body_text=VALUES(body_text)""",
        """INSERT INTO knowledge_chunks_fts(chunk_id,document_id,title,text,source_type)
        SELECT c.chunk_id,c.document_id,d.title,c.text,d.source_type
        FROM knowledge_chunks c JOIN knowledge_documents d ON d.document_id=c.document_id
        ON DUPLICATE KEY UPDATE document_id=VALUES(document_id),title=VALUES(title),
        text=VALUES(text),source_type=VALUES(source_type)""",
        """INSERT INTO memory_entries_fts(memory_id,content,memory_type)
        SELECT memory_id,content,memory_type FROM memory_entries
        ON DUPLICATE KEY UPDATE content=VALUES(content),memory_type=VALUES(memory_type)""",
    )
    for statement in statements:
        conn.execute_native(statement)
        conn.commit()

    # Matter tags are JSON arrays but the FTS field stores joined values.
    rows = conn.execute_native(
        "SELECT matter_id,title,summary,tags FROM matters WHERE deleted_at IS NULL"
    ).fetchall()
    if rows:
        values = []
        for row in rows:
            try:
                tags = json.loads(row[3]) if row[3] else []
            except (TypeError, json.JSONDecodeError):
                tags = []
            if not isinstance(tags, list):
                tags = []
            values.append((row[0], row[1], row[2], " ".join(str(tag) for tag in tags)))
        conn.executemany(
            "INSERT INTO matters_fts(matter_id,title,summary,tags) VALUES(?,?,?,?) "
            "ON DUPLICATE KEY UPDATE title=VALUES(title),summary=VALUES(summary),tags=VALUES(tags)",
            values,
        )
        conn.commit()


def _named_qmarks(statement: str, parameters: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    out: list[str] = []
    names: dict[str, Any] = {}
    quote: str | None = None
    i = 0
    while i < len(statement):
        char = statement[i]
        if quote is not None:
            out.append(char)
            if char == "\\" and quote != "`" and i + 1 < len(statement):
                i += 1
                out.append(statement[i])
            elif char == quote:
                if i + 1 < len(statement) and statement[i + 1] == quote:
                    i += 1
                    out.append(statement[i])
                else:
                    quote = None
            i += 1
            continue
        if char in "'\"`":
            quote = char
            out.append(char)
            i += 1
            continue
        if char == "-" and i + 1 < len(statement) and statement[i + 1] == "-":
            end = statement.find("\n", i)
            if end < 0:
                out.append(statement[i:])
                break
            out.append(statement[i:end + 1])
            i = end + 1
            continue
        if char == "/" and i + 1 < len(statement) and statement[i + 1] == "*":
            end = statement.find("*/", i + 2)
            if end < 0:
                raise MySQLFullTextError("unterminated SQL comment")
            out.append(statement[i:end + 2])
            i = end + 2
            continue
        if char == "?":
            name = f"lka_param_{len(names)}"
            if len(names) >= len(parameters):
                raise MySQLFullTextError("full-text query has fewer parameters than placeholders")
            names[name] = parameters[len(names)]
            out.append(f":{name}")
        else:
            out.append(char)
        i += 1
    if len(names) != len(parameters):
        raise MySQLFullTextError("full-text query parameter count does not match placeholders")
    return "".join(out), names


def _placeholder_name(node: Any) -> str:
    from sqlglot import exp

    if not isinstance(node, exp.Placeholder) or not node.this:
        raise MySQLFullTextError("full-text MATCH requires a bound query parameter")
    return str(node.this)


def _boolean_query(value: Any) -> str:
    """Translate the restricted FTS5 phrase syntax emitted by this app.

    Search domains emit double-quoted literal phrases joined either by FTS5
    ``OR`` (mail/knowledge/matters) or implicit AND (memory). Reject all other
    syntax instead of passing user text through as MySQL Boolean operators.
    """
    if not isinstance(value, str):
        raise MySQLFullTextError("full-text MATCH value must be text")
    phrases = list(re.finditer(r'"(?:""|[^"])*"', value))
    if not phrases or value[:phrases[0].start()].strip() or value[phrases[-1].end():].strip():
        return ""
    separators = [
        value[left.end():right.start()]
        for left, right in pairwise(phrases)
    ]
    modes = []
    for separator in separators:
        if re.fullmatch(r"\s+OR\s+", separator, re.IGNORECASE):
            modes.append("or")
        elif separator and separator.isspace():
            modes.append("and")
        else:
            return ""
    if modes and len(set(modes)) != 1:
        return ""
    decoded = [phrase.group(0)[1:-1].replace('""', '"') for phrase in phrases]
    if any('"' in phrase for phrase in decoded):
        # The two engines escape a quote inside a phrase differently. No
        # results are safer than widening the search on malformed syntax.
        return ""
    if modes == ["and"] * len(modes):
        return " ".join(f'+"{phrase}"' for phrase in decoded)
    return " ".join(f'"{phrase}"' for phrase in decoded)


def _literal_memory_backstop(exp, *, boolean_query: str, alias: str, binds: dict[str, Any]):
    """Build a literal-memory fallback matching the adapter's restricted query grammar.

    It is deliberately limited to memory FTS: memory is small, MemoryService
    already has a scoped content-LIKE fallback, and MySQL can occasionally
    return zero relevance for an otherwise-present ngram document. The caller
    keeps the expression inside the original SELECT so all joins and filters
    remain in force.
    """
    phrases = list(re.finditer(r'(\+?)"((?:""|[^"])*)"', boolean_query))
    if not phrases or any(not item.group(2) for item in phrases):
        return None
    separators = [
        boolean_query[left.end():right.start()]
        for left, right in pairwise(phrases)
    ]
    if any(not separator.isspace() for separator in separators):
        return None
    operators = [bool(item.group(1)) for item in phrases]
    if len(set(operators)) > 1:
        return None

    phrase_predicates = []
    for index, item in enumerate(phrases):
        phrase = item.group(2).replace('""', '"')
        if '"' in phrase:
            return None
        bind_name = f"lka_backstop_{len(binds)}_{index}"
        binds[bind_name] = phrase
        column_predicates = [
            exp.GT(
                this=exp.Anonymous(
                    this="INSTR",
                    expressions=[
                        exp.Column(this=exp.to_identifier(field), table=exp.to_identifier(alias)),
                        exp.Placeholder(this=bind_name),
                    ],
                ),
                expression=exp.Literal.number(0),
            )
            for field in _FIELDS["memory_entries_fts"]
        ]
        predicate = column_predicates[0]
        for item_predicate in column_predicates[1:]:
            predicate = exp.Or(this=predicate, expression=item_predicate)
        phrase_predicates.append(exp.Paren(this=predicate))

    predicate = phrase_predicates[0]
    combine = exp.And if operators and operators[0] else exp.Or
    for phrase_predicate in phrase_predicates[1:]:
        predicate = combine(this=predicate, expression=phrase_predicate)
    return exp.Paren(this=predicate)


def _literal_conversation_backstop(exp, *, boolean_query: str, alias: str, binds: dict[str, Any]):
    """Build a bounded-query phrase fallback for a single conversation scope."""
    phrases = list(re.finditer(r'(\+?)"((?:""|[^"])*)"', boolean_query))
    if not phrases or any(not item.group(2) for item in phrases):
        return None
    separators = [boolean_query[left.end():right.start()] for left, right in pairwise(phrases)]
    if any(not separator.isspace() for separator in separators):
        return None
    operators = [bool(item.group(1)) for item in phrases]
    if len(set(operators)) > 1:
        return None
    predicates = []
    for index, item in enumerate(phrases):
        phrase = item.group(2).replace('""', '"')
        if '"' in phrase:
            return None
        bind_name = f"lka_backstop_{len(binds)}_{index}"
        binds[bind_name] = phrase
        content = exp.Column(this=exp.to_identifier("content"), table=exp.to_identifier(alias))
        needle = exp.Placeholder(this=bind_name)
        predicates.append(exp.Paren(this=exp.GT(
            this=exp.Anonymous(this="INSTR", expressions=[
                exp.Lower(this=content), exp.Lower(this=needle),
            ]),
            expression=exp.Literal.number(0),
        )))
    predicate = predicates[0]
    combine = exp.And if operators and operators[0] else exp.Or
    for next_predicate in predicates[1:]:
        predicate = combine(this=predicate, expression=next_predicate)
    return exp.Paren(this=predicate)


def _conversation_candidate_phrase(boolean_query: str) -> str | None:
    """Return one safe unquoted candidate phrase; exactness stays in INSTR."""
    phrases = list(re.finditer(r'\+?"((?:""|[^"])*)"', boolean_query))
    if len(phrases) != 1:
        return None
    phrase = phrases[0].group(1).replace('""', '"')
    if not phrase or '"' in phrase or not all(char.isalnum() or char.isspace() for char in phrase):
        return None
    return phrase


def _conversation_offset_is_zero(tree: Any, binds: dict[str, Any]) -> bool:
    from sqlglot import exp

    offset = tree.args.get("offset")
    if offset is None:
        return True
    value = offset.args.get("expression")
    if isinstance(value, exp.Placeholder):
        result = binds.get(value.this)
    elif isinstance(value, exp.Literal) and not value.is_string:
        result = value.this
    else:
        return False
    try:
        return int(result) == 0
    except (TypeError, ValueError):
        return False


def _indexed_candidate_query(
    tree: Any, binds: dict[str, Any], *, placeholder_name: str, alias: str,
    fields: Sequence[str], score: Any,
) -> tuple[Any, dict[str, Any]] | None:
    """Add a native token candidate and exact literal filter to translated SQL."""
    from sqlglot import exp

    candidate = _conversation_candidate_phrase(str(binds[placeholder_name]))
    if candidate is None:
        return None
    candidate_tree = tree.copy()
    where = candidate_tree.args.get("where")
    if where is None:
        return None
    matches = [
        node for node in where.find_all(exp.MatchAgainst)
        if isinstance(node.this, exp.Placeholder) and str(node.this.this) == placeholder_name
    ]
    if len(matches) != 1:
        return None
    match = matches[0]
    from_clause = candidate_tree.args.get("from_")
    base_table = from_clause.this if from_clause is not None else None
    if not isinstance(base_table, exp.Table) or (base_table.alias or base_table.name).casefold() != alias.casefold():
        return None
    for join in candidate_tree.args.get("joins") or ():
        join_kind = str(join.args.get("kind") or "").upper()
        if join.args.get("side") or join_kind not in {"", "INNER"}:
            return None
        if isinstance(join.this, exp.Table):
            # The source FTS table is the FROM item; keep its native index as
            # the driving access path before exact substring filtering.
            join.set("method", "STRAIGHT_JOIN")
    candidate_binds = dict(binds)
    query_bind = f"lka_candidate_query_{len(candidate_binds)}"
    candidate_binds[query_bind] = candidate
    phrase_bind = f"lka_candidate_exact_{len(candidate_binds)}"
    candidate_binds[phrase_bind] = candidate
    field_predicates = [
        exp.GT(
            this=exp.Anonymous(
                this="INSTR",
                expressions=[
                    exp.Lower(this=exp.Column(this=exp.to_identifier(field), table=exp.to_identifier(alias))),
                    exp.Lower(this=exp.Placeholder(this=phrase_bind)),
                ],
            ),
            expression=exp.Literal.number(0),
        )
        for field in fields
    ]
    literal = field_predicates[0]
    for field_predicate in field_predicates[1:]:
        literal = exp.Or(this=literal, expression=field_predicate)
    candidate_score = score.copy()
    candidate_score.set("this", exp.Placeholder(this=query_bind))
    match.replace(exp.Paren(this=exp.And(this=candidate_score, expression=exp.Paren(this=literal))))
    return candidate_tree, candidate_binds


def _top_level_and_terms(expression: Any) -> list[Any]:
    from sqlglot import exp

    if isinstance(expression, exp.And):
        return _top_level_and_terms(expression.this) + _top_level_and_terms(expression.expression)
    return [expression]


def _conversation_session_scope(tree: Any, binds: dict[str, Any]) -> tuple[str, str] | None:
    """Return the map alias and bound session only for the reviewed join shape."""
    from sqlglot import exp

    try:
        fts_alias = _resolve_table_alias(tree, "conversation_history_fts")
        map_alias = _resolve_table_alias(tree, "conversation_history_fts_map")
    except MySQLFullTextError:
        return None
    map_joins = [
        join for join in tree.find_all(exp.Join)
        if isinstance(join.this, exp.Table)
        and join.this.name.casefold() == "conversation_history_fts_map"
    ]
    if len(map_joins) != 1:
        return None
    rowid_join = False
    for term in _top_level_and_terms(map_joins[0].args.get("on")):
        if not isinstance(term, exp.EQ):
            continue
        left, right = term.this, term.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        pair = {(left.table.casefold(), left.name.casefold()), (right.table.casefold(), right.name.casefold())}
        if pair == {(fts_alias.casefold(), "rowid"), (map_alias.casefold(), "rowid")}:
            rowid_join = True
            break
    if not rowid_join:
        return None
    where = tree.args.get("where")
    if where is None:
        return None
    scopes = []
    for term in _top_level_and_terms(where.this):
        if not isinstance(term, exp.EQ):
            continue
        left, right = term.this, term.expression
        if isinstance(left, exp.Column) and left.table.casefold() == map_alias.casefold() \
                and left.name.casefold() == "session_id":
            value = right
        elif isinstance(right, exp.Column) and right.table.casefold() == map_alias.casefold() \
                and right.name.casefold() == "session_id":
            value = left
        else:
            continue
        if isinstance(value, exp.Placeholder) and value.this in binds:
            session_id = binds[value.this]
            if isinstance(session_id, str) and session_id:
                scopes.append(session_id)
    if len(scopes) != 1 or tree.args.get("limit") is None:
        return None
    return map_alias, scopes[0]


def _resolve_table_alias(select: Any, table_name: str) -> str:
    from sqlglot import exp

    candidates = [
        table for table in select.find_all(exp.Table)
        if table.name.casefold() == table_name.casefold()
        and table.find_ancestor(exp.Select) is select
    ]
    if len(candidates) != 1:
        raise MySQLFullTextError(f"full-text source table {table_name!r} is missing or ambiguous")
    return candidates[0].alias or candidates[0].name


def _bounded_snippet(
    exp, column, *, phrase: str | None, start_marker: str, end_marker: str,
    ellipsis: str, max_chars: int, bind_name: str,
):
    fallback = exp.Anonymous(
        this="LEFT", expressions=[column.copy(), exp.Literal.number(max_chars)],
    )
    if not phrase:
        return fallback
    term = exp.Placeholder(this=bind_name)

    def locate():
        return exp.Anonymous(
            this="LOCATE",
            expressions=[exp.Lower(this=term.copy()), exp.Lower(this=column.copy())],
        )

    position = locate()
    start = exp.Greatest(
        this=exp.Literal.number(1),
        expressions=[exp.Sub(this=position.copy(), expression=exp.Literal.number(max_chars // 2))],
    )
    prefix_length = exp.Sub(this=position.copy(), expression=start.copy())
    term_length = exp.Anonymous(this="CHAR_LENGTH", expressions=[term.copy()])
    remaining = exp.Greatest(
        this=exp.Literal.number(0),
        expressions=[exp.Sub(
            this=exp.Sub(
                this=exp.Literal.number(max_chars), expression=prefix_length.copy(),
            ), expression=term_length.copy(),
        )],
    )
    end_position = exp.Add(this=position.copy(), expression=term_length.copy())
    content = exp.Anonymous(this="CONCAT", expressions=[
        exp.Anonymous(this="IF", expressions=[
            exp.GT(this=start.copy(), expression=exp.Literal.number(1)),
            exp.Literal.string(ellipsis), exp.Literal.string(""),
        ]),
        exp.Anonymous(this="SUBSTRING", expressions=[column.copy(), start.copy(), prefix_length.copy()]),
        exp.Literal.string(start_marker),
        exp.Anonymous(this="SUBSTRING", expressions=[column.copy(), position.copy(), term_length.copy()]),
        exp.Literal.string(end_marker),
        exp.Anonymous(this="SUBSTRING", expressions=[column.copy(), end_position, remaining]),
        exp.Anonymous(this="IF", expressions=[
            exp.GT(
                this=exp.Add(this=start.copy(), expression=exp.Literal.number(max_chars)),
                expression=exp.Anonymous(this="CHAR_LENGTH", expressions=[column.copy()]),
            ),
            exp.Literal.string(""), exp.Literal.string(ellipsis),
        ]),
    ])
    return exp.Case(
        ifs=[exp.If(
            this=exp.GT(this=position.copy(), expression=exp.Literal.number(0)),
            true=content,
        )],
        default=fallback,
    )


def execute_fulltext(
    conn: MySQLConnection, statement: str, parameters: Sequence[Any] = (),
):
    """Execute one existing SQLite FTS query through native InnoDB FULLTEXT.

    SQLite connections execute the original query unchanged. For MySQL,
    returns ``None`` if there is no FTS MATCH expression for this adapter.
    """
    if getattr(conn, "dialect", None) != "mysql":
        return conn.execute(statement, parameters)
    if not re.search(r"\bMATCH\b", statement, re.IGNORECASE):
        return None
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError as exc:  # pragma: no cover - optional dependency boundary
        raise MySQLFullTextError("native full-text adapter requires sqlglot") from exc
    named_sql, binds = _named_qmarks(statement, parameters)
    try:
        tree = sqlglot.parse_one(named_sql, read="sqlite")
    except Exception as exc:
        raise MySQLFullTextError("SQLite full-text query is outside the reviewed adapter grammar") from exc
    if not isinstance(tree, exp.Select):
        raise MySQLFullTextError("full-text adapter supports SELECT statements only")

    matches = list(tree.find_all(exp.Match))
    if not matches:
        return None
    scores = {}
    first_phrases = {}
    for match in matches:
        source = match.this
        table_name = source.name.casefold() if isinstance(source, exp.Column) else ""
        fields = _FIELDS.get(table_name)
        if not fields:
            raise MySQLFullTextError(f"unsupported FTS virtual table {table_name!r}")
        query_node = match.expression
        placeholder_name = _placeholder_name(query_node)
        source_query = binds[placeholder_name]
        boolean_query = _boolean_query(source_query)
        first_phrase = re.search(r'\+?"([^"]*)"', boolean_query)
        first_phrases[placeholder_name] = first_phrase.group(1) if first_phrase else None
        binds[placeholder_name] = boolean_query
        alias = _resolve_table_alias(tree, table_name)
        score = exp.MatchAgainst(
            this=query_node.copy(),
            expressions=[exp.Column(this=exp.to_identifier(name), table=exp.to_identifier(alias))
                         for name in fields],
            modifier=exp.Var(this="IN BOOLEAN MODE"),
        )
        scores[table_name] = (score, placeholder_name, alias)
        if table_name == "memory_entries_fts" and boolean_query:
            backstop = _literal_memory_backstop(
                exp, boolean_query=boolean_query, alias=alias, binds=binds,
            )
            match.replace(
                exp.Paren(this=exp.Or(this=score.copy(), expression=backstop))
                if backstop is not None else score.copy()
            )
        else:
            match.replace(score.copy())

    for column in list(tree.find_all(exp.Column)):
        if column.name.casefold() != "rank" or column.table:
            continue
        parent = column.parent
        if not isinstance(parent, exp.Ordered):
            continue
        if len(scores) != 1:
            raise MySQLFullTextError("rank ordering with multiple full-text sources is unsupported")
        score = next(iter(scores.values()))[0]
        column.replace(score.copy())
        parent.set("desc", True)

    for snippet in list(tree.find_all(exp.Anonymous)):
        if snippet.name.casefold() != "snippet":
            continue
        args = snippet.expressions
        if len(args) != 6 or not isinstance(args[0], exp.Column):
            raise MySQLFullTextError("unsupported SQLite snippet() shape")
        table_name = args[0].name.casefold()
        snippet_fields = _SNIPPET_FIELDS.get(table_name)
        if snippet_fields is None or not isinstance(args[1], exp.Literal):
            raise MySQLFullTextError("snippet() source table or column index is unsupported")
        try:
            index = int(args[1].this)
        except (TypeError, ValueError) as exc:
            raise MySQLFullTextError("snippet() column index must be a literal integer") from exc
        if not 0 <= index < len(snippet_fields):
            raise MySQLFullTextError("snippet() column index is out of range")
        alias = _resolve_table_alias(tree, table_name)
        column = exp.Column(this=exp.to_identifier(snippet_fields[index]), table=exp.to_identifier(alias))
        token_count = 12
        if isinstance(args[5], exp.Literal) and not args[5].is_string:
            token_count = max(1, min(20, int(args[5].this)))
        match_entry = scores.get(table_name)
        phrase = first_phrases.get(match_entry[1]) if match_entry else None
        snippet_name = f"lka_snippet_{len(binds)}"
        if phrase:
            binds[snippet_name] = phrase
        markers = args[2:5]
        if not all(isinstance(item, exp.Literal) and item.is_string for item in markers):
            raise MySQLFullTextError("snippet() markers must be literal strings")
        snippet.replace(_bounded_snippet(
            exp, column, phrase=phrase,
            start_marker=markers[0].this, end_marker=markers[1].this,
            ellipsis=markers[2].this, max_chars=min(1600, token_count * 80),
            bind_name=snippet_name,
        ))

    def render(select, parameters):
        sql = select.sql(dialect="mysql")
        return re.sub(
            r":(lka_(?:param_\d+|snippet_\d+|backstop_\d+_\d+|candidate_query_\d+|candidate_exact_\d+))\b",
            r"%(\1)s", sql,
        ), parameters

    rendered, rendered_binds = render(tree, binds)
    cursor = conn.execute_native(rendered, rendered_binds)
    if getattr(cursor, "rowcount", None) != 0 or len(matches) != 1:
        return cursor
    match = matches[0]
    source = match.this
    if not isinstance(source, exp.Column):
        return cursor
    placeholder_name = _placeholder_name(match.expression)
    table_name = source.name.casefold()
    if table_name not in {
        "conversation_history_fts", "mail_messages_fts", "matters_fts", "knowledge_chunks_fts",
    }:
        return cursor
    scope = None
    if table_name == "conversation_history_fts":
        scope = _conversation_session_scope(tree, binds)
        if scope is None:
            return cursor
    score, _score_placeholder, score_alias = scores[table_name]
    offset_is_zero = _conversation_offset_is_zero(tree, binds)
    if not offset_is_zero:
        supports_literal = _conversation_candidate_phrase(str(binds[placeholder_name])) is not None
        if table_name == "conversation_history_fts":
            probe_binds = dict(binds)
            supports_literal = supports_literal or _literal_conversation_backstop(
                exp, boolean_query=str(binds[placeholder_name]), alias=score_alias, binds=probe_binds,
            ) is not None
        if not supports_literal:
            return cursor
        probe_tree = tree.copy()
        probe_tree.set("limit", exp.Limit(expression=exp.Literal.number(1)))
        probe_tree.set("offset", exp.Offset(expression=exp.Literal.number(0)))
        probe_sql, probe_params = render(probe_tree, binds)
        probe_cursor = conn.execute_native(probe_sql, probe_params)
        probe_rowcount = getattr(probe_cursor, "rowcount", None)
        probe_cursor.close()
        if probe_rowcount != 0:
            return cursor

    candidate_query = _indexed_candidate_query(
        tree, binds, placeholder_name=placeholder_name,
        alias=score_alias, fields=_FIELDS[table_name], score=score,
    )
    if candidate_query is not None:
        candidate_tree, candidate_binds = candidate_query
        candidate_sql, candidate_params = render(candidate_tree, candidate_binds)
        cursor.close()
        candidate_cursor = conn.execute_native(candidate_sql, candidate_params)
        if getattr(candidate_cursor, "rowcount", None) != 0:
            return candidate_cursor
        if table_name != "conversation_history_fts":
            return candidate_cursor
        candidate_cursor.close()

    if table_name != "conversation_history_fts":
        return cursor

    assert scope is not None
    _map_alias, session_id = scope
    fallback_alias = _resolve_table_alias(tree, table_name)

    fallback_tree = tree.copy()
    fallback_binds = dict(binds)
    fallback_where = fallback_tree.args.get("where")
    if fallback_where is None:
        return cursor
    fallback_matches = [
        node for node in fallback_where.find_all(exp.MatchAgainst)
        if isinstance(node.this, exp.Placeholder) and str(node.this.this) == placeholder_name
    ]
    if len(fallback_matches) != 1:
        return cursor
    fallback_match = fallback_matches[0]
    fallback = _literal_conversation_backstop(
        exp, boolean_query=str(binds[placeholder_name]), alias=fallback_alias, binds=fallback_binds,
    )
    if fallback is None:
        return cursor
    fallback_match.replace(fallback)

    cursor.close()
    scoped_rows = conn.execute_native(
        "SELECT seq FROM conversation_history_fts_map WHERE session_id=%s "
        "ORDER BY seq DESC LIMIT %s",
        (session_id, 5001),
    ).fetchall()
    if len(scoped_rows) > 5000:
        raise MySQLFullTextError(
            "conversation literal full-text fallback exceeded the 5000-row session bound"
        )
    fallback_sql, fallback_params = render(fallback_tree, fallback_binds)
    return conn.execute_native(fallback_sql, fallback_params)
