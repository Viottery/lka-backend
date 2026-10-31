"""Independent bounded review of native storage's physical and safety assumptions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from app.core import agent_storage
from app.storage.connections import connect_database
from app.storage.mysql import MySQLTransactionGuardError
from tests import test_mysql_native_events as event_tests

native_events_store = event_tests.native_events_store


def test_native_upsert_physical_key_and_trigger_assumptions(native_events_store):
    store, _ = native_events_store
    with closing(connect_database(store.db_path)) as conn:
        for table, expected in (("agent_runs", ["run_id"]),
                                ("agent_run_events", ["run_id", "sequence"])):
            keys = {}
            for row in conn.execute_native(
                "SELECT INDEX_NAME,COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND NON_UNIQUE=0 "
                "ORDER BY INDEX_NAME,SEQ_IN_INDEX", (table,),
            ):
                keys.setdefault(row[0], []).append(row[1])
            assert keys == {"PRIMARY": expected, "__lka_rowid": ["__lka_rowid"]}
            rowid = conn.execute_native(
                "SELECT EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME=%s AND COLUMN_NAME='__lka_rowid'", (table,),
            ).fetchone()
            assert rowid is not None and "auto_increment" in rowid[0].lower()
            assert conn.execute_native(
                "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=%s", (table,),
            ).fetchall() == []


def test_native_event_batch_rejected_owner_cannot_write(native_events_store, monkeypatch):
    store, run_id = native_events_store
    original = agent_storage.connect
    fenced = []

    def rejected(_conn):
        fenced.append(True)
        raise RuntimeError("synthetic revoked owner")

    def guarded_connect(path):
        conn = original(path)
        conn.set_transaction_guard(rejected)
        return conn

    monkeypatch.setattr(agent_storage, "connect", guarded_connect)
    with pytest.raises(MySQLTransactionGuardError):
        store.save_events([event_tests._event(run_id, 1, "rejected")])
    assert fenced == [True]
    with closing(connect_database(store.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_run_events WHERE run_id=?", (run_id,)).fetchone()[0] == 0


def test_native_event_same_run_concurrent_batches_preserve_all_rows(native_events_store):
    store, run_id = native_events_store
    barrier = Barrier(2)

    def persist(offset):
        barrier.wait(timeout=5)
        store.save_events([event_tests._event(run_id, offset + number, f"value-{offset + number}")
                           for number in range(1, 4)])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(persist, offset) for offset in (0, 3)]
        for future in futures:
            future.result(timeout=15)
    assert [event["sequence"] for event in store.list_events(run_id)] == [1, 2, 3, 4, 5, 6]
