"""Import an explicitly prepared SQLite snapshot into an empty MySQL schema.

This is a data migration tool, not a schema installer or a live-copy utility.
It refuses unknown source tables and non-empty targets. The source path must
name a consistent, stopped-service snapshot; see ``docs/mysql.md`` for the
required snapshot and cutover procedure.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import struct
import sys
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


class ImportErrorSafe(RuntimeError):
    """A migration refusal whose message contains no row content or secrets."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,190}$")
_FTS_TABLES = {
    "conversation_history_fts": ("content",),
    "knowledge_chunks_fts": ("chunk_id", "document_id", "title", "text", "source_type"),
    "mail_messages_fts": ("message_id", "subject", "sender", "folder", "body_text"),
    "matters_fts": ("matter_id", "title", "summary", "tags"),
    "memory_entries_fts": ("memory_id", "content", "memory_type"),
}
_FTS_SHADOW_SUFFIXES = {"config", "content", "data", "docsize", "idx"}
_MYSQL_FTS_TABLES = set(_FTS_TABLES)
_IMPORT_STATE_TABLE = "lka_data_imports"
_KNOWLEDGE_VEC_BASE = re.compile(r"^knowledge_vec_([0-9a-f]{16})$")
_KNOWLEDGE_VEC_VTAB_SQL = re.compile(
    r"^\s*CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[`\"]?([A-Za-z_][A-Za-z0-9_$]*)[`\"]?\s+USING\s+vec0\s*\(\s*"
    r"vector_id\s+INTEGER\s+PRIMARY\s+KEY\s*,\s*"
    r"embedding\s+float\s*\[\s*(\d+)\s*\]\s+"
    r"distance_metric\s*=\s*cosine\s*\)\s*;?\s*$",
    re.IGNORECASE,
)
_KNOWLEDGE_VEC_SHADOWS = {
    "chunks": (
        ("chunk_id", "INTEGER", 0, 1), ("size", "INTEGER", 1, 0),
        ("validity", "BLOB", 1, 0), ("rowids", "BLOB", 1, 0),
    ),
    "info": (("key", "TEXT", 0, 1), ("value", "ANY", 0, 0)),
    "rowids": (
        ("rowid", "INTEGER", 0, 1), ("id", "", 0, 0),
        ("chunk_id", "INTEGER", 0, 0), ("chunk_offset", "INTEGER", 0, 0),
    ),
    "vector_chunks00": (("rowid", "", 0, 1), ("vectors", "BLOB", 1, 0)),
}
_COPY_PAYLOAD_BUDGET = 3 * 1024 * 1024


@dataclass(frozen=True)
class KnowledgeVectorTable:
    name: str
    index_key: str
    dimensions: int


@dataclass(frozen=True)
class KnowledgeVectorInventory:
    source_tables: tuple[str, ...]
    rows: int
    digest: str
    tables: tuple[KnowledgeVectorTable, ...] = ()
    empty_source_tables: tuple[str, ...] = ()


