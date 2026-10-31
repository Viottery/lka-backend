"""InnoDB integration: concurrency is verified while transactions overlap."""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.storage.mysql import (
    MySQLDeadlockError,
    MySQLLeaseInvalidatedError,
    MySQLLockError,
    MySQLPoolTimeout,
    close_mysql_pools,
    connect_mysql,
)
from app.storage.sqlite_retry import is_sqlite_busy
from tests.test_mysql_owner_live import _config


@pytest.fixture
def engine_table():
    config = _config()
    migration_file = os.environ.get("LKA_MYSQL_TEST_MIGRATION_CONFIG")
    if not migration_file:
        pytest.skip("separate DDL credential required for synthetic transaction tables")
    migration = replace(config, **json.loads(Path(migration_file).read_text()))
    assert (migration.host, migration.port, migration.database) == (config.host, config.port, config.database)
    table = "lka_test_transactions_" + uuid4().hex[:16]
    with connect_mysql(migration) as conn:
        conn.execute_native(f"CREATE TABLE `{table}` (id INT PRIMARY KEY,value INT NOT NULL) ENGINE=InnoDB")
        conn.execute_native(f"INSERT INTO `{table}` VALUES(1,0),(2,0)")
    try:
        yield config, table
    finally:
        with connect_mysql(migration) as conn:
            conn.execute_native(f"DROP TABLE `{table}`")


def test_different_rows_can_hold_write_transactions_concurrently(engine_table):
    config, table = engine_table
    barrier = threading.Barrier(2, timeout=3)
    def write(row_id):
        with connect_mysql(config) as conn:
            conn.execute_native(f"UPDATE `{table}` SET value=value+1 WHERE id=%s", (row_id,))
            barrier.wait()  # a global writer FIFO would deadlock at this barrier
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(write, (1, 2)))
    with connect_mysql(config) as conn:
        assert list(conn.execute_native(f"SELECT value FROM `{table}` ORDER BY id").fetchall()[0]) == [1]
        assert conn.execute_native(f"SELECT SUM(value) FROM `{table}`").fetchone()[0] == 2


def test_deadlock_has_native_classification_and_rolls_back_one_transaction(engine_table):
    config, table = engine_table
    barrier = threading.Barrier(2, timeout=3)
    def write(order):
        try:
            with connect_mysql(config) as conn:
                conn.execute_native(f"UPDATE `{table}` SET value=value+1 WHERE id=%s", (order[0],))
                barrier.wait()
                conn.execute_native(f"UPDATE `{table}` SET value=value+1 WHERE id=%s", (order[1],))
            return "committed"
        except MySQLDeadlockError as exc:
            assert exc.mysql_errno == 1213
            assert not is_sqlite_busy(exc)
            return "deadlock"
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(write, ((1, 2), (2, 1))))
    assert sorted(outcomes) == ["committed", "deadlock"]
    with connect_mysql(config) as conn:
        assert [row[0] for row in conn.execute_native(f"SELECT value FROM `{table}` ORDER BY id")] == [1, 1]


def test_lock_wait_is_bounded_and_failed_transaction_does_not_leak(engine_table):
    base, table = engine_table
    config = replace(base, lock_wait_seconds=1)
    blocker = connect_mysql(base)
    try:
        blocker.execute_native(f"UPDATE `{table}` SET value=7 WHERE id=1")
        started = time.monotonic()
        with pytest.raises(MySQLLockError) as failure, connect_mysql(config) as waiter:
            waiter.execute_native(f"UPDATE `{table}` SET value=9 WHERE id=1")
        assert failure.value.mysql_errno == 1205
        assert not is_sqlite_busy(failure.value)
        assert time.monotonic() - started < 3
    finally:
        blocker.close()  # rollback 7; never return an open transaction to pool
        close_mysql_pools(config)
    with connect_mysql(base) as conn:
        assert conn.execute_native(f"SELECT value FROM `{table}` WHERE id=1").fetchone()[0] == 0


def test_caught_real_deadlock_cannot_resume_in_autocommit_or_commit_partial_work(engine_table):
    config, table = engine_table
    barrier = threading.Barrier(2, timeout=3)

    def write(order):
        conn = connect_mysql(config)
        try:
            conn.execute_native(f"UPDATE `{table}` SET value=value+1 WHERE id=%s", (order[0],))
            barrier.wait()
            try:
                conn.execute_native(f"UPDATE `{table}` SET value=value+1 WHERE id=%s", (order[1],))
            except MySQLDeadlockError:
                # A swallowed 1213 must not make the next DML an unguarded,
                # independently committed statement on the autocommit socket.
                with pytest.raises(MySQLLeaseInvalidatedError):
                    conn.execute_native(f"INSERT INTO `{table}` VALUES(3,999)")
                with pytest.raises(MySQLLeaseInvalidatedError):
                    conn.commit()
                return "invalidated"
            conn.commit()
            return "committed"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(write, ((1, 2), (2, 1)))) == ["committed", "invalidated"]
    with connect_mysql(config) as conn:
        assert [tuple(row) for row in conn.execute_native(f"SELECT id,value FROM `{table}` ORDER BY id")] == [(1, 1), (2, 1)]


def test_pool_has_finite_capacity_and_wait_deadline():
    config = replace(_config(), pool_size=1, pool_wait_seconds=0.1)
    first = connect_mysql(config)
    try:
        started = time.monotonic()
        with pytest.raises(MySQLPoolTimeout):
            connect_mysql(config)
        assert time.monotonic() - started < 0.6
    finally:
        first.close()
    with connect_mysql(config) as recovered:
        assert recovered.execute_native("SELECT 1").fetchone()[0] == 1
    close_mysql_pools(config)


def test_idle_transaction_is_released_by_server_and_pool_recovers(engine_table):
    base, table = engine_table
    config = replace(base, transaction_seconds=1, lock_wait_seconds=1)
    abandoned = connect_mysql(config)
    try:
        abandoned.execute_native(f"UPDATE `{table}` SET value=7 WHERE id=1")
        time.sleep(1.3)  # deliberately exceed the server's idle transaction bound
        with connect_mysql(base) as successor:
            successor.execute_native(f"UPDATE `{table}` SET value=value+1 WHERE id=1")
        with connect_mysql(base) as reader:
            assert reader.execute_native(f"SELECT value FROM `{table}` WHERE id=1").fetchone()[0] == 1
    finally:
        abandoned.close()
    with connect_mysql(config) as recovered:
        assert recovered.execute_native("SELECT 1").fetchone()[0] == 1
    close_mysql_pools(config)


def test_long_select_is_cancelled_by_server_and_releases_its_transaction(engine_table):
    from app.storage.mysql import MySQLStatementTimeout

    base, table = engine_table
    config = replace(base, transaction_seconds=0.1)
    started = time.monotonic()
    try:
        with pytest.raises(MySQLStatementTimeout) as failure, connect_mysql(config) as conn:
            conn.execute_native(f"UPDATE `{table}` SET value=7 WHERE id=1")
            conn.execute_native(
                "SELECT SUM(a.ORDINAL_POSITION) FROM INFORMATION_SCHEMA.COLUMNS a "
                "CROSS JOIN INFORMATION_SCHEMA.COLUMNS b CROSS JOIN INFORMATION_SCHEMA.COLUMNS c",
            ).fetchone()
        assert failure.value.mysql_errno == 3024
        assert time.monotonic() - started < 0.7
        with connect_mysql(base) as reader:
            assert reader.execute_native(f"SELECT value FROM `{table}` WHERE id=1").fetchone()[0] == 0
    finally:
        close_mysql_pools(config)
