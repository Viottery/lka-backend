"""Guarded native event persistence, including real MySQL schema assumptions."""
from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.agent_storage import SqliteAgentRunStore
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.mysql import MySQLConfig, close_mysql_pools


@pytest.fixture
def native_events_store(tmp_path):
    if not os.environ.get("LKA_MYSQL_TEST_CONFIG"):
        pytest.skip("isolated MySQL lab configuration required")
    config = MySQLConfig(**json.loads(Path(os.environ["LKA_MYSQL_TEST_CONFIG"]).read_text()))
    path = tmp_path / "native-events.sqlite3"
    configure_mysql(path, config)
    run_id = "native-events-" + uuid4().hex
    try:
        with closing(connect_database(path)) as conn, conn:
            unique = {}
            for row in conn.execute_native(
                "SELECT INDEX_NAME,COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_run_events' "
                "AND NON_UNIQUE=0 ORDER BY INDEX_NAME,SEQ_IN_INDEX"
            ):
                unique.setdefault(row[0], []).append(row[1])
            # The compatibility schema adds an invisible auto-generated rowid;
            # this INSERT never supplies it, so only the event identity can collide.
            assert unique == {"PRIMARY": ["run_id", "sequence"], "__lka_rowid": ["__lka_rowid"]}
            rowid = conn.execute_native(
                "SELECT EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='agent_run_events' AND COLUMN_NAME='__lka_rowid'"
            ).fetchone()
            assert "auto_increment" in rowid[0].lower()
            conn.execute_native(
                "INSERT INTO agent_runs(run_id,session_id,status,record_payload,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (run_id, run_id, "running", json.dumps({"parent_run_id": "synthetic-parent"}), "v0", "v0"),
            )
        store = SqliteAgentRunStore(path)
        yield store, run_id
    finally:
        with closing(connect_database(path)) as conn, conn:
            conn.execute("DELETE FROM agent_run_events WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM agent_runs WHERE run_id=?", (run_id,))
        unregister_mysql(path, expected=config)
        close_mysql_pools(config)


def _event(run_id, sequence, value, created_at="v1"):
    return {"run_id": run_id, "sequence": sequence, "created_at": created_at,
            "type": "synthetic", "payload": {"value": value}}


def test_native_event_upsert_preserves_creation_and_input_order(native_events_store, monkeypatch):
    store, run_id = native_events_store
    from app.storage.mysql import MySQLConnection
    original = MySQLConnection.execute_native
    inserts = []

    def observed(conn, statement, parameters=()):
        if "INSERT INTO agent_run_events" in statement:
            inserts.append((statement, parameters))
        return original(conn, statement, parameters)

    monkeypatch.setattr(MySQLConnection, "execute_native", observed)
    store.save_events([_event(run_id, 2, "second"), _event(run_id, 1, "first"),
                       _event(run_id, 2, "replacement", "v2")])
    assert [params[1] for _, params in inserts] == [2, 1, 2]
    assert all("ON DUPLICATE KEY UPDATE" in statement for statement, _ in inserts)
    with closing(connect_database(store.db_path)) as conn:
        rows = conn.execute(
            "SELECT sequence,event_payload,created_at FROM agent_run_events WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    assert json.loads(rows[1][1])["payload"]["value"] == "replacement"
    assert rows[1][2] == "v1"


def test_native_event_batch_failure_rolls_back_all_events(native_events_store, monkeypatch):
    store, run_id = native_events_store
    from app.storage.mysql import MySQLConnection
    original = MySQLConnection.execute_native

    def fail_second(conn, statement, parameters=()):
        if "INSERT INTO agent_run_events" in statement and parameters[1] == 2:
            raise RuntimeError("synthetic event failure")
        return original(conn, statement, parameters)

    monkeypatch.setattr(MySQLConnection, "execute_native", fail_second)
    with pytest.raises(RuntimeError, match="synthetic event failure"):
        store.save_events([_event(run_id, 1, "first"), _event(run_id, 2, "second")])
    with closing(connect_database(store.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_run_events WHERE run_id=?", (run_id,)).fetchone()[0] == 0


def test_native_events_keep_transaction_guard_and_commit(monkeypatch, tmp_path):
    from app.core import agent_storage
    from app.storage import mysql
    from tests.test_mysql_storage import FakeRawConnection, config

    raw = FakeRawConnection()
    pool = mysql._Pool(config(), False)
    pool._created = 1
    conn = mysql.MySQLConnection(pool, raw)
    guarded = []
    conn.set_transaction_guard(lambda lease: guarded.append(lease.in_transaction))
    monkeypatch.setattr(agent_storage, "connect", lambda _: conn)
    store = object.__new__(SqliteAgentRunStore)
    store.db_path = tmp_path / "synthetic-mysql.sqlite3"
    store.save_events([_event("synthetic-run", 1, "first"), _event("synthetic-run", 2, "second")])
    assert guarded == [True]
    assert raw.commit_count == 1
    inserts = [sql for sql, _ in raw.statements if sql.startswith("INSERT INTO agent_run_events")]
    assert len(inserts) == 2
    assert all("ON DUPLICATE KEY UPDATE" in sql for sql in inserts)
