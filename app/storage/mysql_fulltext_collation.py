"""Versioned, fail-closed case-insensitive collation for native FTS text."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.storage.mysql import MySQLConnection
from app.storage.mysql_schema import MySQLSchemaError

_VERSION = 12
_NAME = "native_fulltext_case_insensitive_collation"
_TARGET_COLLATION = "utf8mb4_0900_as_ci"
_SOURCE_COLLATION = "utf8mb4_0900_bin"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,190}$")
_TABLE_COLUMNS = {
    "mail_messages_fts": (
        ("rowid", "bigint unsigned", False, "auto_increment", None, None, "PRI"),
        ("message_id", "varchar(768)", False, "", "utf8mb4", _SOURCE_COLLATION, "UNI"),
        ("subject", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, "MUL"),
        ("sender", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, ""),
        ("folder", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, ""),
        ("body_text", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, ""),
    ),
    "matters_fts": (
        ("rowid", "bigint unsigned", False, "auto_increment", None, None, "PRI"),
        ("matter_id", "varchar(768)", False, "", "utf8mb4", _SOURCE_COLLATION, "UNI"),
        ("title", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, "MUL"),
        ("summary", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, ""),
        ("tags", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, ""),
    ),
    "knowledge_chunks_fts": (
        ("rowid", "bigint unsigned", False, "auto_increment", None, None, "PRI"),
        ("chunk_id", "varchar(768)", False, "", "utf8mb4", _SOURCE_COLLATION, "UNI"),
        ("document_id", "varchar(768)", False, "", "utf8mb4", _SOURCE_COLLATION, ""),
        ("title", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, "MUL"),
        ("text", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, ""),
        ("source_type", "varchar(191)", True, "", "utf8mb4", _SOURCE_COLLATION, ""),
    ),
    "memory_entries_fts": (
        ("rowid", "bigint unsigned", False, "auto_increment", None, None, "PRI"),
        ("memory_id", "varchar(768)", False, "", "utf8mb4", _SOURCE_COLLATION, "UNI"),
        ("content", "longtext", True, "", "utf8mb4", _SOURCE_COLLATION, "MUL"),
        ("memory_type", "varchar(191)", True, "", "utf8mb4", _SOURCE_COLLATION, ""),
    ),
    "conversation_history_fts": (
        ("rowid", "bigint unsigned", False, "auto_increment", None, None, "PRI"),
        ("content", "longtext", False, "", "utf8mb4", _SOURCE_COLLATION, "MUL"),
    ),
}


def _definitions() -> dict[str, dict[str, Any]]:
    from app.storage.mysql_fulltext import _FULLTEXT_INDEXES

    expected_types = {
        "subject": ("longtext", True), "sender": ("longtext", True),
        "folder": ("longtext", True), "body_text": ("longtext", True),
        "title": ("longtext", True), "summary": ("longtext", True),
        "tags": ("longtext", True), "text": ("longtext", True),
        "source_type": ("varchar(191)", True), "content": ("longtext", None),
        "memory_type": ("varchar(191)", True),
    }
    result = {}
    for table, index, columns in _FULLTEXT_INDEXES:
        if not _IDENTIFIER.fullmatch(table) or not _IDENTIFIER.fullmatch(index):
            raise MySQLSchemaError("unrecognized native full-text index identity")
        expected = []
        for column in columns:
            if not _IDENTIFIER.fullmatch(column) or column not in expected_types:
                raise MySQLSchemaError("unrecognized native full-text indexed column")
            column_type, nullable = expected_types[column]
            # conversation_history_fts.content is the one non-null source column.
            if table == "conversation_history_fts" and column == "content":
                nullable = False
            expected.append((column, column_type, nullable))
        result[table] = {"index": index, "columns": tuple(expected)}
    if len(result) != len(_FULLTEXT_INDEXES):
        raise MySQLSchemaError("duplicate native full-text table configuration")
    return result


def _checksum() -> str:
    payload = {"version": _VERSION, "name": _NAME, "collation": _TARGET_COLLATION,
               "definitions": _definitions(), "table_columns": _TABLE_COLUMNS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _verify_v11(conn: MySQLConnection) -> None:
    from app.storage.mysql_knowledge_vectors import verify_knowledge_vector_blobs_v11

    verify_knowledge_vector_blobs_v11(conn)


def _column_rows(conn: MySQLConnection, table: str) -> list[tuple[Any, ...]]:
    return conn.execute_native(
        "SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA,ORDINAL_POSITION,"
        "CHARACTER_SET_NAME,COLLATION_NAME,GENERATION_EXPRESSION,COLUMN_COMMENT,COLUMN_KEY "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
        "ORDER BY ORDINAL_POSITION",
        (table,),
    ).fetchall()


def _index_rows(conn: MySQLConnection, table: str) -> list[tuple[Any, ...]]:
    return conn.execute_native(
        "SELECT INDEX_NAME,SEQ_IN_INDEX,COLUMN_NAME,INDEX_TYPE "
        "FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
        "AND INDEX_TYPE='FULLTEXT' ORDER BY INDEX_NAME,SEQ_IN_INDEX",
        (table,),
    ).fetchall()


def _verify_fulltext_index_ddl(conn: MySQLConnection, table: str, index: str,
                               columns: tuple[str, ...]) -> None:
    row = conn.execute_native(f"SHOW CREATE TABLE `{table}`").fetchone()
    ddl = str(row[1]) if row is not None and len(row) > 1 else ""
    ddl = re.sub(r"/\*!\d+\s*(.*?)\*/", r"\1", ddl, flags=re.DOTALL)
    match = re.search(
        rf"FULLTEXT\s+(?:KEY|INDEX)\s+`?{re.escape(index)}`?\s*\(([^)]*)\)"
        r"\s*WITH\s+PARSER\s+`?([A-Za-z0-9_$]+)`?",
        ddl,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise MySQLSchemaError(f"native FULLTEXT index parser differs for {table}")
    found_columns = tuple(re.findall(r"`([^`]+)`", match.group(1)))
    if found_columns != columns or match.group(2).lower() != "ngram":
        raise MySQLSchemaError(f"native FULLTEXT index definition differs for {table}")


def _inspect_table(conn: MySQLConnection, table: str, definition: dict[str, Any], *,
                   allowed_collations: set[str], allow_missing_index: bool = False
                   ) -> dict[str, tuple[str, str]]:
    rows = _column_rows(conn, table)
    by_name = {str(row[0]): row for row in rows}
    expected_layout = _TABLE_COLUMNS.get(table)
    if expected_layout is None or tuple(by_name) != tuple(item[0] for item in expected_layout):
        raise MySQLSchemaError(f"unrecognized native full-text table columns: {table}")
    indexed_columns = {item[0] for item in definition["columns"]}
    if not indexed_columns.issubset(by_name):
        raise MySQLSchemaError(f"native full-text table {table} is missing indexed columns")
    desired_index = definition["index"]
    grouped: dict[str, list[tuple[int, str, str]]] = {}
    for index_name, seq, column, index_type in _index_rows(conn, table):
        grouped.setdefault(str(index_name), []).append(
            (int(seq), str(column), str(index_type).upper()),
        )
    for index_name, entries in grouped.items():
        expected = (desired_index, [(i, col, "FULLTEXT")
                                    for i, (col, _type, _nullable) in enumerate(definition["columns"], 1)])
        if index_name != desired_index or (index_name, entries) != expected:
            raise MySQLSchemaError(f"unrecognized FULLTEXT index on {table}")
    if not grouped and not allow_missing_index:
        raise MySQLSchemaError(f"native FULLTEXT index is missing for {table}")
    if grouped:
        _verify_fulltext_index_ddl(
            conn, table, desired_index,
            tuple(column for column, _type, _nullable in definition["columns"]),
        )
    modifications = {}
    for ordinal, (column, expected_type, expected_nullable, expected_extra,
                  expected_charset, expected_collation, expected_key) in enumerate(expected_layout, 1):
        row = by_name[column]
        name, column_type, nullable, default, extra, actual_ordinal, charset, collation, generated, comment, key = row
        actual_nullable = str(nullable).upper() == "YES"
        expected_nullable_str = "YES" if expected_nullable else "NO"
        target_column = column in indexed_columns
        allowed_collation = allowed_collations if target_column else {expected_collation}
        if column == definition["columns"][0][0]:
            expected_key = "MUL" if grouped else ""
        if (str(name) != column or str(column_type).lower() != expected_type
                or str(nullable).upper() != expected_nullable_str or int(actual_ordinal) != ordinal
                or default is not None or str(extra or "").lower() != expected_extra
                or (str(charset).lower() if charset is not None else None) != expected_charset
                or (str(collation).lower() if collation is not None else None) not in allowed_collation
                or str(generated or "") or str(comment or "")
                or str(key or "").upper() != expected_key):
            raise MySQLSchemaError(f"unrecognized native full-text column definition: {table}.{column}")
        if target_column:
            modifications[column] = (str(column_type), "YES" if actual_nullable else "NO")

    return modifications


def _verify_layout(conn: MySQLConnection) -> None:
    definitions = _definitions()
    for table, definition in definitions.items():
        _inspect_table(conn, table, definition, allowed_collations={_TARGET_COLLATION})
        rows = _index_rows(conn, table)
        actual = [(str(row[0]), int(row[1]), str(row[2]), str(row[3]).upper())
                  for row in rows]
        expected = [(definition["index"], i, column, "FULLTEXT")
                    for i, (column, _type, _nullable) in enumerate(definition["columns"], 1)]
        if actual != expected:
            raise MySQLSchemaError(f"native FULLTEXT index differs for {table}")
        _verify_fulltext_index_ddl(
            conn, table, definition["index"],
            tuple(column for column, _type, _nullable in definition["columns"]),
        )


def install_fulltext_collation_v12(conn: MySQLConnection, install: bool = False) -> None:
    """Install or verify V12. Runtime callers must use the verify-only default."""
    if conn.in_transaction:
        raise MySQLSchemaError("full-text collation migration cannot run inside a transaction")
    _verify_v11(conn)
    name, checksum = _NAME, _checksum()
    current = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_VERSION,),
    ).fetchone()
    if current is not None:
        if current[0] != name or current[1] != checksum:
            raise MySQLSchemaError("full-text collation V12 migration checksum/name mismatch")
        try:
            _verify_layout(conn)
            return
        except MySQLSchemaError:
            if not install:
                raise
    if not install:
        raise MySQLSchemaError("full-text collation V12 is missing; run the schema migration")
    database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
    if not database:
        raise MySQLSchemaError("full-text collation V12 migration needs database identity")
    lock_name = "lka-fulltext-collation-v12-" + hashlib.sha256(str(database).encode()).hexdigest()[:32]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLSchemaError("timed out acquiring full-text collation V12 migration lock")
    try:
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_VERSION,),
        ).fetchone()
        if current is not None:
            if current[0] != name or current[1] != checksum:
                raise MySQLSchemaError("full-text collation V12 migration checksum/name mismatch")
            try:
                _verify_layout(conn)
                return
            except MySQLSchemaError:
                # An exact V12 ledger row is written before DDL. It is the
                # recovery marker for an interrupted, auto-committing migration.
                pass
        definitions = _definitions()
        current_collations = {_SOURCE_COLLATION, _TARGET_COLLATION}
        recovery = current is not None
        column_shapes = {}
        for table, definition in definitions.items():
            column_shapes[table] = _inspect_table(
                conn, table, definition,
                allowed_collations=current_collations if recovery else {_SOURCE_COLLATION},
                allow_missing_index=recovery,
            )
        if current is None:
            # MySQL DDL commits implicitly. A pre-DDL exact checksum row marks
            # intermediate states as owned by this installer, so a retry can
            # distinguish them from pre-existing schema damage.
            conn.execute_native(
                "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
                (_VERSION, name, checksum),
            )
            conn.commit()
        for table, definition in definitions.items():
            index = definition["index"]
            if _index_rows(conn, table):
                conn.execute_native(f"DROP INDEX `{index}` ON `{table}`")
            for column, _expected_type, _expected_nullable in definition["columns"]:
                column_type, nullable = column_shapes[table][column]
                null_clause = "NULL" if nullable == "YES" else "NOT NULL"
                conn.execute_native(
                    f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {column_type} "
                    f"CHARACTER SET utf8mb4 COLLATE `{_TARGET_COLLATION}` {null_clause}"
                )
            fields = ",".join(f"`{column}`" for column, _type, _nullable in definition["columns"])
            conn.execute_native(
                f"CREATE FULLTEXT INDEX `{index}` ON `{table}` ({fields}) WITH PARSER ngram"
            )
        _verify_layout(conn)
        conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()
