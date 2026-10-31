"""Versioned native storage for embeddings in MySQL knowledge indexes.

The vector payload table is deliberately native-only: access goes through
``execute_native`` in the MySQL semantic index and the migration verifier.
Runtime startup verifies it but never creates or alters it.
"""
from __future__ import annotations

import hashlib
import json
import re

from app.storage.mysql import MySQLConnection
from app.storage.mysql_schema import MySQLSchemaError

_VERSION = 11
_NAME = "knowledge_vector_blobs"
_TABLE = "knowledge_vector_blobs"
_DDL_TEMPLATE = """CREATE TABLE `knowledge_vector_blobs` (
    `vector_id` {column_type} NOT NULL,
    `embedding` LONGBLOB NOT NULL,
    PRIMARY KEY (`vector_id`),
    CONSTRAINT `fk_knowledge_vector_blobs_embedding`
        FOREIGN KEY (`vector_id`) REFERENCES `knowledge_embedding_records` (`vector_id`)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin"""
_CHECKSUM = hashlib.sha256(json.dumps(
    {"version": _VERSION, "name": _NAME, "ddl": _DDL_TEMPLATE},
    sort_keys=True, separators=(",", ":"),
).encode("utf-8")).hexdigest()


def _table_exists(conn: MySQLConnection, table: str) -> bool:
    return conn.execute_native(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (table,),
    ).fetchone() is not None


def _source_vector_id(conn: MySQLConnection) -> dict[str, str | None]:
    row = conn.execute_native(
        "SELECT DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,COLUMN_KEY,EXTRA "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='knowledge_embedding_records' AND COLUMN_NAME='vector_id'",
    ).fetchone()
    if row is None:
        raise MySQLSchemaError("knowledge vector migration requires knowledge_embedding_records.vector_id")
    result = {
        "data_type": str(row[0]).lower(),
        "column_type": str(row[1]).lower(),
        "nullable": str(row[2]).upper() == "YES",
        "key": str(row[3]).upper(),
        "extra": str(row[4] or "").lower(),
    }
    # Match the physical source key exactly: deployed MySQL schema conversion
    # may use INT or BIGINT (and may preserve unsignedness).
    if (result["data_type"] not in {"int", "bigint"}
            or not re.fullmatch(r"(?:int|bigint)(?:\(\d+\))?(?: unsigned)?", result["column_type"])
            or result["nullable"] or result["key"] != "PRI" or "auto_increment" not in result["extra"]):
        raise MySQLSchemaError("unrecognized knowledge_embedding_records.vector_id definition")
    return result


def _verify_table(conn: MySQLConnection) -> None:
    source = _source_vector_id(conn)
    table_row = conn.execute_native(
        "SELECT ENGINE FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (_TABLE,),
    ).fetchone()
    if table_row is None or str(table_row[0]).lower() != "innodb":
        raise MySQLSchemaError("knowledge vector blob table is missing or is not InnoDB")
    rows = conn.execute_native(
        "SELECT COLUMN_NAME,DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,COLUMN_KEY,COLUMN_DEFAULT,EXTRA "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (_TABLE,),
    ).fetchall()
    columns = {str(row[0]): row for row in rows}
    if set(columns) != {"vector_id", "embedding"}:
        raise MySQLSchemaError("knowledge vector blob table has unexpected columns")
    vector_id = columns["vector_id"]
    if (str(vector_id[1]).lower() != source["data_type"]
            or str(vector_id[2]).lower() != source["column_type"]
            or str(vector_id[3]).upper() != "NO" or str(vector_id[4]).upper() != "PRI"
            or vector_id[5] is not None or str(vector_id[6] or "")):
        raise MySQLSchemaError("knowledge vector blob FK column differs from its referenced key")
    embedding = columns["embedding"]
    if (str(embedding[1]).lower() != "longblob" or str(embedding[2]).lower() != "longblob"
            or str(embedding[3]).upper() != "NO" or str(embedding[4]).upper()
            or embedding[5] is not None or str(embedding[6] or "")):
        raise MySQLSchemaError("knowledge vector blob payload column has an unrecognized definition")
    foreign_keys = conn.execute_native(
        "SELECT k.COLUMN_NAME,k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME,r.DELETE_RULE "
        "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS r "
        "ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.TABLE_NAME=k.TABLE_NAME "
        "AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE k.CONSTRAINT_SCHEMA=DATABASE() "
        "AND k.TABLE_NAME=%s AND k.REFERENCED_TABLE_NAME IS NOT NULL",
        (_TABLE,),
    ).fetchall()
    if len(foreign_keys) != 1 or tuple(str(item) for item in foreign_keys[0]) != (
        "vector_id", "knowledge_embedding_records", "vector_id", "CASCADE",
    ):
        raise MySQLSchemaError("knowledge vector blob FK does not cascade from embedding metadata")


def verify_knowledge_vector_blobs_v11(conn: MySQLConnection) -> None:
    """Verify v10 ancestry, v11 checksum, and exact table structure without DDL."""
    if conn.in_transaction:
        raise MySQLSchemaError("knowledge vector verification cannot run inside a transaction")
    previous = conn.execute_native(
        "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (10,),
    ).fetchone()
    if previous is None:
        raise MySQLSchemaError("knowledge vector storage requires MySQL schema V10")
    record = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_VERSION,),
    ).fetchone()
    if record is None or record[0] != _NAME or record[1] != _CHECKSUM:
        raise MySQLSchemaError("knowledge vector V11 migration checksum/name mismatch or missing")
    _verify_table(conn)


def install_knowledge_vector_blobs_v11(conn: MySQLConnection, *, install: bool = False) -> None:
    """Install V11 under explicit migration authority; runtime mode is verify-only."""
    if conn.in_transaction:
        raise MySQLSchemaError("knowledge vector migration cannot run inside a transaction")
    previous = conn.execute_native(
        "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (10,),
    ).fetchone()
    if previous is None:
        raise MySQLSchemaError("knowledge vector V11 requires schema V10")
    if not install:
        verify_knowledge_vector_blobs_v11(conn)
        return
    database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
    if not database:
        raise MySQLSchemaError("knowledge vector V11 migration requires database identity")
    lock_name = "lka-knowledge-vectors-v11-" + hashlib.sha256(str(database).encode()).hexdigest()[:32]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLSchemaError("timed out acquiring knowledge vector V11 migration lock")
    try:
        record = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_VERSION,),
        ).fetchone()
        if record is not None:
            verify_knowledge_vector_blobs_v11(conn)
            return
        if _table_exists(conn, _TABLE):
            raise MySQLSchemaError("unversioned knowledge_vector_blobs table already exists")
        _source_vector_id(conn)
        conn.execute_native(_DDL_TEMPLATE.format(column_type=_source_vector_id(conn)["column_type"].upper()))
        _verify_table(conn)
        conn.execute_native(
            "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
            (_VERSION, _NAME, _CHECKSUM),
        )
        conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()
