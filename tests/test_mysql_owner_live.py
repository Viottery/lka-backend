"""Actual engine ownership tests; opt in with a dedicated test database only."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from app.storage.mysql import MySQLConfig, MySQLTransactionGuardError, connect_mysql
from app.storage.mysql_owner import MySQLCoordinatorOwner


def _config():
    filename = os.environ.get("LKA_MYSQL_TEST_CONFIG")
    if not filename:
        pytest.skip("dedicated MySQL config not supplied")
    config = MySQLConfig(**json.loads(Path(filename).read_text()))
    if not any(part in config.database.lower() for part in ("test", "lab")):
        pytest.fail("live MySQL tests require an explicitly named test/lab database")
    return config


def test_one_coordinator_and_stale_owner_is_fenced():
    config = _config()
    first = MySQLCoordinatorOwner(config, heartbeat_interval_seconds=0.1)
    second = MySQLCoordinatorOwner(config)
    assert first.acquire()
    try:
        assert not second.acquire()
        with connect_mysql(config) as conn:
            conn.set_transaction_guard(first.guard_transaction)
            conn.execute("BEGIN")
            assert conn.execute_native("SELECT token FROM lka_coordinator_owner WHERE owner_id=1").fetchone()
        # Model a failed coordinator session without killing any business query.
        first._connection.close()
        for _ in range(100):
            if first.ownership_lost:
                break
            threading.Event().wait(0.01)
        assert first.ownership_lost
        assert not first.is_active
        assert not first.acquire()
        with connect_mysql(config) as conn:
            conn.set_transaction_guard(first.guard_transaction)
            with pytest.raises(MySQLTransactionGuardError):
                conn.execute("BEGIN")
            assert not conn.in_transaction
        assert second.acquire()
        with connect_mysql(config) as conn:
            conn.set_transaction_guard(first.guard_transaction)
            with pytest.raises(MySQLTransactionGuardError):
                conn.execute("BEGIN")
        with connect_mysql(config) as conn:
            conn.set_transaction_guard(second.guard_transaction)
            conn.execute("BEGIN")
            assert conn.in_transaction
    finally:
        first.release()
        second.release()


def test_owner_heartbeat_keeps_session_alive_past_short_idle_timeout():
    owner = MySQLCoordinatorOwner(_config(), heartbeat_interval_seconds=0.1)
    assert owner.acquire()
    raw = owner._connection
    try:
        with raw.cursor() as cursor:
            cursor.execute("SET SESSION wait_timeout=2")
        # This is a per-session override in the isolated lab to simulate a
        # server with a short idle timeout; the production acquisition path
        # starts with wait_timeout >= 300.
        threading.Event().wait(2.5)
        assert owner.is_active
        assert not owner.ownership_lost
        token, connection_id = owner.fence_identity()
        assert token == owner._token and connection_id == owner._connection_id
        with raw.cursor() as cursor:
            cursor.execute("SELECT IS_USED_LOCK(%s)=CONNECTION_ID()", (owner._name,))
            assert cursor.fetchone()[0] == 1
    finally:
        owner.release()
