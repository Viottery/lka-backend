"""Focused live MySQL tests for read-only idle claims and guarded orphan recovery."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.agent_runs import AgentRunRecord, AgentRunStatus
from app.core.agent_storage import SqliteAgentRunStore
from app.core.background_jobs import BackgroundJobStore
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.mysql import MySQLConfig, close_mysql_pools
from tests.test_mysql_runtime_live import runtime  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("LKA_MYSQL_TEST_CONFIG"),
    reason="LKA_MYSQL_TEST_CONFIG is required for the disposable live MySQL database",
)


@pytest.fixture
def mysql_reduction_lab(tmp_path):
    config_path = Path(os.environ["LKA_MYSQL_TEST_CONFIG"]).expanduser().resolve(strict=True)
    config = MySQLConfig(**json.loads(config_path.read_text(encoding="utf-8")))
    identity = tmp_path / "mysql-load-reduction.sqlite3"
    configure_mysql(identity, config)
    prefix = "test.mysql.load." + uuid4().hex
    store = BackgroundJobStore(identity)
    run_store = SqliteAgentRunStore(identity)
    expected_tables = {
        "background_jobs", "background_job_scopes", "session_admissions", "agent_runs",
        "agent_run_events", "agent_tool_invocations", "agent_sessions",
    }
    ready = False
    try:
        with connect_database(identity) as conn:
            rows = conn.execute_native(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() "
                "AND table_name IN (" + ",".join("%s" for _ in expected_tables) + ")",
                tuple(sorted(expected_tables)),
            ).fetchall()
            ready = {row[0] for row in rows} == expected_tables
        if not ready:
            pytest.skip("Disposable MySQL schema is not bootstrapped for these tests")
        yield store, run_store, identity, config, prefix
    finally:
        if ready:
            with connect_database(identity) as conn:
                conn.execute("BEGIN")
                scopes = [row[0] for row in conn.execute(
                    "SELECT scope_id FROM background_job_scopes WHERE scope_id LIKE ?",
                    (prefix + ".%",),
                ).fetchall()]
                if scopes:
                    marks = ",".join("?" for _ in scopes)
                    for table in ("background_pending_inputs", "background_pending_watermarks",
                                  "background_watermark_heads", "background_job_inputs"):
                        conn.execute(f"DELETE FROM {table} WHERE scope_id IN ({marks})", scopes)
                    conn.execute(f"DELETE FROM background_jobs WHERE scope_id IN ({marks})", scopes)
                    conn.execute(f"DELETE FROM background_job_scopes WHERE scope_id IN ({marks})", scopes)
                session_id = prefix + ".session"
                run_id = prefix + ".orphan"
                conn.execute("DELETE FROM agent_run_events WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM agent_tool_invocations WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM agent_runs WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM session_admissions WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM agent_sessions WHERE session_id=?", (session_id,))
                conn.commit()
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)


def test_mysql_idle_claim_is_read_only_and_exact_claim_does_not_take_another_job(mysql_reduction_lab, monkeypatch):
    store, _, _, _, prefix = mysql_reduction_lab
    kind = prefix + ".exact"
    store.enqueue(kind, prefix + ".high", "other", {"run_id": "synthetic-other"}, priority=100)
    selected = store.enqueue(kind, prefix + ".selected", "selected", {"run_id": "synthetic-selected"})

    opened = store._connect
    statements = []

    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, sql, params=()):
            statements.append(sql.lstrip().split(None, 1)[0].upper())
            return self.connection.execute(sql, params)

    monkeypatch.setattr(store, "_connect", lambda: RecordingConnection(opened()))
    assert store.claim("idle-worker", 30, kinds=(kind + ".missing",)) is None
    assert "BEGIN" not in statements
    assert not any(word in {"INSERT", "UPDATE", "DELETE", "REPLACE"} for word in statements)

    monkeypatch.setattr(store, "_connect", opened)
    claimed = store.claim("exact-worker", 30, kinds=(kind,), job_ids=(selected["job_id"],))
    assert claimed and claimed["job_id"] == selected["job_id"]
    assert store.get(store.list(scope_id=prefix + ".high", limit=1)[0]["job_id"])["status"] == "queued"


def test_mysql_deadline_cleanup_is_capped_per_sweep(mysql_reduction_lab):
    store, _, _, _, prefix = mysql_reduction_lab
    kind = prefix + ".deadline"
    jobs = [store.enqueue(kind, f"{prefix}.deadline.{index}", "expired", {},
                          deadline=datetime.now(UTC) + timedelta(minutes=1))
            for index in range(40)]
    old = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store._connect() as conn:
        conn.execute("UPDATE background_jobs SET deadline=? WHERE job_id IN (" +
                     ",".join("?" for _ in jobs) + ")", (old, *(job["job_id"] for job in jobs)))
    store._next_deadline_sweep = time.monotonic() - 1
    assert store.claim("deadline-worker", 30, kinds=(kind,)) is None
    statuses = [store.get(job["job_id"])["status"] for job in jobs]
    assert statuses.count("failed") == 32
    assert statuses.count("queued") == 8


def test_mysql_maintenance_expires_only_selected_kind_without_claiming(mysql_reduction_lab):
    store, _, _, _, prefix = mysql_reduction_lab
    kind = prefix + ".maintenance"
    selected = store.enqueue(kind, prefix + ".expired", "expired", {},
                             deadline=datetime.now(UTC) + timedelta(minutes=1))
    unrelated = store.enqueue(prefix + ".unrelated", prefix + ".untouched", "unrelated", {},
                              deadline=datetime.now(UTC) + timedelta(minutes=1))
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store._connect() as conn:
        conn.execute("UPDATE background_jobs SET deadline=? WHERE job_id IN (?,?)",
                     (past, selected["job_id"], unrelated["job_id"]))
        store._ensure_scope(conn, kind, prefix + ".pending")
        conn.execute(
            "INSERT INTO background_pending_watermarks"
            "(kind,scope_id,watermark_id,payload_json,priority,available_at,max_attempts,deadline,updated_at) "
            "VALUES(?,?, 'expired-watermark','{}',0,?,3,?,?)",
            (kind, prefix + ".pending", past, past, past),
        )
        conn.execute(
            "INSERT INTO background_pending_inputs(kind,scope_id,input_id,created_at) VALUES(?,?,?,?)",
            (kind, prefix + ".input", "input-1", past),
        )
    store._next_deadline_sweep = time.monotonic() - 1
    assert store.maintain_expired(kinds=(kind,)) == 2
    assert store.get(selected["job_id"])["status"] == "failed"
    assert store.get(unrelated["job_id"])["status"] == "queued"
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM background_pending_watermarks WHERE scope_id=?",
                            (prefix + ".pending",)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM background_pending_inputs WHERE scope_id=?",
                            (prefix + ".input",)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM background_jobs WHERE scope_id=? AND status='running'",
                            (prefix + ".expired",)).fetchone()[0] == 0
    assert store.maintain_expired(kinds=(kind,)) == 0


def test_mysql_unstarted_queued_root_recovery_locks_and_preserves_other_jobs(mysql_reduction_lab):
    store, run_store, identity, _, prefix = mysql_reduction_lab
    session_id, run_id = prefix + ".session", prefix + ".orphan"
    created = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with connect_database(identity) as conn:
        conn.execute(
            "INSERT INTO agent_sessions(session_id,title,status,metadata,created_at,updated_at) "
            "VALUES(?,?,'active','{}',?,?)", (session_id, "Synthetic orphan recovery", created, created),
        )
    record = AgentRunRecord(
        run_id=run_id, session_id=session_id, trace_id=prefix + ".trace", status=AgentRunStatus.QUEUED,
        user_input="synthetic only", created_at=created,
    ).model_dump(mode="json")
    run_store.save_run(record, updated_at=created)
    unrelated = store.enqueue("agent_dispatch", prefix + ".other", "other", {"run_id": "other-run"})
    lock = threading.Lock()
    guard_states = []

    @contextmanager
    def recovery_guard(guarded_run_id):
        assert guarded_run_id == run_id
        lock.acquire()
        try:
            # This caller-owned lease is held while store terminalization commits.
            guard_states.append(lock.locked())
            yield True
        finally:
            lock.release()

    recovered = run_store.reconcile_unstarted_queued_runs(
        recovery_guard=recovery_guard, older_than=created, limit=5,
    )
    assert len(recovered) == 1 and recovered[0]["status"] == "failed"
    assert guard_states == [True]
    assert store.get(unrelated["job_id"])["status"] == "queued"
    with connect_database(identity) as conn:
        status = conn.execute("SELECT status FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()[0]
        slot = conn.execute("SELECT run_id FROM session_admissions WHERE session_id=?", (session_id,)).fetchone()[0]
        event = conn.execute("SELECT event_payload FROM agent_run_events WHERE run_id=?", (run_id,)).fetchone()
    assert status == "failed" and slot is None
    assert json.loads(event[0])["type"] == "run_failed"


def test_mysql_knowledge_acl_and_secret_policy_remain_enforced(runtime):  # noqa: F811
    marker = "mysql-acl-secret-" + uuid4().hex
    allowed = runtime.create_session(title="Synthetic ACL allowed " + marker).session.session_id
    denied = runtime.create_session(title="Synthetic ACL denied " + marker).session.session_id
    imported = runtime.knowledge_service.import_text_document(KnowledgeDocumentInput(
        source=KnowledgeSourceInput(
            display_name=marker, uri="synthetic://" + marker, sensitivity="secret", remote_policy="deny",
            access_scope={"session_ids": [allowed]},
        ),
        title=marker, uri="synthetic://" + marker,
        text="synthetic api_key=sk-" + uuid4().hex * 2,
        sensitivity="secret", remote_policy="deny",
    ))
    try:
        assert imported.secret_chunks_redacted == 1
        visible = runtime.knowledge_service.list_authorized_source_ids(
            workspace_path=None, session_id=allowed,
        )
        invisible = runtime.knowledge_service.list_authorized_source_ids(
            workspace_path=None, session_id=denied,
        )
        assert imported.source_id in visible
        assert imported.source_id not in invisible
        searched = runtime.knowledge_service.search(query=marker, source_ids=[imported.source_id])
        assert searched.results == []
        document = runtime.knowledge_service.load_document(document_id=imported.document_id, include_text=True)
        assert document.text is None
        assert document.policy_decision == "deny"
    finally:
        with runtime._conn() as conn:
            conn.execute("DELETE FROM knowledge_chunks_fts WHERE document_id=?", (imported.document_id,))
            conn.execute("DELETE FROM knowledge_embedding_records WHERE chunk_id IN "
                         "(SELECT chunk_id FROM knowledge_chunks WHERE document_id=?)", (imported.document_id,))
            conn.execute("DELETE FROM knowledge_access_audit WHERE document_id=?", (imported.document_id,))
            conn.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (imported.document_id,))
            conn.execute("DELETE FROM knowledge_documents WHERE document_id=?", (imported.document_id,))
            conn.execute("DELETE FROM knowledge_sources WHERE source_id=?", (imported.source_id,))
            conn.execute("DELETE FROM session_admissions WHERE session_id IN (?,?)", (allowed, denied))
            conn.execute("DELETE FROM agent_sessions WHERE session_id IN (?,?)", (allowed, denied))