def _ident(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ImportErrorSafe("source schema contains an unsupported identifier")
    return f"`{value}`"


@dataclass(frozen=True)
class TableInventory:
    name: str
    rows: int
    source_columns: tuple[str, ...]
    rowid_column: str | None
    rowid_kind: str | None
    sequence: int | None
    digest: str


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_xinfo({_ident(table)})").fetchall()
    # SQLite hidden=1 columns belong to virtual-table internals; generated
    # columns (2/3) are not accepted because the native catalog has no port.
    if any(int(row[6]) in (2, 3) for row in rows):
        raise ImportErrorSafe(f"generated columns are not supported in source table {table}")
    return [str(row[1]) for row in rows if int(row[6]) == 0]


def _source_rowid_name(columns: Iterable[str], *, integer_pk: str | None = None) -> str:
    if integer_pk is not None:
        return integer_pk
    used = {str(column).casefold() for column in columns}
    for candidate in ("rowid", "_rowid_", "oid"):
        if candidate not in used:
            return candidate
    raise ImportErrorSafe("source table shadows every SQLite rowid alias")


def _load_sqlite_vec_for_snapshot(conn: sqlite3.Connection) -> bool:
    """Load only the packaged sqlite-vec extension when a reviewed vec0 table exists."""
    rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    vector_tables = [
        (str(name), str(sql or "")) for name, sql in rows
        if re.search(r"\bUSING\s+vec0\b", str(sql or ""), re.IGNORECASE)
    ]
    if not vector_tables:
        return False
    for name, statement in vector_tables:
        match = _KNOWLEDGE_VEC_VTAB_SQL.fullmatch(statement)
        if not _KNOWLEDGE_VEC_BASE.fullmatch(name) or match is None or match.group(1) != name:
            raise ImportErrorSafe("SQLite snapshot contains an unsupported vec0 virtual table")
        if int(match.group(2)) < 1:
            raise ImportErrorSafe("SQLite vec0 dimension metadata is invalid")
    try:
        import sqlite_vec
    except ImportError as exc:
        raise ImportErrorSafe("SQLite vec0 source requires the packaged sqlite_vec extension") from exc
    module_path = Path(getattr(sqlite_vec, "__file__", "")).resolve(strict=False)
    if not module_path.is_relative_to(Path(sys.prefix).resolve(strict=False)):
        raise ImportErrorSafe("SQLite vec0 extension did not resolve from the active Python environment")
    try:
        conn.enable_load_extension(True)
        try:
            # sqlite_vec.load selects its bundled extension; no caller-supplied
            # extension path is ever loaded.
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except Exception as exc:
        try:
            conn.enable_load_extension(False)
        except sqlite3.Error:
            pass
        raise ImportErrorSafe("could not load the packaged sqlite_vec extension") from exc
    return True


def _knowledge_vec_catalog(conn: sqlite3.Connection) -> tuple[list[KnowledgeVectorTable], set[str]]:
    objects = conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    all_names = {str(name) for _kind, name, _sql in objects}
    virtuals: list[tuple[str, int]] = []
    for kind, name, sql in objects:
        name, statement = str(name), str(sql or "")
        if not re.search(r"\bUSING\s+vec0\b", statement, re.IGNORECASE):
            continue
        match = _KNOWLEDGE_VEC_VTAB_SQL.fullmatch(statement)
        if not _KNOWLEDGE_VEC_BASE.fullmatch(name) or match is None or match.group(1) != name:
            raise ImportErrorSafe("SQLite snapshot contains an unsupported vec0 virtual table")
        if str(kind) != "table":
            raise ImportErrorSafe("SQLite vec0 catalog object has an unsupported type")
        virtuals.append((name, int(match.group(2))))
    excluded: set[str] = set()
    tables: list[KnowledgeVectorTable] = []
    listed = {str(row[1]): str(row[2]) for row in conn.execute("PRAGMA table_list")}
    for name, dimensions in virtuals:
        if dimensions < 1 or listed.get(name) != "virtual":
            raise ImportErrorSafe("SQLite vec0 virtual table metadata is inconsistent")
        prefix = name + "_"
        shadow_names = {
            suffix: prefix + suffix for suffix in _KNOWLEDGE_VEC_SHADOWS
        }
        if not set(shadow_names.values()).issubset(all_names):
            raise ImportErrorSafe("SQLite vec0 shadow-table inventory is incomplete")
        for suffix, shadow_name in shadow_names.items():
            expected = _KNOWLEDGE_VEC_SHADOWS[suffix]
            info = conn.execute(f"PRAGMA table_info({_ident(shadow_name)})").fetchall()
            actual = tuple(
                (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
                for row in info
            )
            if actual != expected:
                raise ImportErrorSafe("SQLite vec0 shadow-table definition is unrecognized")
            if listed.get(shadow_name) not in {"table", "shadow"}:
                raise ImportErrorSafe("SQLite vec0 shadow-table catalog type is unrecognized")
            excluded.add(shadow_name)
        extras = {
            candidate for candidate in all_names
            if candidate.startswith(prefix) and candidate not in shadow_names.values()
        }
        if extras:
            raise ImportErrorSafe("SQLite vec0 has an unrecognized shadow table")
        suffix = _KNOWLEDGE_VEC_BASE.fullmatch(name).group(1)
        tables.append(KnowledgeVectorTable(name, suffix, dimensions))
        excluded.add(name)
    return tables, excluded


def _source_tables(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    objects = conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    tables = {str(name) for kind, name, _sql in objects if kind == "table"}
    views = {str(name) for kind, name, _sql in objects if kind == "view"}
    if views:
        raise ImportErrorSafe("SQLite snapshot contains views; inventory and migrate them explicitly")
    # sqlite_sequence and sqlite_stat* are engine metadata. FTS5 shadow tables
    # are disposable indexes and must not be confused with the logical FTS rows.
    excluded = {name for name in tables if name.startswith("sqlite_")}
    _vec_tables, vec_excluded = _knowledge_vec_catalog(conn)
    excluded.update(vec_excluded)
    fts_virtuals = {
        str(name) for kind, name, sql in objects
        if kind == "table" and str(name) in _FTS_TABLES
        and sql and re.search(r"\bUSING\s+fts5\b", str(sql), re.IGNORECASE)
    }
    for name in tables:
        for fts_name in fts_virtuals:
            if name.startswith(fts_name + "_") and name[len(fts_name) + 1:] in _FTS_SHADOW_SUFFIXES:
                excluded.add(name)
    return tables - excluded, excluded


def _sqlite_sequence(conn: sqlite3.Connection) -> dict[str, int]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        return {}
    return {str(name): int(seq) for name, seq in conn.execute("SELECT name,seq FROM sqlite_sequence")}


def _tagged_digest_value(value: Any) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, bytes):
        return b"B" + struct.pack(">Q", len(value)) + value
    if isinstance(value, str):
        data = value.encode("utf-8", "surrogatepass")
        return b"S" + struct.pack(">Q", len(data)) + data
    if isinstance(value, bool):
        return b"I1" if value else b"I0"
    if isinstance(value, int):
        return b"I" + str(value).encode("ascii") + b";"
    if isinstance(value, float):
        if not math.isfinite(value):
            return b"F" + repr(value).encode("ascii") + b";"
        return b"F" + struct.pack(">d", value)
    # PyMySQL may return Decimal for a SQLite NUMERIC-affinity value. Decimal's
    # text form is deterministic and keeps the stored numeric value auditable.
    if value.__class__.__module__ == "decimal":
        return b"D" + str(value).encode("ascii") + b";"
    raise ImportErrorSafe("a database value has an unsupported driver type")


def _digest_rows(rows: Iterable[Iterable[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(b"R")
        for value in row:
            encoded = _tagged_digest_value(value)
            digest.update(struct.pack(">Q", len(encoded)))
            digest.update(encoded)
    return digest.hexdigest()


def _source_digest(conn: sqlite3.Connection, table: str, columns: list[str], rowid: str) -> str:
    select = ",".join(_ident(column) for column in columns)
    rows = conn.execute(
        f"SELECT {_ident(rowid)},{select} FROM {_ident(table)} ORDER BY {_ident(rowid)}"
    )
    return _digest_rows(rows)


def _target_digest(conn, table: str, columns: list[str], rowid: str,
                   *, batch_size: int = 250) -> str:
    """Digest a target in bounded pages using its numeric SQLite-rowid proxy."""
    if not 1 <= batch_size <= 2000:
        raise ValueError("batch_size must be between 1 and 2000")
    digest = hashlib.sha256()
    rowid_index = next(
        (index for index, column in enumerate(columns) if column.casefold() == rowid.casefold()),
        None,
    )
    selected = list(columns)
    if rowid_index is None:
        selected.append(rowid)
    select = ",".join(_ident(column) for column in selected)
    last_key: int | None = None
    while True:
        if last_key is None:
            statement = (
                f"SELECT {select} FROM {_ident(table)} "
                f"ORDER BY {_ident(rowid)} LIMIT %s"
            )
            parameters = (batch_size,)
        else:
            statement = (
                f"SELECT {select} FROM {_ident(table)} WHERE {_ident(rowid)}>%s "
                f"ORDER BY {_ident(rowid)} LIMIT %s"
            )
            parameters = (last_key, batch_size)
        cursor = conn.execute_native(statement, parameters)
        try:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                key = row[rowid_index] if rowid_index is not None else row[-1]
                try:
                    numeric_key = int(key)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ImportErrorSafe(f"target rowid proxy is invalid for table {table}") from exc
                if key is None or (last_key is not None and numeric_key <= last_key):
                    raise ImportErrorSafe(f"target rowid ordering is invalid for table {table}")
                last_key = numeric_key
                _digest_update(digest, row[:len(columns)])
        finally:
            cursor.close()
        if len(rows) < batch_size:
            break
    return digest.hexdigest()


def _fk_order(conn: sqlite3.Connection, tables: set[str]) -> list[str]:
    dependencies: dict[str, set[str]] = {table: set() for table in tables}
    for table in tables:
        for row in conn.execute(f"PRAGMA foreign_key_list({_ident(table)})"):
            referenced = str(row[2])
            if referenced in tables and referenced != table:
                dependencies[table].add(referenced)
    ordered: list[str] = []
    remaining = {table: set(refs) for table, refs in dependencies.items()}
    while remaining:
        ready = sorted(table for table, refs in remaining.items() if not refs)
        if not ready:
            # Session foreign_key_checks is disabled only during row insertion;
            # source and target FK validation both run before completion.
            ordered.extend(sorted(remaining))
            break
        for table in ready:
            ordered.append(table)
            remaining.pop(table)
        for refs in remaining.values():
            refs.difference_update(ready)
    return ordered


def _mysql_business_tables(conn) -> set[str]:
    from app.storage.mysql_schema import MySQLSchema

    rows = MySQLSchema(str(conn._pool.config.database)).master_objects(conn, object_type="table")
    return {str(name) for name, _sql in rows if str(name) not in _MYSQL_FTS_TABLES}


def _mysql_rowid_map(conn, tables: set[str]) -> dict[str, tuple[str, str]]:
    rows = conn.execute_native(
        "SELECT table_name,column_name,proxy_kind FROM lka_mysql_rowid_columns"
    ).fetchall()
    mapping = {str(table): (str(column), str(kind)) for table, column, kind in rows}
    missing = sorted(tables - set(mapping))
    if missing:
        raise ImportErrorSafe("verified target schema lacks rowid metadata for a source table")
    return {table: mapping[table] for table in tables}


def _validate_mysql_vector_table(conn) -> None:
    columns = conn.execute_native(
        "SELECT COLUMN_NAME,COLUMN_TYPE,DATA_TYPE,IS_NULLABLE,EXTRA "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='knowledge_vector_blobs' ORDER BY ORDINAL_POSITION"
    ).fetchall()
    if len(columns) != 2 or [str(row[0]) for row in columns] != ["vector_id", "embedding"]:
        raise ImportErrorSafe("verified MySQL knowledge vector table has an unexpected column layout")
    vector_id_type = str(columns[0][1]).lower()
    if (str(columns[0][3]).upper() != "NO" or str(columns[0][4] or "")
            or str(columns[1][2]).lower() != "longblob"
            or str(columns[1][3]).upper() != "NO" or str(columns[1][4] or "")):
        raise ImportErrorSafe("verified MySQL knowledge vector table has unsupported column types")
    metadata_type = conn.execute_native(
        "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='knowledge_embedding_records' AND COLUMN_NAME='vector_id'"
    ).fetchone()
    if metadata_type is None or str(metadata_type[0]).lower() != vector_id_type:
        raise ImportErrorSafe("MySQL knowledge vector ID type differs from its metadata table")
    primary_key = conn.execute_native(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='knowledge_vector_blobs' AND INDEX_NAME='PRIMARY' "
        "AND COLUMN_NAME='vector_id' AND SEQ_IN_INDEX=1"
    ).fetchone()
    if int(primary_key[0]) != 1:
        raise ImportErrorSafe("MySQL knowledge vector ID is not the expected primary key")
    relationship = conn.execute_native(
        "SELECT k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME,r.DELETE_RULE "
        "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS r "
        "ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.TABLE_NAME=k.TABLE_NAME "
        "AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE k.CONSTRAINT_SCHEMA=DATABASE() "
        "AND k.TABLE_NAME='knowledge_vector_blobs' AND k.COLUMN_NAME='vector_id' "
        "AND k.REFERENCED_TABLE_NAME IS NOT NULL"
    ).fetchone()
    if relationship is None or tuple(map(str, relationship)) != (
        "knowledge_embedding_records", "vector_id", "CASCADE",
    ):
        raise ImportErrorSafe("MySQL knowledge vector metadata cascade is not installed")


def _target_visible_columns(conn, table: str) -> set[str]:
    rows = conn.execute_native(
        "SELECT COLUMN_NAME,EXTRA FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,),
    ).fetchall()
    return {str(name) for name, extra in rows if "INVISIBLE" not in str(extra or "").upper()}


def _schema_ast_key(schema, statement: str) -> str | None:
    from sqlglot.errors import ParseError

    from app.storage.mysql_schema import MySQLSchemaError

    try:
        return schema._probe_ast_key(statement)
    except (ParseError, MySQLSchemaError):
        return None


def _schema_ast_key_with_column_order_normalized(schema, statement: str, *, drop_manager_reasoning_check: bool = False) -> str | None:
    """Canonicalize only column order (and one documented historical CHECK)."""
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    from app.storage.mysql_schema import MySQLSchemaError

    try:
        parsed = parse_one(statement, read="sqlite")
        if not isinstance(parsed, exp.Create) or not isinstance(parsed.this, exp.Schema):
            return None
        expressions = list(parsed.this.expressions)
        column_positions = [index for index, item in enumerate(expressions) if isinstance(item, exp.ColumnDef)]
        if not column_positions:
            return None
        columns = [expressions[index] for index in column_positions]
        if drop_manager_reasoning_check:
            reasoning = [item for item in columns if str(item.this.name).casefold() == "reasoning_level"]
            if len(reasoning) != 1:
                return None
            column = reasoning[0]
            constraints = list(column.args.get("constraints") or [])
            checks = [
                item for item in constraints
                if isinstance(item, exp.ColumnConstraint)
                and isinstance(item.args.get("kind"), exp.CheckColumnConstraint)
            ]
            if len(checks) != 1:
                return None
            column.set("constraints", [item for item in constraints if item not in checks])
        columns.sort(key=lambda item: str(item.this.name).casefold())
        for index, column in zip(column_positions, columns, strict=True):
            expressions[index] = column
        parsed.this.set("expressions", expressions)
        return _schema_ast_key(schema, parsed.sql(dialect="sqlite"))
    except (ParseError, MySQLSchemaError):
        return None


def _manager_reasoning_values_are_supported(source: sqlite3.Connection) -> bool:
    invalid = source.execute(
        "SELECT 1 FROM manager_controllers WHERE reasoning_level IS NOT NULL "
        "AND reasoning_level NOT IN ('off','low','high','max') LIMIT 1"
    ).fetchone()
    return invalid is None


def _inventory_source(
    conn: sqlite3.Connection, target_tables: set[str], rowids,
) -> tuple[list[TableInventory], list[str]]:
    tables, excluded = _source_tables(conn)
    fts_present = tables & set(_FTS_TABLES)
    if fts_present != set(_FTS_TABLES):
        raise ImportErrorSafe("SQLite snapshot is missing a logical FTS projection; upgrade a copy first")
    fts_sql = {
        str(name): str(sql or "") for kind, name, sql in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE type='table'"
        ) if str(name) in _FTS_TABLES
    }
    if any(not re.search(r"\bUSING\s+fts5\b", sql, re.IGNORECASE) for sql in fts_sql.values()):
        raise ImportErrorSafe("source full-text tables are not the expected FTS5 projections")
    ordinary = tables - fts_present
    unknown = ordinary - target_tables
    if unknown:
        raise ImportErrorSafe("SQLite snapshot has unknown application tables; explicit mapping is required")
    all_sequences = _sqlite_sequence(conn)
    if set(all_sequences) - ordinary - excluded:
        raise ImportErrorSafe("SQLite sequence metadata refers to a table outside the import inventory")
    sequence = {name: value for name, value in all_sequences.items() if name in ordinary}
    inventory: list[TableInventory] = []
    for table in sorted(ordinary):
        columns = _column_names(conn, table)
        if not columns:
            raise ImportErrorSafe(f"source table {table} has no supported columns")
        rowid_col, rowid_kind = rowids[table]
        source_count = int(conn.execute(f"SELECT count(*) FROM {_ident(table)}").fetchone()[0])
        # Use rowid for ordering and digest. For INTEGER PRIMARY KEY this is the
        # explicit primary-key alias; for other tables it remains implicit.
        rowid_expr = _source_rowid_name(
            columns, integer_pk=rowid_col if rowid_kind == "integer_primary_key" else None,
        )
        if rowid_kind == "invisible_auto_increment":
            minimum = conn.execute(
                f"SELECT min({_ident(rowid_expr)}) FROM {_ident(table)}"
            ).fetchone()[0]
            if minimum is not None and int(minimum) < 0:
                raise ImportErrorSafe(f"negative implicit rowid cannot fit the target proxy for table {table}")
        digest = _source_digest(conn, table, columns, rowid_expr)
        inventory.append(TableInventory(
            table, source_count, tuple(columns), rowid_col, rowid_kind,
            sequence.get(table), digest,
        ))
    for table in sorted(fts_present):
        columns = ["rowid", *_FTS_TABLES[table]]
        count = int(conn.execute(f"SELECT count(*) FROM {_ident(table)}").fetchone()[0])
        minimum = conn.execute(f"SELECT min(rowid) FROM {_ident(table)}").fetchone()[0]
        if minimum is not None and int(minimum) < 0:
            raise ImportErrorSafe(f"negative FTS rowid cannot fit the target proxy for table {table}")
        digest = _source_digest(conn, table, _FTS_TABLES[table], "rowid")
        inventory.append(TableInventory(
            table, count, tuple(columns), "rowid", "fts_rowid", sequence.get(table), digest,
        ))
    return inventory, sorted(excluded)


def _validate_source_target_columns(source: sqlite3.Connection, mysql, inventory: list[TableInventory], rowids):
    schema = getattr(mysql, "_schema", None)
    if schema is None:
        raise ImportErrorSafe("verified MySQL schema metadata is unavailable")
    for item in inventory:
        source_row = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (item.name,),
        ).fetchone()
        object_type = "fts" if item.name in _FTS_TABLES else "table"
        target_source = schema.master_objects(mysql, object_type=object_type, names=[item.name])
        if source_row is None or len(target_source) != 1 or not target_source[0][1]:
            raise ImportErrorSafe(f"reviewed source schema is missing for table {item.name}")
        source_sql = str(source_row[0])
        accepted_sql = [str(target_source[0][1]), *schema.object_source_variants(mysql, object_type, item.name)]
        normalized = schema._normalize_probe_sql(source_sql)
        source_ast = _schema_ast_key(schema, source_sql)
        matches = False
        legacy_reasoning_check = False
        for accepted in accepted_sql:
            if normalized == schema._normalize_probe_sql(accepted):
                matches = True
                break
            accepted_ast = _schema_ast_key(schema, accepted)
            if source_ast is not None and source_ast == accepted_ast:
                matches = True
                break
            source_orderless = _schema_ast_key_with_column_order_normalized(schema, source_sql)
            accepted_orderless = _schema_ast_key_with_column_order_normalized(schema, accepted)
            if source_orderless is not None and source_orderless == accepted_orderless:
                matches = True
                break
            if item.name == "manager_controllers":
                historical = _schema_ast_key_with_column_order_normalized(
                    schema, accepted, drop_manager_reasoning_check=True,
                )
                if source_orderless is not None and source_orderless == historical:
                    matches = True
                    legacy_reasoning_check = True
                    break
        if not matches:
            raise ImportErrorSafe(f"source schema version is not accepted for table {item.name}")
        if legacy_reasoning_check and not _manager_reasoning_values_are_supported(source):
            raise ImportErrorSafe("source manager reasoning_level contains values outside the supported target constraint")
        if item.name in _FTS_TABLES:
            target = _target_visible_columns(mysql, item.name)
            expected = {"rowid", *_FTS_TABLES[item.name]}
        else:
            source_columns = set(item.source_columns)
            target = _target_visible_columns(mysql, item.name)
            rowid_column, kind = rowids[item.name]
            if not source_columns.issubset(target):
                raise ImportErrorSafe(f"source/target columns differ for table {item.name}")
            extra_columns = target - source_columns
            if extra_columns:
                metadata = mysql.execute_native(
                    "SELECT COLUMN_NAME,IS_NULLABLE,COLUMN_DEFAULT,EXTRA,GENERATION_EXPRESSION "
                    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME=%s", (item.name,),
                ).fetchall()
                for name, nullable, default, extra, generated in metadata:
                    if str(name) not in extra_columns:
                        continue
                    extra_text = str(extra or "").upper()
                    if (str(nullable).upper() != "YES" and default is None) or extra_text or generated:
                        raise ImportErrorSafe(
                            f"target-only column is not safely defaultable in table {item.name}"
                        )
            if kind == "integer_primary_key" and rowid_column not in source_columns:
                raise ImportErrorSafe(f"integer rowid metadata is incompatible for table {item.name}")
            if kind == "invisible_auto_increment":
                metadata = mysql.execute_native(
                    "SELECT EXTRA FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME=%s AND COLUMN_NAME=%s", (item.name, rowid_column),
                ).fetchone()
                if metadata is None or "INVISIBLE" not in str(metadata[0]).upper():
                    raise ImportErrorSafe(f"hidden rowid proxy is missing for table {item.name}")
            continue
        if target != expected:
            raise ImportErrorSafe(f"source/target projection differs for table {item.name}")


def _trigger_snapshot(conn) -> list[dict[str, Any]]:
    rows = conn.execute_native(
        "SELECT TRIGGER_NAME,EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_ORDER,"
        "SQL_MODE,DEFINER,CHARACTER_SET_CLIENT,COLLATION_CONNECTION,DATABASE_COLLATION "
        "FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
        "ORDER BY EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_ORDER"
    ).fetchall()
    triggers: list[dict[str, Any]] = []
    for row in rows:
        name = str(row[0])
        result = conn.execute_native(f"SHOW CREATE TRIGGER {_ident(name)}")
        description = [str(item[0]) for item in result.description or ()]
        created = result.fetchone()
        if created is None:
            raise ImportErrorSafe("could not snapshot a verified MySQL trigger")
        values = dict(zip(description, created))
        statement = values.get("SQL Original Statement") or values.get("Create Trigger")
        if not statement:
            raise ImportErrorSafe("MySQL did not return a trigger definition")
        triggers.append({
            "name": name, "table": str(row[1]), "timing": str(row[2]),
            "event": str(row[3]), "action_order": int(row[4]),
            "sql_mode": str(row[5] or ""), "definer": str(row[6] or ""),
            "character_set_client": str(row[7] or "utf8mb4"),
            "collation_connection": str(row[8] or "utf8mb4_0900_bin"),
            "database_collation": str(row[9] or "utf8mb4_0900_bin"),
            "create_sql": str(statement),
        })
    return triggers


def _drop_triggers(conn, triggers: list[dict[str, Any]]) -> None:
    for trigger in triggers:
        conn.execute_native(f"DROP TRIGGER {_ident(trigger['name'])}")


def _restore_triggers(conn, triggers: list[dict[str, Any]]) -> None:
    # MySQL assigns ACTION_ORDER by creation order within each event group.
    wanted = {item["name"] for item in triggers}
    present = _trigger_snapshot(conn)
    for item in present:
        if item["name"] in wanted:
            conn.execute_native(f"DROP TRIGGER {_ident(item['name'])}")
    ordered = sorted(triggers, key=lambda item: (
        item["table"].casefold(), item["timing"], item["event"], item["action_order"],
    ))
    for trigger in ordered:
        conn.execute_native("SET SESSION sql_mode=%s", (trigger["sql_mode"],))
        conn.execute_native(
            f"SET NAMES {_sql_string(trigger['character_set_client'])} "
            f"COLLATE {_sql_string(trigger['collation_connection'])}"
        )
        conn.execute_native(trigger["create_sql"])
    conn.execute_native("SET SESSION sql_mode=DEFAULT")
    conn.execute_native("SET NAMES utf8mb4 COLLATE utf8mb4_0900_bin")


def _sql_string(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ImportErrorSafe("trigger session metadata contains unsupported charset text")
    return "'" + value + "'"


def _target_is_empty(conn, tables: set[str]) -> None:
    for table in sorted(tables | _MYSQL_FTS_TABLES):
        count = conn.execute_native(f"SELECT COUNT(*) FROM {_ident(table)}").fetchone()[0]
        if int(count):
            raise ImportErrorSafe("MySQL target is not empty; import requires a fresh schema")


def _assert_fresh_import_state(conn, *, create: bool = False) -> None:
    exists = conn.execute_native(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE' LIMIT 1", (_IMPORT_STATE_TABLE,),
    ).fetchone() is not None
    if not exists and not create:
        return
    if not exists:
        conn.execute_native(
            "CREATE TABLE `lka_data_imports` ("
            "import_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,"
            "status VARCHAR(16) NOT NULL,"
            "source_fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
            "started_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),"
            "completed_at TIMESTAMP(6) NULL,"
            "CONSTRAINT chk_lka_data_import_status CHECK (status IN ('in_progress','failed','verified'))"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin"
        )
        conn.commit()
        return
    columns = conn.execute_native(
        "SELECT COLUMN_NAME,DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,EXTRA "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (_IMPORT_STATE_TABLE,),
    ).fetchall()
    expected = {
        ("import_id", "char", "char(32)", "NO", ""),
        ("status", "varchar", "varchar(16)", "NO", ""),
        ("source_fingerprint", "char", "char(64)", "NO", ""),
        ("started_at", "timestamp", "timestamp(6)", "NO", "DEFAULT_GENERATED"),
        ("completed_at", "timestamp", "timestamp(6)", "YES", ""),
    }
    actual = {
        (str(name), str(data_type).lower(), str(column_type).lower(), str(nullable).upper(), str(extra or "").upper())
        for name, data_type, column_type, nullable, extra in columns
    }
    # MySQL may vary EXTRA casing; normalize while keeping every shape strict.
    actual = {(name, data_type, column_type, nullable, extra.casefold())
              for name, data_type, column_type, nullable, extra in actual}
    expected = {(name, data_type, column_type, nullable, extra.casefold())
                for name, data_type, column_type, nullable, extra in expected}
    if actual != expected:
        raise ImportErrorSafe("existing import state table has an unrecognized definition")
    count = conn.execute_native(f"SELECT COUNT(*) FROM `{_IMPORT_STATE_TABLE}`").fetchone()[0]
    if int(count):
        raise ImportErrorSafe("target already has an import attempt; inspect or rebuild it explicitly")


def _set_import_status(conn, import_id: str, status: str, fingerprint: str | None = None) -> None:
    if status == "in_progress":
        conn.execute_native(
            f"INSERT INTO `{_IMPORT_STATE_TABLE}`(import_id,status,source_fingerprint) "
            "VALUES(%s,'in_progress',%s)", (import_id, fingerprint),
        )
    elif status in {"failed", "verified"}:
        allowed_previous = "status='in_progress'" if status == "verified" else "status IN ('in_progress','verified')"
        conn.execute_native(
            f"UPDATE `{_IMPORT_STATE_TABLE}` SET status=%s,completed_at=UTC_TIMESTAMP(6) "
            f"WHERE import_id=%s AND {allowed_previous}", (status, import_id),
        )
        found = conn.execute_native(
            f"SELECT status FROM `{_IMPORT_STATE_TABLE}` WHERE import_id=%s", (import_id,),
        ).fetchone()
        if found is None or str(found[0]) != status:
            raise ImportErrorSafe("could not persist the import completion state")
    else:
        raise ValueError("unsupported import state")
    conn.commit()


def _source_fingerprint(inventory: list[TableInventory],
                        knowledge_vectors: KnowledgeVectorInventory | None = None) -> str:
    public_inventory = [(item.name, item.rows, item.sequence, item.digest) for item in inventory]
    vector_identity = None if knowledge_vectors is None else {
        "source_tables": knowledge_vectors.source_tables,
        "empty_source_tables": knowledge_vectors.empty_source_tables,
        "rows": knowledge_vectors.rows,
        "sha256": knowledge_vectors.digest,
    }
    raw = json.dumps(
        {"tables": public_inventory, "knowledge_vectors": vector_identity},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _verify_source_sequences(source: sqlite3.Connection, inventory: list[TableInventory]) -> None:
    current = _sqlite_sequence(source)
    for item in inventory:
        if current.get(item.name) != item.sequence:
            raise ImportErrorSafe("SQLite sequence metadata changed during import")


def _digest_update(digest, row: Iterable[Any]) -> None:
    digest.update(b"R")
    for value in row:
        encoded = _tagged_digest_value(value)
        digest.update(struct.pack(">Q", len(encoded)))
        digest.update(encoded)


def _iter_knowledge_vector_rows(source: sqlite3.Connection,
                               vector_tables: tuple[KnowledgeVectorTable, ...]):
    """Yield metadata-matched raw float32 vectors in global vector_id order."""
    metadata_exists = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_embedding_records'"
    ).fetchone() is not None
    if not metadata_exists:
        if vector_tables:
            raise ImportErrorSafe("SQLite vec0 index lacks knowledge vector metadata")
        return
    counts = {
        str(index_key): int(count)
        for index_key, count in source.execute(
            "SELECT index_key,COUNT(*) FROM knowledge_embedding_records GROUP BY index_key"
        )
    }
    by_key = {item.index_key: item for item in vector_tables}
    if len(by_key) != len(vector_tables):
        raise ImportErrorSafe("SQLite vec0 index keys are not unique")
    for index_key, count in counts.items():
        if count and index_key not in by_key:
            raise ImportErrorSafe("knowledge vector metadata has no matching vec0 cache")
        if index_key in by_key:
            table = by_key[index_key]
            if source.execute(
                "SELECT 1 FROM knowledge_embedding_records WHERE index_key=? AND dimensions<>? LIMIT 1",
                (index_key, table.dimensions),
            ).fetchone() is not None:
                raise ImportErrorSafe("knowledge vector metadata dimensions differ from vec0 cache")
    for table in vector_tables:
        if table.index_key not in counts:
            raise ImportErrorSafe("SQLite vec0 cache has no matching knowledge vector metadata")
        digest_suffix = hashlib.sha256(table.index_key.encode("utf-8")).hexdigest()[:16]
        if digest_suffix != _KNOWLEDGE_VEC_BASE.fullmatch(table.name).group(1):
            raise ImportErrorSafe("SQLite vec0 table name does not match its knowledge index key")

    cursors = [
        source.execute(
            f"SELECT vector_id,embedding FROM {_ident(item.name)} ORDER BY vector_id"
        )
        for item in vector_tables
    ]
    metadata_cursor = source.execute(
        "SELECT vector_id,index_key,dimensions FROM knowledge_embedding_records ORDER BY vector_id"
    )
    metadata_row = metadata_cursor.fetchone()
    table_by_key = {item.index_key: item for item in vector_tables}
    try:
        for vector_row in heapq.merge(*cursors, key=lambda row: int(row[0])):
            vector_id, raw_embedding = int(vector_row[0]), vector_row[1]
            if metadata_row is None or int(metadata_row[0]) != vector_id:
                raise ImportErrorSafe("SQLite vec0 vectors and metadata rows do not match")
            index_key, dimensions = str(metadata_row[1]), int(metadata_row[2])
            table = table_by_key.get(index_key)
            if table is None or dimensions != table.dimensions:
                raise ImportErrorSafe("SQLite vec0 vectors and metadata model do not match")
            if not isinstance(raw_embedding, (bytes, bytearray, memoryview)):
                raise ImportErrorSafe("SQLite vec0 embedding has an unsupported storage type")
            embedding = bytes(raw_embedding)
            if len(embedding) != 4 * dimensions:
                raise ImportErrorSafe("SQLite vec0 embedding byte length differs from metadata dimensions")
            yield vector_id, embedding
            metadata_row = metadata_cursor.fetchone()
        if metadata_row is not None:
            raise ImportErrorSafe("knowledge vector metadata has no SQLite vec0 embedding")
    finally:
        for cursor in cursors:
            cursor.close()
        metadata_cursor.close()


def _knowledge_vector_inventory(source: sqlite3.Connection) -> KnowledgeVectorInventory:
    catalog_tables, _shadow_names = _knowledge_vec_catalog(source)
    vector_tables = []
    empty_tables = []
    # Resolve hash suffixes only from the persisted model/index keys.
    virtual_objects = source.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    grouped: dict[str, int] = {}
    metadata_present = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_embedding_records'"
    ).fetchone() is not None
    if metadata_present:
        grouped = {
            str(key): int(count)
            for key, count in source.execute(
                "SELECT index_key,COUNT(*) FROM knowledge_embedding_records GROUP BY index_key"
            )
        }
    for catalog_table in catalog_tables:
        name = catalog_table.name
        match = _KNOWLEDGE_VEC_BASE.fullmatch(name)
        assert match is not None
        suffix = match.group(1)
        matching_keys = [
            key for key in grouped
            if hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] == suffix
        ]
        if len(matching_keys) == 0:
            row_count = int(source.execute(f"SELECT COUNT(*) FROM {_ident(name)}").fetchone()[0])
            if row_count == 0:
                empty_tables.append(name)
                continue
            raise ImportErrorSafe("SQLite vec0 table lacks unique matching knowledge metadata")
        if len(matching_keys) != 1:
            raise ImportErrorSafe("SQLite vec0 table lacks unique matching knowledge metadata")
        vector_tables.append(KnowledgeVectorTable(name, matching_keys[0], catalog_table.dimensions))

    for name, statement in virtual_objects:
        name, statement = str(name), str(statement or "")
        match = _KNOWLEDGE_VEC_VTAB_SQL.fullmatch(statement)
        if match is None:
            continue
        if name not in {item.name for item in catalog_tables}:
            raise ImportErrorSafe("SQLite vec0 catalog inventory is inconsistent")
    if grouped and not vector_tables:
        raise ImportErrorSafe("knowledge vector metadata exists without SQLite vec0 caches")
    if not metadata_present and vector_tables:
        raise ImportErrorSafe("SQLite vec0 index lacks knowledge vector metadata")
    table_tuple = tuple(sorted(vector_tables, key=lambda item: item.name))
    digest = hashlib.sha256()
    count = 0
    for vector_id, embedding in _iter_knowledge_vector_rows(source, table_tuple):
        _digest_update(digest, (vector_id, embedding))
        count += 1
    expected_count = sum(grouped.values())
    if count != expected_count:
        raise ImportErrorSafe("SQLite vec0 vector count differs from knowledge metadata")
    return KnowledgeVectorInventory(
        tuple(item.name for item in catalog_tables), count, digest.hexdigest(), table_tuple,
        tuple(sorted(empty_tables)),
    )


def _copy_knowledge_vectors(source: sqlite3.Connection, target, inventory: KnowledgeVectorInventory,
                           *, batch_size: int) -> None:
    statement = "INSERT INTO `knowledge_vector_blobs`(`vector_id`,`embedding`) VALUES(%s,%s)"
    digest = hashlib.sha256()
    count = 0
    payload = []
    for row in _iter_knowledge_vector_rows(source, inventory.tables):
        payload.append(row)
        _digest_update(digest, row)
        count += 1
        if len(payload) >= batch_size:
            target.executemany_native(statement, payload)
            target.commit()
            payload.clear()
    if payload:
        target.executemany_native(statement, payload)
        target.commit()
    if count != inventory.rows or digest.hexdigest() != inventory.digest:
        raise ImportErrorSafe("SQLite vec0 cache changed during import")


def _verify_knowledge_vectors(target, inventory: KnowledgeVectorInventory) -> None:
    row = target.execute_native(
        "SELECT COUNT(*) FROM `knowledge_vector_blobs`"
    ).fetchone()
    if int(row[0]) != inventory.rows:
        raise ImportErrorSafe("MySQL knowledge vector row count differs from source")
    digest = _target_digest(
        target, "knowledge_vector_blobs", ["vector_id", "embedding"], "vector_id",
    )
    if digest != inventory.digest:
        raise ImportErrorSafe("MySQL knowledge vector digest differs from source")


def _payload_size(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return 16


def _row_payload_size(row: Iterable[Any]) -> int:
    return sum(_payload_size(value) for value in row)


def _copy_ordinary_table(source: sqlite3.Connection, target, item: TableInventory,
                         *, batch_size: int, payload_budget: int = _COPY_PAYLOAD_BUDGET) -> None:
    if batch_size < 1 or payload_budget < 1:
        raise ValueError("copy batch limits must be positive")
    table = item.name
    columns = list(item.source_columns)
    rowid_column, kind = item.rowid_column, item.rowid_kind
    if rowid_column is None or kind not in {"integer_primary_key", "invisible_auto_increment"}:
        raise ImportErrorSafe(f"rowid mapping is invalid for source table {table}")
    source_rowid = _source_rowid_name(
        columns, integer_pk=rowid_column if kind == "integer_primary_key" else None,
    )
    select = _ident(source_rowid) + "," + ",".join(_ident(column) for column in columns)
    cursor = source.execute(f"SELECT {select} FROM {_ident(table)} ORDER BY {_ident(source_rowid)}")
    target_columns = list(columns)
    if kind == "invisible_auto_increment":
        target_columns.append(rowid_column)
    placeholders = ",".join("%s" for _ in target_columns)
    insert = f"INSERT INTO {_ident(table)} ({','.join(_ident(c) for c in target_columns)}) VALUES ({placeholders})"
    actual = hashlib.sha256()
    count = 0
    payload: list[tuple[Any, ...]] = []
    payload_bytes = 0

    def flush() -> None:
        nonlocal payload_bytes, count
        if payload:
            target.executemany_native(insert, payload)
            target.commit()
            count += len(payload)
            payload.clear()
            payload_bytes = 0

    for row in cursor:
        source_rowid, *source_values = row
        source_rowid = int(source_rowid)
        if kind == "integer_primary_key":
            column_index = columns.index(rowid_column)
            if source_values[column_index] is None or int(source_values[column_index]) != source_rowid:
                raise ImportErrorSafe(f"integer rowid alias mismatch in table {table}")
        else:
            source_values.append(source_rowid)
        values = tuple(source_values)
        row_bytes = _row_payload_size(values)
        if payload and (len(payload) >= batch_size or payload_bytes + row_bytes > payload_budget):
            flush()
        payload.append(values)
        payload_bytes += row_bytes
        _digest_update(actual, (source_rowid, *row[1:]))
        if len(payload) >= batch_size or payload_bytes >= payload_budget:
            flush()
    flush()
    if count != item.rows or actual.hexdigest() != item.digest:
        raise ImportErrorSafe(f"source snapshot changed during import of table {table}")


def _copy_fts(source: sqlite3.Connection, target, item: TableInventory,
              *, batch_size: int, payload_budget: int = _COPY_PAYLOAD_BUDGET) -> None:
    if batch_size < 1 or payload_budget < 1:
        raise ValueError("copy batch limits must be positive")
    table = item.name
    fields = list(_FTS_TABLES[table])
    cursor = source.execute(
        f"SELECT rowid,{','.join(_ident(field) for field in fields)} "
        f"FROM {_ident(table)} ORDER BY rowid"
    )
    columns = ["rowid", *fields]
    insert = f"INSERT INTO {_ident(table)} ({','.join(_ident(c) for c in columns)}) VALUES ({','.join('%s' for _ in columns)})"
    count = 0
    digest = hashlib.sha256()
    payload: list[tuple[Any, ...]] = []
    payload_bytes = 0

    def flush() -> None:
        nonlocal payload_bytes, count
        if payload:
            target.executemany_native(insert, payload)
            target.commit()
            for pending in payload:
                _digest_update(digest, pending)
            count += len(payload)
            payload.clear()
            payload_bytes = 0

    for source_row in cursor:
        row = tuple(source_row)
        row_bytes = _row_payload_size(row)
        if payload and (len(payload) >= batch_size or payload_bytes + row_bytes > payload_budget):
            flush()
        payload.append(row)
        payload_bytes += row_bytes
        if len(payload) >= batch_size or payload_bytes >= payload_budget:
            flush()
    flush()
    if count != item.rows or digest.hexdigest() != item.digest:
        raise ImportErrorSafe(f"FTS projection mismatch for table {table}")


def _set_auto_increment(target, item: TableInventory, rowid: str) -> None:
    metadata = target.execute_native(
        "SELECT EXTRA FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME=%s AND COLUMN_NAME=%s", (item.name, rowid),
    ).fetchone()
    if metadata is None:
        raise ImportErrorSafe(f"target rowid column is missing for table {item.name}")
    if "AUTO_INCREMENT" not in str(metadata[0] or "").upper():
        if item.sequence is not None:
            raise ImportErrorSafe(
                f"SQLite sequence cannot be restored because target rowid is not AUTO_INCREMENT in table {item.name}"
            )
        return
    maximum = 0
    if item.rows:
        maximum = int(target.execute_native(
            f"SELECT MAX({_ident(rowid)}) FROM {_ident(item.name)}"
        ).fetchone()[0] or 0)
    next_value = max(maximum, int(item.sequence or 0)) + 1
    target.execute_native(f"ALTER TABLE {_ident(item.name)} AUTO_INCREMENT={next_value}")
    actual = target.execute_native(
        "SELECT AUTO_INCREMENT FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (item.name,),
    ).fetchone()
    if actual is None or int(actual[0] or 0) != next_value:
        raise ImportErrorSafe(f"AUTO_INCREMENT verification failed for table {item.name}")


def _mysql_foreign_key_check(conn, tables: set[str]) -> None:
    rows = conn.execute_native(
        "SELECT TABLE_NAME,CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,"
        "REFERENCED_COLUMN_NAME,ORDINAL_POSITION FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
        "WHERE CONSTRAINT_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL "
        "ORDER BY TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION"
    ).fetchall()
    groups: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for table, constraint, column, parent, parent_column, _position in rows:
        table, parent = str(table), str(parent)
        if table not in tables or parent not in tables:
            continue
        groups.setdefault((table, str(constraint), parent), []).append(
            (str(column), str(parent_column)),
        )
    for (table, _constraint, parent), pairs in groups.items():
        nonnull = " AND ".join(f"c.{_ident(child)} IS NOT NULL" for child, _ in pairs)
        equal = " AND ".join(
            f"p.{_ident(parent_col)} <=> c.{_ident(child)}" for child, parent_col in pairs
        )
        row = conn.execute_native(
            f"SELECT 1 FROM {_ident(table)} c WHERE {nonnull} AND NOT EXISTS "
            f"(SELECT 1 FROM {_ident(parent)} p WHERE {equal}) LIMIT 1"
        ).fetchone()
        if row is not None:
            raise ImportErrorSafe(f"foreign-key verification failed for table {table}")


def _verify_table(source: sqlite3.Connection, target, item: TableInventory,
                  target_columns: list[str], target_rowid: str) -> None:
    source_count = int(source.execute(f"SELECT count(*) FROM {_ident(item.name)}").fetchone()[0])
    target_count = int(target.execute_native(f"SELECT count(*) FROM {_ident(item.name)}").fetchone()[0])
    if source_count != item.rows or target_count != item.rows:
        raise ImportErrorSafe(f"row count verification failed for table {item.name}")
    source_digest = _source_digest(
        source, item.name,
        list(_FTS_TABLES[item.name]) if item.name in _FTS_TABLES else list(item.source_columns),
        "rowid",
    )
    target_digest = _target_digest(target, item.name, target_columns, target_rowid)
    if source_digest != item.digest or target_digest != item.digest:
        raise ImportErrorSafe(f"row digest verification failed for table {item.name}")


def _check_fk(source: sqlite3.Connection) -> None:
    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ImportErrorSafe("SQLite snapshot failed integrity_check")
    if source.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ImportErrorSafe("SQLite snapshot contains foreign-key violations")


def _load_config(path: Path):
    from app.storage.mysql import MySQLConfig

    try:
        raw = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
        return MySQLConfig(**raw)
    except Exception as exc:
        raise ImportErrorSafe("a MySQL connection file is invalid or unreadable") from exc


def _same_database(left, right) -> bool:
    return (left.host.casefold(), left.port, left.database.casefold()) == (
        right.host.casefold(), right.port, right.database.casefold(),
    )


def _open_snapshot(path: Path, *, validate_integrity: bool = True) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ImportErrorSafe("SQLite snapshot path is not a regular file")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(resolved) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ImportErrorSafe("SQLite snapshot has non-empty WAL sidecars; checkpoint it before import")
    uri = resolved.as_uri() + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        _load_sqlite_vec_for_snapshot(conn)
        if validate_integrity:
            _check_fk(conn)
        return conn
    except Exception as exc:
        if isinstance(exc, ImportErrorSafe):
            raise
        raise ImportErrorSafe("could not open or verify the SQLite snapshot") from exc


_RECEIPT_FIELDS = {
    "format", "source_format", "source_size", "source_sha256",
    "integrity_check", "foreign_key_check",
}


def _snapshot_file_identity(path: Path) -> tuple[int, str]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size, after.st_mtime_ns, after.st_ino,
    ):
        raise ImportErrorSafe("SQLite snapshot changed while its validation receipt was checked")
    return int(after.st_size), digest.hexdigest()


def _read_validation_receipt(path: Path, source_path: Path) -> bool:
    if not path.exists():
        return False
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportErrorSafe("source validation receipt is invalid or unreadable") from exc
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ImportErrorSafe("source validation receipt has an unsupported format")
    size, fingerprint = _snapshot_file_identity(source_path)
    if (type(receipt["format"]) is not int or receipt["format"] != 1
            or receipt["source_format"] != "sqlite3"
            or type(receipt["source_size"]) is not int or receipt["source_size"] != size
            or not isinstance(receipt["source_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["source_sha256"])
            or receipt["source_sha256"] != fingerprint
            or receipt["integrity_check"] != "ok"
            or receipt["foreign_key_check"] != "ok"):
        raise ImportErrorSafe("source validation receipt does not match the SQLite snapshot")
    return True


def _write_validation_receipt(path: Path, source_path: Path) -> None:
    size, fingerprint = _snapshot_file_identity(source_path)
    receipt = {
        "format": 1, "source_format": "sqlite3", "source_size": size,
        "source_sha256": fingerprint, "integrity_check": "ok", "foreign_key_check": "ok",
    }
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    except FileExistsError as exc:
        raise ImportErrorSafe("source validation receipt already exists; validate it before reuse") from exc
    except OSError as exc:
        raise ImportErrorSafe("could not securely write source validation receipt") from exc


@contextmanager
def _temporary_import_wait_timeout(connection, seconds: int = 7200):
    """Extend only idle-session expiry during offline import, then restore it."""
    original = _set_import_wait_timeout(connection, seconds)
    try:
        yield
    except BaseException:
        _restore_timeout_preserving_failure(connection, original)
        raise
    else:
        _restore_timeout_preserving_failure(connection, original)


def _set_import_wait_timeout(connection, seconds: int = 7200) -> int:
    if hasattr(connection, "execute_native"):
        original = int(connection.execute_native("SELECT @@SESSION.wait_timeout").fetchone()[0])
        connection.execute_native("SET SESSION wait_timeout=%s", (seconds,))
    else:
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@SESSION.wait_timeout")
            original = int(cursor.fetchone()[0])
            cursor.execute("SET SESSION wait_timeout=%s", (seconds,))
    return original


def _restore_import_wait_timeout(connection, original: int) -> None:
    if hasattr(connection, "execute_native"):
        connection.execute_native("SET SESSION wait_timeout=%s", (original,))
    else:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION wait_timeout=%s", (original,))


def _discard_connection(connection) -> None:
    if hasattr(connection, "_broken"):
        connection._broken = True


def _restore_timeout_preserving_failure(connection, original: int) -> None:
    active_failure = sys.exc_info()[1]
    try:
        _restore_import_wait_timeout(connection, original)
    except Exception:
        _discard_connection(connection)
        if active_failure is None:
            raise


def _load_trigger_definitions(conn, expected_table_names: set[str]) -> list[dict[str, Any]]:
    triggers = _trigger_snapshot(conn)
    # bootstrap_mysql(install=False) has just verified the declared triggers;
    # extend that allowlist with the generated collision guards and native
    # shared-sync trigger names, then refuse any unmanaged trigger.
    expected = conn.execute_native(
        "SELECT object_name,table_name FROM lka_mysql_schema_objects "
        "WHERE object_type IN ('trigger','external_trigger') ORDER BY object_name"
    ).fetchall()
    expected_names = {str(row[0]) for row in expected}
    asset = json.loads((Path(__file__).parents[1] / "app/storage/mysql_schema_v1.json").read_text())
    for port in asset["ports"].get("unbounded_unique_text_hashes", []):
        expected_names.update(str(name) for name in port.get("guards", []))
    from app.domains.shared_sync_mysql import (
        manifest_data,
        manifest_v5_data,
        manifest_v6_data,
        manifest_v7_data,
    )

    manifests = (manifest_data(), manifest_v5_data(), manifest_v6_data(), manifest_v7_data())
    sync_logical = {str(item["name"]) for manifest in manifests for item in manifest["triggers"]}
    expected_names.difference_update(sync_logical)
    for manifest in manifests:
        expected_names.update(str(item["native_name"]) for item in manifest["triggers"])
    actual_names = {item["name"] for item in triggers}
    if actual_names != expected_names:
        raise ImportErrorSafe("target trigger inventory differs from verified schema metadata")
    if any(item["table"] not in expected_table_names | _MYSQL_FTS_TABLES for item in triggers):
        raise ImportErrorSafe("target has a trigger outside the reviewed application schema")
    return triggers


def _do_check(source: sqlite3.Connection, runtime_config, migration_config, *, batch_size: int):
    from app.storage.mysql import connect_mysql
    from app.storage.mysql_bootstrap import bootstrap_mysql

    with connect_mysql(runtime_config, read_only=True) as runtime:
        bootstrap_mysql(runtime, install=False)
        target_tables = _mysql_business_tables(runtime)
        rowids = _mysql_rowid_map(runtime, target_tables)
    # Source inventory hashes every source row. Do not hold a MySQL lease idle
    # while that potentially long local scan runs.
    inventory, excluded = _inventory_source(source, target_tables, rowids)
    vectors = _knowledge_vector_inventory(source)
    with connect_mysql(runtime_config, read_only=True) as runtime:
        bootstrap_mysql(runtime, install=False)
        if _mysql_business_tables(runtime) != target_tables or _mysql_rowid_map(runtime, target_tables) != rowids:
            raise ImportErrorSafe("target schema metadata changed during source inventory")
        _validate_source_target_columns(source, runtime, inventory, rowids)
        _validate_mysql_vector_table(runtime)
        _target_is_empty(runtime, target_tables)
    if not _same_database(runtime_config, migration_config):
        raise ImportErrorSafe("runtime and migration connection files must name the same MySQL database")
    with connect_mysql(migration_config, read_only=True) as migration:
        bootstrap_mysql(migration, install=False)
        _assert_fresh_import_state(migration)
        _load_trigger_definitions(migration, target_tables)
    return inventory, excluded, vectors


def _execute_import(source: sqlite3.Connection, runtime_config, migration_config,
                    inventory: list[TableInventory], knowledge_vectors: KnowledgeVectorInventory,
                    *, batch_size: int):
    from app.storage.mysql import connect_mysql
    from app.storage.mysql_bootstrap import bootstrap_mysql
    from app.storage.mysql_owner import MySQLCoordinatorOwner

    owner = MySQLCoordinatorOwner(migration_config)
    if not owner.acquire():
        raise ImportErrorSafe("a MySQL coordinator is active; stop it before import")
    migration = None
    migration_wait_timeout = None
    try:
        migration = connect_mysql(migration_config)
        migration_wait_timeout = _set_import_wait_timeout(migration)
        triggers: list[dict[str, Any]] = []
        dropped = False
        import_id: str | None = None
        fingerprint = _source_fingerprint(inventory, knowledge_vectors)
        try:
            # Re-verify target schema while holding the coordinator lock.
            phase = "schema_verify"
            bootstrap_mysql(migration, install=False)
            _assert_fresh_import_state(migration)
            target_tables = _mysql_business_tables(migration)
            rowids = _mysql_rowid_map(migration, target_tables)
            _validate_mysql_vector_table(migration)
            _target_is_empty(migration, target_tables)
            _validate_source_target_columns(source, migration, inventory, rowids)
            if _knowledge_vector_inventory(source) != knowledge_vectors:
                raise ImportErrorSafe("SQLite knowledge vectors changed after source check")
            triggers = _load_trigger_definitions(migration, target_tables)
            with connect_mysql(runtime_config) as target, _temporary_import_wait_timeout(target):
                bootstrap_mysql(target, install=False, allow_incomplete_import=True)
                _target_is_empty(target, target_tables)
                _validate_source_target_columns(source, target, inventory, rowids)
                _validate_mysql_vector_table(target)
                _assert_fresh_import_state(migration, create=True)
                import_id = uuid4().hex
                phase = "marker_in_progress"
                _set_import_status(migration, import_id, "in_progress", fingerprint)
                original_sql_mode = target.execute_native("SELECT @@SESSION.sql_mode").fetchone()[0]
                mode_set = set(filter(None, str(original_sql_mode).split(",")))
                mode_set.add("NO_AUTO_VALUE_ON_ZERO")
                target.execute_native("SET SESSION sql_mode=%s", (",".join(sorted(mode_set)),))
                dropped = True
                phase = "drop_triggers"
                _drop_triggers(migration, triggers)
                # The source snapshot has already passed foreign_key_check.
                # This session-only switch supports valid self/cyclic refs.
                target.execute_native("SET SESSION foreign_key_checks=0")
                try:
                    item_by_name = {item.name: item for item in inventory}
                    source_tables = set(item_by_name) - _MYSQL_FTS_TABLES
                    ordered = _fk_order(source, source_tables)
                    for table in ordered:
                        phase = f"copy_table:{table}"
                        _copy_ordinary_table(source, target, item_by_name[table], batch_size=batch_size)
                    for name in sorted(_FTS_TABLES):
                        phase = f"copy_fts:{name}"
                        _copy_fts(source, target, item_by_name[name], batch_size=batch_size)
                    phase = "copy_knowledge_vectors"
                    _copy_knowledge_vectors(
                        source, target, knowledge_vectors, batch_size=batch_size,
                    )
                    # Runtime credentials are intentionally DML-only. The
                    # migration connection owns reviewed schema/sequence DDL.
                    migration.execute_native("SET SESSION information_schema_stats_expiry=0")
                    for item in inventory:
                        phase = f"sequence:{item.name}"
                        proxy = "rowid" if item.name in _FTS_TABLES else rowids[item.name][0]
                        _set_auto_increment(migration, item, proxy)
                finally:
                    target.execute_native("SET SESSION foreign_key_checks=1")
                    target.execute_native("SET SESSION sql_mode=%s", (original_sql_mode,))
                phase = "foreign_key_verify"
                _mysql_foreign_key_check(target, target_tables)

                # Triggers were disabled, so the copied shared-sync state is the
                # exact source state (including journal ids and bootstrap markers).
                item_by_name = {item.name: item for item in inventory}
                for table in ("shared_sync_journal", "shared_sync_heads", "shared_sync_meta", "shared_sync_proposals"):
                    if table not in item_by_name:
                        raise ImportErrorSafe("shared-sync source tables are missing")

                phase = "restore_triggers"
                _restore_triggers(migration, triggers)
                dropped = False

                # Re-verify trigger definitions and all source rows after restore.
                phase = "schema_and_trigger_verify"
                bootstrap_mysql(migration, install=False, allow_incomplete_import=True)
                _verify_restored_triggers(migration, triggers)
                for item in inventory:
                    phase = f"row_verify:{item.name}"
                    if item.name in _FTS_TABLES:
                        fields = ["rowid", *_FTS_TABLES[item.name]]
                        _verify_table(source, target, item, fields, "rowid")
                    else:
                        proxy, kind = rowids[item.name]
                        _verify_table_with_rowid(source, target, item, list(item.source_columns), proxy, kind)
                phase = "row_verify:knowledge_vector_blobs"
                _verify_knowledge_vectors(target, knowledge_vectors)
                phase = "sequence_verify"
                _verify_source_sequences(source, inventory)
                phase = "marker_verified"
                _set_import_status(migration, import_id, "verified")
                phase = "final_bootstrap"
                bootstrap_mysql(migration, install=False)

            # Standard projections are checked against the source above. This
            # routine is intentionally not called: it would alter source-equal
            # FTS bytes/text and lose a streaming equality guarantee.
            return {
                "tables": {item.name: {"rows": item.rows, "sha256": item.digest} for item in inventory},
                "knowledge_vectors": {
                    "rows": knowledge_vectors.rows, "sha256": knowledge_vectors.digest,
                    "source_tables": list(knowledge_vectors.source_tables),
                    "empty_source_tables": list(knowledge_vectors.empty_source_tables),
                },
            }
        except BaseException as failure:
            if dropped:
                try:
                    _restore_triggers(migration, triggers)
                    dropped = False
                except Exception:  # noqa: BLE001,S110 - continue fail-closed state recording.
                    pass
            if import_id is not None:
                try:
                    _set_import_status(migration, import_id, "failed")
                except Exception:  # noqa: BLE001,S110 - keep the original import failure.
                    pass
                if isinstance(failure, Exception):
                    errno = getattr(failure, "mysql_errno", None)
                    errno_text = str(errno) if isinstance(errno, int) else "none"
                    reason = str(failure) if isinstance(failure, ImportErrorSafe) else (
                        f"database or runtime exception {type(failure).__name__}"
                    )
                    raise ImportErrorSafe(
                        f"{reason}; phase={phase}; mysql_errno={errno_text}; "
                        "target is incomplete and must not be started"
                    ) from None
                raise
            raise
    finally:
        try:
            if migration is not None and not migration.closed:
                try:
                    if migration_wait_timeout is not None:
                        _restore_timeout_preserving_failure(migration, migration_wait_timeout)
                finally:
                    migration.close()
        finally:
            owner.release()


def _verify_restored_triggers(conn, original: list[dict[str, Any]]) -> None:
    actual = _trigger_snapshot(conn)
    by_name = {item["name"]: item for item in actual}
    if set(by_name) != {item["name"] for item in original}:
        raise ImportErrorSafe("trigger inventory changed during restoration")
    for expected in original:
        found = by_name[expected["name"]]
        for key in (
            "table", "timing", "event", "action_order", "sql_mode", "definer",
            "character_set_client", "collation_connection", "database_collation", "create_sql",
        ):
            if found[key] != expected[key]:
                raise ImportErrorSafe("trigger definition or action order changed during restoration")


def _verify_table_with_rowid(source, target, item, target_columns, target_rowid, kind):
    source_count = int(source.execute(f"SELECT count(*) FROM {_ident(item.name)}").fetchone()[0])
    target_count = int(target.execute_native(f"SELECT count(*) FROM {_ident(item.name)}").fetchone()[0])
    if source_count != item.rows or target_count != item.rows:
        raise ImportErrorSafe(f"row count verification failed for table {item.name}")
    columns = list(item.source_columns)
    source_rowid = _source_rowid_name(
        columns, integer_pk=target_rowid if kind == "integer_primary_key" else None,
    )
    if kind == "invisible_auto_increment":
        # The proxy is the target's rowid expression and is compared in the
        # first digest field against the source SQLite rowid.
        # Compare source explicit values and rowid as an extra field.
        source_digest = hashlib.sha256()
        cursor = source.execute(
            f"SELECT {_ident(source_rowid)},{','.join(_ident(c) for c in item.source_columns)} "
            f"FROM {_ident(item.name)} ORDER BY {_ident(source_rowid)}"
        )
        for row in cursor:
            _digest_update(source_digest, tuple(row))
    else:
        source_digest = hashlib.sha256()
        cursor = source.execute(
            f"SELECT {_ident(source_rowid)},{','.join(_ident(c) for c in columns)} "
            f"FROM {_ident(item.name)} ORDER BY {_ident(source_rowid)}"
        )
        for row in cursor:
            _digest_update(source_digest, tuple(row))
    target_digest = _target_digest(target, item.name, [target_rowid, *item.source_columns], target_rowid)
    if target_digest != source_digest.hexdigest():
        raise ImportErrorSafe(f"row digest verification failed for table {item.name}")


def _report(inventory: list[TableInventory], excluded: list[str], result: dict[str, Any] | None,
            mode: str, source_absent_target_tables: list[str] | None = None,
            knowledge_vectors: KnowledgeVectorInventory | None = None) -> dict[str, Any]:
    tables = [{"name": item.name, "rows": item.rows, "sha256": item.digest} for item in inventory]
    vectors = knowledge_vectors or KnowledgeVectorInventory((), 0, hashlib.sha256().hexdigest())
    return {"format": 1, "mode": mode, "tables": tables, "excluded_sqlite_objects": excluded,
            "source_absent_target_tables": sorted(source_absent_target_tables or []),
            "knowledge_vectors": {
                "rows": vectors.rows, "sha256": vectors.digest,
                "source_tables": list(vectors.source_tables),
                "empty_source_tables": list(vectors.empty_source_tables),
            },
            "external_file_snapshot_required": True, "result": result or {}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-snapshot", type=Path, required=True)
    parser.add_argument("--snapshot-confirmed", action="store_true",
                        help="acknowledge the input is a stopped-service consistent backup copy")
    parser.add_argument("--runtime-connection-file", type=Path, required=True)
    parser.add_argument("--migration-connection-file", type=Path, required=True)
    parser.add_argument(
        "--source-validation-receipt", type=Path,
        help="private receipt to reuse successful SQLite integrity/FK validation for this exact snapshot",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args(argv)
    if not args.snapshot_confirmed:
        parser.error("--snapshot-confirmed is required; live SQLite databases are not supported")
    if not 1 <= args.batch_size <= 2000:
        parser.error("--batch-size must be between 1 and 2000")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    phase = "config_load"
    try:
        runtime = _load_config(args.runtime_connection_file)
        migration = _load_config(args.migration_connection_file)
        if not _same_database(runtime, migration):
            raise ImportErrorSafe("runtime and migration connection files must name the same MySQL database")
        phase = "snapshot_open"
        snapshot_path = args.sqlite_snapshot.expanduser().resolve(strict=True)
        receipt_path = args.source_validation_receipt.expanduser().resolve() if args.source_validation_receipt else None
        if receipt_path is not None and receipt_path in {
            snapshot_path, args.runtime_connection_file.expanduser().resolve(),
            args.migration_connection_file.expanduser().resolve(),
        }:
            raise ImportErrorSafe("source validation receipt must use a separate private file")
        receipt_reused = _read_validation_receipt(receipt_path, snapshot_path) if receipt_path else False
        source = _open_snapshot(snapshot_path, validate_integrity=not receipt_reused)
        if receipt_path is not None and not receipt_reused:
            _write_validation_receipt(receipt_path, snapshot_path)
        try:
            phase = "target_check"
            inventory, excluded, knowledge_vectors = _do_check(
                source, runtime, migration, batch_size=args.batch_size,
            )
            present = {item.name for item in inventory if item.name not in _FTS_TABLES}
            if knowledge_vectors.source_tables:
                present.add("knowledge_vector_blobs")
            # The check has already validated that every source table is known.
            # Only empty optional target tables may be absent from a local source.
            from app.storage.mysql import connect_mysql

            with connect_mysql(runtime, read_only=True) as target:
                target_tables = _mysql_business_tables(target)
            absent = sorted(target_tables - present)
            result = None
            selected_mode = "check_only"
            if args.execute:
                phase = "execute"
                result = _execute_import(
                    source, runtime, migration, inventory, knowledge_vectors,
                    batch_size=args.batch_size,
                )
                selected_mode = "execute"
            report = _report(
                inventory, excluded, result, selected_mode, absent, knowledge_vectors,
            )
        finally:
            source.close()
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.report_file:
            report_path = args.report_file.expanduser().resolve()
            protected = {
                snapshot_path,
                args.runtime_connection_file.expanduser().resolve(),
                args.migration_connection_file.expanduser().resolve(),
            }
            if receipt_path is not None:
                protected.add(receipt_path)
            if report_path in protected:
                raise ImportErrorSafe("report output must not overwrite an input file")
            with report_path.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        print(encoded, end="")
        return 0
    except ImportErrorSafe as exc:
        print(f"SQLite/MySQL import refused: phase={phase}; reason={exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never print vendor details or sensitive paths.
        errno = getattr(exc, "mysql_errno", None)
        errno_text = str(errno) if isinstance(errno, int) else "none"
        print(
            f"SQLite/MySQL import failed: phase={phase}; error_type={type(exc).__name__}; "
            f"mysql_errno={errno_text}", file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
