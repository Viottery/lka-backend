"""SQLite changes() compatibility checks against the opt-in MySQL lab."""
from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from app.storage.mysql import MySQLDialectError, connect_mysql
from tests.test_mysql_owner_live import _config


def test_changes_tracks_last_logical_dml_across_reads_and_transactions():
    config = _config()
    table = "lka_test_changes_" + uuid4().hex[:16]
    with connect_mysql(config) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute_native(
            f"CREATE TABLE `{table}` (id INT PRIMARY KEY, value INT NOT NULL) ENGINE=InnoDB"
        )
        try:
            inserted = conn.execute(f"INSERT INTO `{table}`(id,value) VALUES(?,?)", (1, 10))
            assert inserted.rowcount == 1
            assert conn.execute("SELECT changes() AS affected").fetchone()["affected"] == 1
            conn.execute("SELECT 1")
            conn.commit()
            assert conn.execute("SELECT changes()").fetchone()[0] == 1

            updated = conn.execute(f"UPDATE `{table}` SET value=? WHERE id=?", (11, 1))
            assert updated.rowcount == 1
            assert conn.execute("SELECT changes()").fetchone()[0] == 1
            native = conn.execute_native(f"UPDATE `{table}` SET value=%s WHERE id=%s", (12, 1))
            assert native.rowcount == 1
            assert conn.execute("SELECT changes()").fetchone()[0] == 1
            missing = conn.execute(f"UPDATE `{table}` SET value=? WHERE id=?", (12, 999))
            assert missing.rowcount == 0
            assert conn.execute("SELECT changes()").fetchone()[0] == 0

            many = conn.executemany(
                f"UPDATE `{table}` SET value=? WHERE id=?", [(13, 1), (14, 999)],
            )
            assert many.rowcount == 1  # executemany keeps aggregate cursor.rowcount
            assert conn.execute("SELECT changes() AS last_update").fetchone()["last_update"] == 0
            # Reads do not replace the last individual DML result.
            conn.execute("SELECT value FROM `" + table + "` WHERE id=1").fetchone()
            assert conn.execute("SELECT changes()").fetchone()[0] == 0
            with pytest.raises(MySQLDialectError, match="does not accept bind parameters"):
                conn.execute("SELECT changes()", (1,))
        finally:
            conn.rollback()
            conn.execute_native(f"DROP TABLE `{table}`")
