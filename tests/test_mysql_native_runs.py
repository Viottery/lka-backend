"""Native run persistence retains admission ordering and SQLite update semantics."""
from __future__ import annotations

import os
import sqlite3
from uuid import uuid4

import pytest

from app.core import agent_storage
from app.core.agent_storage import SqliteAgentRunStore
from app.core.session_admission import SessionAdmissionStore


def _record(**updates):
    return dict(run_id="run-native", session_id="session-native", status="queued",
                parent_run_id=None, created_at="2026-10-29T00:00:00+00:00", **updates)


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
@pytest.mark.parametrize("status", ["queued", "completed"])
def test_run_upsert_keeps_admission_order_and_binds_all_fields(monkeypatch, dialect, status):
    calls = []

    class Connection:
        def execute(self, sql, params):
            calls.append(("execute", sql, params))

        def execute_native(self, sql, params):
            calls.append(("execute_native", sql, params))

    conn = Connection()
    conn.dialect = dialect
    monkeypatch.setattr(agent_storage, "lock_row", lambda *a: calls.append(("lock_run",)) or None)
    monkeypatch.setattr(SessionAdmissionStore, "lock_session", lambda *a: calls.append(("lock_session",)))
    monkeypatch.setattr(SessionAdmissionStore, "reserve", lambda *a, **k: calls.append(("reserve",)))
    monkeypatch.setattr(SessionAdmissionStore, "_existing_session", lambda *a, **k: calls.append(("existing",)))
    monkeypatch.setattr(SessionAdmissionStore, "release_if_terminal", lambda *a, **k: calls.append(("release",)))
    record = _record()
    record["status"] = status
    store = SqliteAgentRunStore.__new__(SqliteAgentRunStore)
    store.save_run_in_transaction(conn, record, updated_at="updated")
    operation = "execute_native" if dialect == "mysql" else "execute"
    assert [call[0] for call in calls] == [
        "lock_session", "lock_run", "reserve" if status == "queued" else "existing", operation, "release",
    ]
    _, sql, params = calls[3]
    assert params == (record["run_id"], record["session_id"], status,
                      agent_storage._json_dump(record), record["created_at"], "updated")
    update = sql.split("UPDATE", 1)[1]
    assert "created_at" not in update
    for field in ("session_id", "status", "record_payload", "updated_at"):
        assert field in update
    assert ("ON DUPLICATE KEY UPDATE" in sql) == (dialect == "mysql")


def test_sqlite_run_update_preserves_created_at_and_transaction_rollback(tmp_path):
    path = tmp_path / "runs.sqlite3"
    store = SqliteAgentRunStore(path)
    record = _record()
    store.save_run(record, updated_at="initial")
    replacement = dict(record, session_id="changed-session", status="completed", created_at="replacement")
    store.save_run(replacement, updated_at="changed")
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT session_id,status,record_payload,created_at,updated_at FROM agent_runs").fetchone()
        assert row == ("changed-session", "completed", agent_storage._json_dump(replacement), record["created_at"], "changed")
        conn.execute("BEGIN")
        store.save_run_in_transaction(conn, dict(replacement, status="failed"), updated_at="rolled-back")
        conn.rollback()
        assert conn.execute("SELECT status,updated_at FROM agent_runs").fetchone() == ("completed", "changed")


@pytest.mark.skipif(not os.environ.get("LKA_MYSQL_TEST_CONFIG"), reason="Disposable MySQL config required")
def test_live_native_run_update_and_rollback_avoid_upsert_metadata(tmp_path, monkeypatch):
    from app.storage.connections import configure_mysql, connect_database, unregister_mysql
    from app.storage.mysql import close_mysql_pools
    from tests.test_mysql_owner_live import _config

    config = _config()
    identity = tmp_path / "native-run-authority.sqlite3"
    configure_mysql(identity, config)
    record = _record()
    record.update(run_id="test.native-run." + uuid4().hex, session_id="test.native-session." + uuid4().hex)
    try:
        store = SqliteAgentRunStore(identity)
        # A native write must not invoke the generic ON CONFLICT metadata path.
        with connect_database(identity) as conn:
            monkeypatch.setattr(conn._schema, "validate_conflict_target", lambda *a: pytest.fail("generic upsert metadata path"))
            conn.execute("BEGIN")
            store.save_run_in_transaction(conn, record, updated_at="initial")
            replacement = dict(record, status="completed", created_at="replacement", marker="updated")
            store.save_run_in_transaction(conn, replacement, updated_at="changed")
            row = conn.execute("SELECT status,record_payload,created_at,updated_at FROM agent_runs WHERE run_id=?",
                               (record["run_id"],)).fetchone()
            assert tuple(row) == ("completed", agent_storage._json_dump(replacement), record["created_at"], "changed")
            conn.rollback()
        with connect_database(identity) as conn:
            assert conn.execute("SELECT 1 FROM agent_runs WHERE run_id=?", (record["run_id"],)).fetchone() is None
            assert conn.execute("SELECT 1 FROM session_admissions WHERE session_id=?", (record["session_id"],)).fetchone() is None
        assert not identity.exists()
    finally:
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)
