"""Recovery retries SQL admission, never a model/tool handler."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import agent_storage, background_jobs
from app.core.agent_runs import AgentRunRecord, AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_storage import connect as connect_run_store
from app.core.background_jobs import BackgroundJobStore, BackgroundJobWorker, _classify_worker_error
from app.core.sessions import SessionService
from app.storage import sqlite_retry
from app.storage.sqlite_retry import begin_sqlite_write
from app.storage.sqlite_timing import connect_sqlite
from app.storage.sqlite_write_queue import (
    SqliteTransactionTimeoutError,
    SqliteWriteAdmissionError,
    SqliteWriteQueueError,
    WriteQueuePolicy,
)


def test_real_queue_timeout_retries_only_admission_then_executes_sql_once(tmp_path, monkeypatch):
    path = tmp_path / "admission.sqlite3"
    policy = WriteQueuePolicy(wait_seconds=0.03, transaction_seconds=3)
    with connect_sqlite(path, write_policy=policy) as setup:
        setup.execute("CREATE TABLE effects(value)")
    setup.close()
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold_writer():
        conn = connect_sqlite(path, write_policy=policy)
        try:
            conn.execute("BEGIN IMMEDIATE")
            ready.set()
            assert release.wait(3)
            conn.rollback()
        except BaseException as exc:  # noqa: BLE001 - return writer-thread failures to the test.
            errors.append(exc)
        finally:
            conn.close()

    writer = threading.Thread(target=hold_writer)
    writer.start()
    assert ready.wait(2)
    retries = []

    def retry_pause(seconds):
        retries.append(seconds)
        release.set()
        writer.join(2)
        assert not writer.is_alive()

    monkeypatch.setattr(sqlite_retry, "time", SimpleNamespace(sleep=retry_pause))
    conn = connect_sqlite(path, write_policy=policy)
    try:
        begin_sqlite_write(conn)
        conn.execute("INSERT INTO effects VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
        assert retries == [0.05]
        assert not errors
        assert not conn._write_queue.waiters
    finally:
        release.set()
        writer.join(2)
        conn.close()


@pytest.mark.parametrize("error", [
    SqliteTransactionTimeoutError("expired"),
    SqliteWriteQueueError("nested write"),
    sqlite3.OperationalError("syntax error"),
])
def test_expired_nested_and_sql_errors_are_not_retried(error):
    calls = []

    def execute(sql):
        calls.append(sql)
        raise error

    with pytest.raises(type(error)):
        begin_sqlite_write(SimpleNamespace(execute=execute, in_transaction=False))
    assert calls == ["BEGIN IMMEDIATE"]


def test_completion_admission_recovers_without_repeating_handler(tmp_path, monkeypatch):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3")
    store.ensure_schema()
    job = store.enqueue("test", "scope", "once", {})
    original_connect = store._connect
    calls, attempted = [], []

    def handler(_):
        calls.append("external_effect")

        def fault_connection():
            conn = original_connect()
            execute = conn.execute

            def execute_once(sql, *args, **kwargs):
                if sql == "BEGIN IMMEDIATE" and not attempted:
                    attempted.append("admission")
                    raise SqliteWriteAdmissionError("wait_timeout")
                return execute(sql, *args, **kwargs)

            conn.execute = execute_once
            return conn

        monkeypatch.setattr(store, "_connect", fault_connection)

    assert BackgroundJobWorker(store, {"test": handler}).run_one()
    assert calls == ["external_effect"]
    assert attempted == ["admission"]
    assert store.get(job["job_id"])["status"] == "succeeded"
    assert store.get(job["job_id"])["attempts"] == 1


def test_handler_admission_error_keeps_precise_class_without_replaying_effect(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3")
    store.ensure_schema()
    job = store.enqueue("test", "scope", "once", {}, max_attempts=3)
    calls = []

    def handler(_):
        calls.append("model_or_tool")
        raise SqliteWriteAdmissionError("wait_timeout")

    worker = BackgroundJobWorker(store, {"test": handler})
    assert worker.run_one()
    assert not worker.run_one()
    failed = store.get(job["job_id"])
    assert failed["status"] == "failed"
    assert failed["error_class"] == "database_queue_wait_timeout"
    assert failed["attempts"] == 1
    assert calls == ["model_or_tool"]
    assert _classify_worker_error(SqliteTransactionTimeoutError("expired")) == (
        "database_transaction_expired", False,
    )


def test_recent_failure_metrics_exclude_old_and_recovered_jobs(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3")
    store.ensure_schema()
    rows = {}
    for key in ("old", "recovered", "recent"):
        job = store.enqueue("test", key, key, {})
        rows[key] = job["job_id"]
    now = datetime.now(UTC)
    with sqlite3.connect(store.db_path) as conn:
        for key, status, stamp in [
            ("old", "failed", now - timedelta(days=2)),
            ("recovered", "succeeded", now),
            ("recent", "failed", now),
        ]:
            conn.execute(
                "UPDATE background_jobs SET status=?,error_class=?,updated_at=? WHERE job_id=?",
                (status, key, stamp.isoformat(), rows[key]),
            )
    metrics = store.health_metrics()
    assert metrics["recent_failure_categories"] == {"recent": 1}
    assert metrics["recent_failure_count"] == 1
    assert metrics["recent_failure_window_seconds"] == 86400
    assert metrics["queue_depth_by_status"]["failed"] == 2


def test_admission_retry_does_not_renew_expired_job_lease(tmp_path, monkeypatch):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3")
    store.ensure_schema()
    job = store.enqueue("test", "scope", "once", {})
    claimed = store.claim("owner", 30)
    original = background_jobs.begin_sqlite_write

    def acquire_and_expire(conn):
        original(conn)
        # The old pre-admission timestamp precedes this boundary; a timestamp
        # refreshed after admission observes the now-expired lease.
        expiry = datetime.now(UTC).isoformat()
        conn.execute("UPDATE background_jobs SET lease_expires_at=? WHERE job_id=?", (expiry, job["job_id"]))

    monkeypatch.setattr(background_jobs, "begin_sqlite_write", acquire_and_expire)
    assert not store.heartbeat(job["job_id"], "owner", claimed["lease_epoch"], 60)
    assert not store.complete(job["job_id"], "owner", claimed["lease_epoch"])
    assert not store.complete_input(job["job_id"], "owner", claimed["lease_epoch"], "input-1")


def test_async_event_batch_recovers_admission_without_poisoning_writer(tmp_path, monkeypatch):
    store = SqliteAgentRunStore(tmp_path / "event-admission.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="events", user_input="events")
    connect = agent_storage.connect
    failures = []

    def fault_connection(*args, **kwargs):
        conn = connect(*args, **kwargs)
        executemany = conn.executemany

        def insert_events_once(sql, parameters):
            if "INSERT INTO agent_run_events" in sql and not failures:
                failures.append("admission")
                raise SqliteWriteAdmissionError("wait_timeout")
            return executemany(sql, parameters)
        conn.executemany = insert_events_once
        return conn

    monkeypatch.setattr(agent_storage, "connect", fault_connection)
    try:
        manager.append_event(run.run_id, "llm_delta", "a", payload={"delta": "a"})
        manager.append_event(run.run_id, "llm_delta", "b", payload={"delta": "b"})
        manager.flush_run_events(run.run_id)
        events = store.list_events(run.run_id)
        assert [event["sequence"] for event in events] == [1, 2]
        assert failures == ["admission"]
        assert manager._event_writer_error is None
        assert not manager._pending_run_events
    finally:
        manager.close()


def _queued_run_store(tmp_path, *, metadata=None, parent_run_id=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "unstarted.sqlite3"
    store = SqliteAgentRunStore(path)
    jobs = BackgroundJobStore(path)
    jobs.ensure_schema()
    sessions = SessionService(lambda: connect_run_store(path))
    session_id = sessions.create_session(title="Unstarted recovery").session.session_id
    created = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    record = AgentRunRecord(
        run_id="queued-orphan", session_id=session_id, trace_id="trace-orphan",
        parent_run_id=parent_run_id, status=AgentRunStatus.QUEUED,
        user_input="synthetic", created_at=created, metadata=metadata or {},
    ).model_dump(mode="json")
    store.save_run(record, updated_at=created)
    return store, jobs, session_id, record, created


def _safe_recovery_guard(store):
    @contextmanager
    def guard(_run_id):
        # Acquiring a SQLite writer here proves the caller enters the guard
        # before opening the terminalization transaction.
        with connect_run_store(store.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        yield True
    return guard


def test_unstarted_queued_orphan_fails_atomically_and_releases_slot(tmp_path):
    store, _, session_id, record, created = _queued_run_store(tmp_path)
    recovered = store.reconcile_unstarted_queued_runs(
        recovery_guard=_safe_recovery_guard(store), older_than=created, limit=5,
    )
    assert len(recovered) == 1
    assert recovered[0]["status"] == "failed"
    assert recovered[0]["error_type"] == "unstarted_dispatch_missing"
    with connect_run_store(store.db_path) as conn:
        saved = conn.execute("SELECT status FROM agent_runs WHERE run_id=?", (record["run_id"],)).fetchone()
        slot = conn.execute("SELECT run_id FROM session_admissions WHERE session_id=?", (session_id,)).fetchone()
        event = conn.execute("SELECT event_payload FROM agent_run_events WHERE run_id=?", (record["run_id"],)).fetchone()
    assert saved["status"] == "failed"
    assert slot["run_id"] is None
    assert json.loads(event["event_payload"])["type"] == "run_failed"


def test_unstarted_recovery_skips_any_dispatch_job_status(tmp_path):
    for status in ("queued", "running", "retry_wait", "succeeded", "failed", "cancelled"):
        store, jobs, _, record, created = _queued_run_store(tmp_path / status)
        job = jobs.enqueue("agent_dispatch", record["session_id"], status,
                           {"run_id": record["run_id"]})
        with connect_run_store(store.db_path) as conn:
            conn.execute("UPDATE background_jobs SET status=? WHERE job_id=?", (status, job["job_id"]))
        calls = []

        @contextmanager
        def guard(run_id, recorded=calls):
            recorded.append(run_id)
            yield True

        assert store.reconcile_unstarted_queued_runs(
            recovery_guard=guard, older_than=created,
        ) == []
        assert calls == []
        assert store.load_run(record["run_id"])["status"] == "queued"


@pytest.mark.parametrize("metadata", [
    {"completion_claimed": True}, {"pause_requested": True}, {"cancel_requested": True},
])
def test_unstarted_recovery_skips_completion_pause_cancel_and_child_state(tmp_path, metadata):
    store, _, _, record, created = _queued_run_store(tmp_path, metadata=metadata)
    called = []

    @contextmanager
    def guard(run_id):
        called.append(run_id)
        yield True

    assert store.reconcile_unstarted_queued_runs(recovery_guard=guard, older_than=created) == []
    assert called == []
    assert store.load_run(record["run_id"])["status"] == "queued"

    child_store, _, _, child, child_created = _queued_run_store(tmp_path / "child", parent_run_id="parent")
    assert child_store.reconcile_unstarted_queued_runs(
        recovery_guard=guard, older_than=child_created,
    ) == []
    assert child_store.load_run(child["run_id"])["status"] == "queued"


def test_unstarted_recovery_requires_execution_guard_and_no_tool_evidence(tmp_path):
    store, _, _, record, created = _queued_run_store(tmp_path)
    assert store.claim_tool_invocation(
        invocation_id="invocation-evidence", run_id=record["run_id"], tool_name="filesystem.read_file",
        tool_input={"path": "synthetic.txt"}, claimed_at=datetime.now(UTC).isoformat(),
    )["status"] == "claimed"

    @contextmanager
    def safe_guard(_run_id):
        yield True

    @contextmanager
    def active_guard(_run_id):
        yield False

    assert store.reconcile_unstarted_queued_runs(recovery_guard=safe_guard, older_than=created) == []
    assert store.reconcile_unstarted_queued_runs(recovery_guard=active_guard, older_than=created) == []
    assert store.load_run(record["run_id"])["status"] == "queued"


def test_unstarted_recovery_rechecks_mutations_and_rolls_back_run_event_slot(tmp_path):
    store, _, _session_id, record, created = _queued_run_store(tmp_path)

    @contextmanager
    def changed_state_guard(_run_id):
        changed = {**record, "status": "cancelled", "cancelled_at": datetime.now(UTC).isoformat()}
        store.save_run(changed, updated_at=changed["cancelled_at"])
        yield True

    assert store.reconcile_unstarted_queued_runs(
        recovery_guard=changed_state_guard, older_than=created,
    ) == []
    assert store.load_run(record["run_id"])["status"] == "cancelled"

    rollback_store, _, rollback_session, rollback_record, rollback_created = _queued_run_store(tmp_path / "rollback")
    with connect_run_store(rollback_store.db_path) as conn:
        conn.execute("""CREATE TRIGGER reject_reconcile_event BEFORE INSERT ON agent_run_events
            WHEN NEW.run_id='queued-orphan' BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic rollback"):
        rollback_store.reconcile_unstarted_queued_runs(
            recovery_guard=_safe_recovery_guard(rollback_store), older_than=rollback_created,
        )
    assert rollback_store.load_run(rollback_record["run_id"])["status"] == "queued"
    with connect_run_store(rollback_store.db_path) as conn:
        assert conn.execute("SELECT run_id FROM session_admissions WHERE session_id=?",
                            (rollback_session,)).fetchone()[0] == rollback_record["run_id"]
        assert conn.execute("SELECT 1 FROM agent_run_events WHERE run_id=?",
                            (rollback_record["run_id"],)).fetchone() is None
