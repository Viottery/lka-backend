from __future__ import annotations

import hashlib

import pytest

from app.storage.mysql_schema import MySQLSchemaError
from app.storage.mysql_watermarks import (
    _EXPECTED_COLUMNS,
    _load_v10_asset,
    install_watermark_order_v10,
)


class _Cursor:
    def __init__(self, row=None, rows=()):
        self._row = row
        self._rows = list(rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _Connection:
    in_transaction = False

    def __init__(self, *, data_type="int", migration_record=None):
        self.columns = {
            key: {
                "data_type": data_type,
                "column_type": data_type,
                "nullable": nullable,
                "default": None,
                "extra": "",
                "comment": "",
            }
            for key, nullable in _EXPECTED_COLUMNS.items()
        }
        self.migration_record = migration_record
        self.statements = []
        self.inserts = 0
        self.commits = 0
        self.released_locks = 0
        self._pool = type("Pool", (), {"config": type("Config", (), {"database": "lka_test_watermarks"})()})()

    def execute_native(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if "GET_LOCK" in statement:
            return _Cursor((1,))
        if "RELEASE_LOCK" in statement:
            self.released_locks += 1
            return _Cursor((1,))
        if "INFORMATION_SCHEMA.COLUMNS" in statement:
            rows = [
                (table, column, value["data_type"], value["column_type"],
                 "YES" if value["nullable"] else "NO", value["default"], value["extra"], value["comment"])
                for (table, column), value in self.columns.items()
            ]
            return _Cursor(rows=rows)
        if "SELECT 1 FROM `lka_schema_migrations`" in statement:
            return _Cursor((1,) if parameters == (9,) else None)
        if "SELECT name,checksum FROM `lka_schema_migrations`" in statement:
            return _Cursor(self.migration_record if parameters == (10,) else None)
        if statement.startswith("ALTER TABLE"):
            table = "background_watermark_heads" if "background_watermark_heads" in statement else "background_pending_watermarks"
            self.columns[(table, "watermark_order")]["data_type"] = "bigint"
            self.columns[(table, "watermark_order")]["column_type"] = "bigint"
            return _Cursor()
        if statement.startswith("INSERT INTO `lka_schema_migrations`"):
            self.inserts += 1
            self.migration_record = (parameters[1], parameters[2])
            return _Cursor()
        raise AssertionError(f"unexpected native SQL operation: {statement.split(None, 1)[0]}")

    def commit(self):
        self.commits += 1


def test_v10_asset_preserves_signed64_and_nullability_contract():
    asset, raw = _load_v10_asset()
    assert asset["version"] == 10
    assert hashlib.sha256(raw).hexdigest()
    assert asset["columns"] == [
        {"table": "background_watermark_heads", "column": "watermark_order",
         "nullable": False, "mysql_type": "BIGINT"},
        {"table": "background_pending_watermarks", "column": "watermark_order",
         "nullable": True, "mysql_type": "BIGINT"},
    ]


def test_install_widens_only_target_columns_and_is_idempotent():
    connection = _Connection()
    install_watermark_order_v10(connection, install=True)
    assert [statement for statement, _ in connection.statements if statement.startswith("ALTER TABLE")] == [
        "ALTER TABLE `background_watermark_heads` MODIFY COLUMN `watermark_order` BIGINT NOT NULL",
        "ALTER TABLE `background_pending_watermarks` MODIFY COLUMN `watermark_order` BIGINT NULL",
    ]
    assert connection.columns[("background_watermark_heads", "watermark_order")]["nullable"] is False
    assert connection.columns[("background_pending_watermarks", "watermark_order")]["nullable"] is True
    assert connection.inserts == 1 and connection.commits == 1
    assert connection.released_locks == 1

    install_watermark_order_v10(connection, install=True)
    assert len([statement for statement, _ in connection.statements if statement.startswith("ALTER TABLE")]) == 2
    assert connection.inserts == 1 and connection.released_locks == 2


def test_runtime_verification_is_read_only_and_requires_v10_record():
    asset, raw = _load_v10_asset()
    checksum = hashlib.sha256(raw).hexdigest()
    connection = _Connection(data_type="bigint", migration_record=(asset["migration_name"], checksum))
    install_watermark_order_v10(connection, install=False)
    assert not any(statement.startswith(("ALTER TABLE", "INSERT "))
                   for statement, _ in connection.statements)
    assert not any("GET_LOCK" in statement or "RELEASE_LOCK" in statement
                   for statement, _ in connection.statements)

    missing = _Connection(data_type="bigint")
    with pytest.raises(MySQLSchemaError, match="checksum/name mismatch or missing"):
        install_watermark_order_v10(missing, install=False)
    assert not any(statement.startswith("ALTER TABLE") for statement, _ in missing.statements)


@pytest.mark.parametrize("field,value", [
    ("column_type", "int unsigned"),
    ("nullable", True),
    ("default", "0"),
    ("extra", "generated stored"),
    ("comment", "manual schema change"),
])
def test_install_fails_closed_on_unrecognized_existing_column(field, value):
    connection = _Connection()
    connection.columns[("background_watermark_heads", "watermark_order")][field] = value
    with pytest.raises(MySQLSchemaError, match="unrecognized definition"):
        install_watermark_order_v10(connection, install=True)
    assert not any(statement.startswith("ALTER TABLE") for statement, _ in connection.statements)
    assert connection.inserts == 0
    assert connection.released_locks == 1


def test_preexisting_correct_bigints_are_recorded_without_ddl():
    connection = _Connection(data_type="bigint")
    install_watermark_order_v10(connection, install=True)
    assert not any(statement.startswith("ALTER TABLE") for statement, _ in connection.statements)
    assert connection.inserts == 1 and connection.commits == 1
