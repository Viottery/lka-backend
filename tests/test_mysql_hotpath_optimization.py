"""Result-local reuse and connection cleanup must retain safety contracts."""

from __future__ import annotations

import sqlite3
import threading
import time
import weakref

import pytest

from app.storage import mysql
from tests.test_mysql_storage import FakeRawConnection, config

pytest.importorskip("sqlglot")


class _StateRaw(FakeRawConnection):
    def __init__(self):
        super().__init__()
        self.server_status = 2  # SERVER_STATUS_AUTOCOMMIT
        self.autocommit = True
        self.fail_rollback = False

    @property
    def open(self):
        return not self.closed

    def get_autocommit(self):
        return self.autocommit

    def begin(self):
        super().begin()
        self.server_status |= 1

    def commit(self):
        super().commit()
        self.server_status &= ~1

    def rollback(self):
        if self.fail_rollback or self.closed:
            raise RuntimeError("synthetic reset failure")
        super().rollback()
        if isinstance(getattr(self, "server_status", None), int):
            self.server_status &= ~1


def _lease():
    pool = mysql._Pool(config(), False)
    pool._created = 1
    raw = _StateRaw()
    return mysql.MySQLConnection(pool, raw), pool, raw


def test_clean_return_skips_reset_but_checkout_still_verifies_socket():
    conn, pool, raw = _lease()
    conn.execute("SELECT 1")
    conn.close()
    assert raw.rollback_count == 0
    assert pool.acquire() is raw
    assert raw.ping_count == 1
    pool.release(raw, broken=True)
    assert raw.closed and pool._created == 0


@pytest.mark.parametrize("state", ["transaction", "autocommit_off", "unknown", "missing"])
def test_return_rolls_back_active_or_uncertain_state(state):
    conn, pool, raw = _lease()
    if state == "transaction":
        # Even if the wrapper did not start it, the driver's transaction wins.
        raw.server_status |= 1
    elif state == "autocommit_off":
        raw.autocommit = False
    elif state == "unknown":
        raw.server_status = None
    else:
        del raw.server_status
    conn.close()
    assert raw.rollback_count == 1
    assert pool._idle == [raw]


def test_close_active_transaction_rolls_back_once_and_preserves_owner_guard():
    conn, pool, raw = _lease()
    guarded = []
    conn.set_transaction_guard(lambda lease: guarded.append(lease.in_transaction))
    conn.execute("UPDATE sample SET name=? WHERE id=?", ("synthetic", 1))
    conn.execute("UPDATE sample SET name=? WHERE id=?", ("synthetic", 2))
    assert guarded == [True] and raw.begin_count == 1
    conn.close()
    assert raw.rollback_count == 1 and not raw.in_transaction
    assert pool._idle == [raw]


def test_successful_commit_is_not_removed_and_unknown_commit_never_reused():
    conn, pool, raw = _lease()
    conn.execute("UPDATE sample SET name=? WHERE id=?", ("synthetic", 1))
    conn.commit()
    conn.close()
    assert raw.commit_count == 1 and raw.rollback_count == 0
    assert pool._idle == [raw]

    conn, pool, raw = _lease()
    conn.execute("UPDATE sample SET name=? WHERE id=?", ("synthetic", 1))
    raw.fail_commit = RuntimeError("synthetic unknown commit outcome")
    with pytest.raises(mysql.MySQLCommitOutcomeUnknown):
        conn.commit()
    assert raw.closed and pool._created == 0 and not pool._idle
    del conn
    assert raw.closed and pool._created == 0 and not pool._idle


def test_failed_reset_and_broken_connection_are_discarded():
    conn, pool, raw = _lease()
    raw.server_status |= 1
    raw.fail_rollback = True
    conn.close()
    assert raw.closed and pool._created == 0 and not pool._idle


def test_abandoned_schema_bound_lease_is_discarded_when_wrapper_is_collected():
    conn, pool, raw = _lease()

    class Schema:
        def rowid_column(self, connection, _table):
            return "rowid"

        def validate_conflict_target(self, connection, _table, _columns):
            return True

        def validate_ignore_target(self, connection, _table):
            return None

    conn.set_schema(Schema())
    reference = weakref.ref(conn)
    del conn

    assert reference() is None
    assert raw.closed
    assert pool._created == 0
    assert not pool._idle


def test_real_schema_attach_resolvers_do_not_retain_abandoned_lease():
    from app.storage.mysql_schema import MySQLSchema, attach_schema_metadata

    conn, pool, raw = _lease()
    schema = MySQLSchema("fixture")
    rowid_calls = []
    schema.rowid_column = lambda _connection, table: rowid_calls.append(table) or "proxy_id"
    schema.validate_conflict_target = lambda _connection, _table, _columns: True
    schema.validate_ignore_target = lambda _connection, _table: None
    attach_schema_metadata(conn, schema, ensure_metadata=False, verify_metadata=False)

    assert conn._schema is schema
    assert conn._rowid_column("records") == "proxy_id"
    assert conn._rowid_column("RECORDS") == "proxy_id"
    assert rowid_calls == ["records"]
    assert conn._conflict_target("records", ("id",))
    assert conn._ignore_target("records") is None

    reference = weakref.ref(conn)
    del conn
    assert reference() is None
    assert raw.closed
    assert pool._created == 0
    assert not pool._idle


