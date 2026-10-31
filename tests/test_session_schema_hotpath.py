from __future__ import annotations

import json
import sqlite3
from threading import Event, Thread, current_thread

from app.core.session_admission import SessionAdmissionStore
from app.core.sessions import SessionService


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_context_schema_is_checked_once_per_service_instance(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    first_statements = []
    def traced_connect():
        conn = _connect(path)
        conn.set_trace_callback(first_statements.append)
        return conn

    service = SessionService(traced_connect)
    service.prepare_context_schema()

    assert any("CREATE TABLE IF NOT EXISTS agent_session_context_state" in sql
               for sql in first_statements)
    assert any("PRAGMA table_info(agent_session_context_state)" in sql
               for sql in first_statements)

    later_statements = []
    conn = _connect(path)
    conn.set_trace_callback(later_statements.append)
    service._ensure_context_state_table(conn)
    conn.close()
    assert not any(sql.lstrip().upper().startswith(("CREATE ", "ALTER ", "PRAGMA "))
                   for sql in later_statements)


def test_context_schema_first_use_upgrades_legacy_columns_without_losing_data(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE agent_session_context_state ("
                 "session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                 "next_seq INTEGER NOT NULL, covered_seq INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO agent_session_context_state VALUES('s1',3,8,5)")
    conn.commit()
    conn.close()

    service = SessionService(lambda: _connect(path))
    service.prepare_context_schema()
    conn = _connect(path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_session_context_state)")}
    row = conn.execute("SELECT * FROM agent_session_context_state WHERE session_id='s1'").fetchone()
    conn.close()

    assert {"summary_revision", "summary_metadata"} <= columns
    assert row["revision"] == 3
    assert row["next_seq"] == 8
    assert row["covered_seq"] == 5
    assert row["summary_revision"] == 0
    assert json.loads(row["summary_metadata"]) == {}


def test_admission_hot_path_does_not_repeat_schema_ddl(tmp_path):
    path = tmp_path / "admission.sqlite3"
    conn = _connect(path)
    conn.executescript("""
        CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE agent_runs(run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            status TEXT NOT NULL, record_payload TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO agent_sessions VALUES('s1','active');
    """)
    SessionAdmissionStore.ensure_schema(conn)
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)

    generation = SessionAdmissionStore.reserve(conn, "s1", "run-1", schema_prepared=True)
    SessionAdmissionStore.release_if_terminal(conn, {
        "parent_run_id": None,
        "status": "completed",
        "session_id": "s1",
        "run_id": "run-1",
    }, schema_prepared=True)
    conn.commit()
    conn.close()

    assert generation == 1
    assert not any(sql.lstrip().upper().startswith(("CREATE ", "ALTER ", "PRAGMA "))
                   for sql in statements)
    assert not any("sqlite_master" in sql for sql in statements)
    check = sqlite3.connect(path)
    row = check.execute("SELECT run_id,generation FROM session_admissions WHERE session_id='s1'").fetchone()
    check.close()
    assert row == (None, 1)


def test_admission_explicit_schema_initialization_remains_available(tmp_path):
    conn = _connect(tmp_path / "empty.sqlite3")
    SessionAdmissionStore.ensure_schema(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(session_admissions)")}
    conn.close()
    assert columns == {"session_id", "run_id", "generation"}


def test_lazy_context_setup_after_rollback_retries_then_publishes_ready(tmp_path):
    path = tmp_path / "rollback.sqlite3"
    service = SessionService(lambda: _connect(path))
    conn = _connect(path)
    conn.execute("BEGIN")
    service._ensure_context_state_table(conn)
    conn.rollback()
    conn.close()
    assert not service._context_schema_ready

    service.prepare_context_schema()
    assert service._context_schema_ready
    conn = _connect(path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_session_context_state'").fetchone()
    conn.close()


def test_lazy_context_setup_does_not_invert_schema_and_database_lock_order(tmp_path, monkeypatch):
    path = tmp_path / "schema-lock-order.sqlite3"
    service = SessionService(lambda: sqlite3.connect(path, timeout=0.5))
    writer_has_lock = Event()
    prepare_entered_schema = Event()
    errors = []
    original_create = service._create_context_state_schema

    def observed_create(conn):
        if current_thread().name == "schema-preparer":
            prepare_entered_schema.set()
        original_create(conn)

    monkeypatch.setattr(service, "_create_context_state_schema", observed_create)

    def lazy_writer():
        conn = sqlite3.connect(path, timeout=0.5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            writer_has_lock.set()
            if not prepare_entered_schema.wait(2):
                raise AssertionError("schema preparer did not reach migration")
            service._ensure_context_state_table(conn)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - re-raised by assertion in the test thread below
            errors.append(exc)
            conn.rollback()
        finally:
            conn.close()

    def prepare():
        if not writer_has_lock.wait(2):
            errors.append(AssertionError("lazy writer did not acquire its transaction"))
            return
        try:
            service.prepare_context_schema()
        except Exception as exc:  # noqa: BLE001 - re-raised by assertion in the test thread below
            errors.append(exc)

    writer = Thread(target=lazy_writer, name="schema-lazy-writer")
    preparer = Thread(target=prepare, name="schema-preparer")
    writer.start()
    preparer.start()
    writer.join(timeout=3)
    preparer.join(timeout=3)

    assert not writer.is_alive()
    assert not preparer.is_alive()
    assert errors == []
    assert service._context_schema_ready


def test_admission_direct_lazy_api_still_initializes_schema(tmp_path):
    conn = _connect(tmp_path / "lazy-admission.sqlite3")
    conn.executescript("""
        CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE agent_runs(run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            status TEXT NOT NULL, record_payload TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO agent_sessions VALUES('s1','active');
    """)
    assert SessionAdmissionStore.reserve(conn, "s1", "run-1") == 1
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(session_admissions)")}
    conn.close()
    assert columns == {"session_id", "run_id", "generation"}
