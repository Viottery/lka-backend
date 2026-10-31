"""Versioned MySQL migration for signed 64-bit background watermarks."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.storage.mysql import MySQLConnection
from app.storage.mysql_schema import MySQLSchemaError

_ASSET_PATH = Path(__file__).with_name("mysql_schema_v10.json")
_EXPECTED_COLUMNS = {
    ("background_watermark_heads", "watermark_order"): False,
    ("background_pending_watermarks", "watermark_order"): True,
}


@lru_cache(maxsize=1)
def _load_v10_asset() -> tuple[dict[str, Any], bytes]:
    try:
        raw = _ASSET_PATH.read_bytes()
        asset = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MySQLSchemaError("watermark V10 migration asset is unavailable or invalid") from exc
    expected = {
        "format": 1,
        "version": 10,
        "migration_name": "background_watermark_order_signed64",
        "columns": [
            {"table": "background_watermark_heads", "column": "watermark_order",
             "nullable": False, "mysql_type": "BIGINT"},
            {"table": "background_pending_watermarks", "column": "watermark_order",
             "nullable": True, "mysql_type": "BIGINT"},
        ],
    }
    if asset != expected:
        raise MySQLSchemaError("watermark V10 migration asset differs from reviewed source")
    return asset, raw


def _database(conn: MySQLConnection) -> str:
    database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
    if not database:
        raise MySQLSchemaError("watermark V10 migration requires database identity")
    return str(database)


def _migration_record(conn: MySQLConnection):
    return conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (10,),
    ).fetchone()


def _columns(conn: MySQLConnection) -> dict[tuple[str, str], dict[str, str | None]]:
    rows = conn.execute_native(
        "SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA,COLUMN_COMMENT "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME IN (%s,%s) AND COLUMN_NAME=%s",
        ("background_watermark_heads", "background_pending_watermarks", "watermark_order"),
    ).fetchall()
    return {
        (str(row[0]), str(row[1])): {
            "data_type": str(row[2]).lower(),
            "column_type": str(row[3]).lower(),
            "nullable": str(row[4]).upper() == "YES",
            "default": row[5],
            "extra": str(row[6] or "").strip().lower(),
            "comment": str(row[7] or ""),
        }
        for row in rows
    }


def _validate_columns(conn: MySQLConnection, *, allow_int: bool) -> dict[tuple[str, str], dict[str, str | None]]:
    actual = _columns(conn)
    for key, nullable in _EXPECTED_COLUMNS.items():
        column = actual.get(key)
        if column is None:
            raise MySQLSchemaError(f"watermark V10 required column {key[0]}.{key[1]} is missing")
        accepted_types = {"bigint"}
        if allow_int:
            accepted_types.add("int")
        if (column["data_type"] not in accepted_types
                or not re.fullmatch(r"(?:bigint|int)(?:\(\d+\))?", column["column_type"])
                or column["nullable"] is not nullable
                or column["default"] is not None
                or column["extra"]
                or column["comment"]):
            raise MySQLSchemaError(f"watermark V10 found an unrecognized definition for {key[0]}.{key[1]}")
    return actual


def _verify_record(conn: MySQLConnection, asset: dict[str, Any], raw: bytes) -> None:
    row = _migration_record(conn)
    checksum = hashlib.sha256(raw).hexdigest()
    if row is None or row[0] != asset["migration_name"] or row[1] != checksum:
        raise MySQLSchemaError("watermark V10 migration checksum/name mismatch or missing")


def install_watermark_order_v10(conn: MySQLConnection, *, install: bool = False) -> None:
    """Install V10 under explicit migration authority or verify it read-only.

    This migration widens only two signed integer columns. MySQL's ALTER TABLE
    preserves their existing values; the V10 ledger entry is written only after
    both physical definitions are verified. Runtime mode performs no DDL.
    """
    if conn.in_transaction:
        raise MySQLSchemaError("watermark V10 migration cannot run inside a business transaction")
    asset, raw = _load_v10_asset()
    version = int(asset["version"])
    name = str(asset["migration_name"])
    checksum = hashlib.sha256(raw).hexdigest()

    if not install:
        previous = conn.execute_native(
            "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (9,),
        ).fetchone()
        if previous is None:
            raise MySQLSchemaError("watermark V10 requires schema V9; run the schema migration")
        _verify_record(conn, asset, raw)
        _validate_columns(conn, allow_int=False)
        return

    database = _database(conn)
    lock_name = "lka-watermark-v10-" + hashlib.sha256(database.encode()).hexdigest()[:32]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLSchemaError("timed out acquiring watermark V10 migration lock")
    try:
        previous = conn.execute_native(
            "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (9,),
        ).fetchone()
        if previous is None:
            raise MySQLSchemaError("watermark V10 requires schema V9; run the schema migration")
        current = _migration_record(conn)
        if current is not None:
            _verify_record(conn, asset, raw)
            _validate_columns(conn, allow_int=False)
            return

        actual = _validate_columns(conn, allow_int=True)
        for (table, column), nullable in _EXPECTED_COLUMNS.items():
            if actual[(table, column)]["data_type"] == "bigint":
                continue
            nullability = "NULL" if nullable else "NOT NULL"
            conn.execute_native(
                f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` BIGINT {nullability}",
            )
        _validate_columns(conn, allow_int=False)
        conn.execute_native(
            "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
            (version, name, checksum),
        )
        conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()