def test_abandoned_transaction_is_discarded_without_commit():
    conn, pool, raw = _lease()
    conn.execute("UPDATE sample SET name=? WHERE id=?", ("synthetic", 1))
    reference = weakref.ref(conn)
    del conn

    assert reference() is None
    assert raw.closed
    assert raw.commit_count == 0
    assert pool._created == 0
    assert not pool._idle


def test_explicit_close_detaches_finalizer_and_never_double_releases():
    conn, pool, raw = _lease()
    conn.close()
    assert pool._created == 1 and pool._idle == [raw]
    del conn
    assert pool._created == 1 and pool._idle == [raw] and not raw.closed
    pool.release(raw, broken=True)
    assert pool._created == 0 and raw.closed


def test_abandoned_lease_release_wakes_pool_waiter(monkeypatch):
    pool = mysql._Pool(config(pool_size=1, pool_wait_seconds=1), False)
    pool._created = 1
    raw = _StateRaw()
    conn = mysql.MySQLConnection(pool, raw)
    replacement = _StateRaw()
    monkeypatch.setattr(pool, "_connect_raw", lambda: replacement)
    acquired = []
    finished = threading.Event()

    def wait_for_lease():
        acquired.append(pool.acquire())
        finished.set()

    waiter = threading.Thread(target=wait_for_lease)
    waiter.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with pool._condition:
            if pool._waiters:
                break
        time.sleep(0.005)
    else:
        pytest.fail("pool waiter did not enter the wait queue")

    del conn
    assert finished.wait(1)
    waiter.join(timeout=1)
    assert acquired == [replacement]
    assert raw.closed and pool._created == 1
    pool.release(replacement, broken=True)
    assert pool._created == 0

    conn, pool, raw = _lease()
    conn._broken = True
    conn.close()
    assert raw.rollback_count == 0
    assert raw.closed and pool._created == 0 and not pool._idle


def test_result_metadata_is_shared_immutable_and_result_local():
    conn, _pool, raw = _lease()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor._cursor.description = (("id",), ("id",))
    cursor._cursor.rows = [(1, 2), (3, 4)]
    first, second = cursor.fetchall()
    assert tuple(first) == (1, 2) and tuple(second) == (3, 4)
    assert first["id"] == 1 and second["id"] == 3
    assert first._names is second._names
    assert first._first_index is second._first_index
    with pytest.raises(TypeError):
        first._first_index["id"] = 1
    first.keys().clear()
    assert first.keys() == ["id", "id"]

    cursor.execute("SELECT id,name FROM sample")
    replacement = cursor.fetchone()
    assert replacement.keys() == ["id", "name"]
    assert dict(replacement) == {"id": 7, "name": "sample"}
    assert first.keys() == ["id", "id"] and tuple(first) == (1, 2)
    independent = conn.execute("SELECT id,name FROM sample").fetchone()
    assert independent._first_index is not replacement._first_index
    assert not raw.in_transaction
    conn.close()


def test_custom_factory_and_mapping_rows_keep_result_protocol():
    conn, _pool, _raw = _lease()
    calls = []

    def factory(cursor, row):
        calls.append(cursor)
        return dict(row)

    conn.row_factory = factory
    cursor = conn.execute("SELECT id,name FROM sample")
    cursor._cursor.rows = [{"id": 9, "name": "mapping"}]
    assert cursor.fetchone() == {"id": 9, "name": "mapping"}
    assert calls == [cursor]
    conn.close()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("write", [False, True])
def test_with_is_classified_once_per_execute_and_write_still_starts_transaction(
    monkeypatch, native, write,
):
    conn, _pool, raw = _lease()
    original = mysql.MySQLConnection._kind
    classified = []

    def classify(statement):
        classified.append(statement)
        return original(statement)

    monkeypatch.setattr(mysql.MySQLConnection, "_kind", staticmethod(classify))
    statement = "WITH candidate AS (SELECT id FROM sample WHERE name=?) "
    statement += (
        "UPDATE sample SET name='next' WHERE id IN (SELECT id FROM candidate)"
        if write else "SELECT id FROM candidate"
    )
    guarded = []
    conn.set_transaction_guard(lambda lease: guarded.append(lease.in_transaction))
    (conn.execute_native if native else conn.execute)(statement, ("synthetic",))
    assert classified == [statement]
    assert raw.begin_count == int(write)
    assert guarded == ([True] if write else [])
    conn.close()
