import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.core import agent_storage
from app.core.agent_dispatch import AgentDispatcher, AgentDispatchFenced
from app.core.agent_graph import AgentGraphRunner
from app.core.agent_runs import AgentRunCancelled, AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.background_jobs import BackgroundJobStore
from app.core.sessions import SessionService
from app.storage.db import connect, init_db
from app.storage.sqlite_write_queue import SqliteWriteQueueError


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "dispatch-recovery.sqlite3"
    init_db(path)
    sessions = SessionService(lambda: connect(path))
    sid = sessions.create_session().session.session_id
    manager = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(path))
    jobs = BackgroundJobStore(path)
    jobs.ensure_schema()
    dispatchers = []

    def create(**kwargs):
        dispatcher = AgentDispatcher(
            jobs, manager,
            execute=kwargs.pop("execute"),
            resume=kwargs.pop("resume", kwargs.get("execute")),
            has_checkpoint=kwargs.pop("has_checkpoint", lambda _: True),
            lease_seconds=kwargs.pop("lease_seconds", 0.3),
            poll_seconds=0.01,
            **kwargs,
        )
        dispatchers.append(dispatcher)
        return dispatcher

    yield path, sid, manager, jobs, create
    for dispatcher in dispatchers:
        dispatcher.stop()
    manager.close()


def test_transient_heartbeat_write_error_retries_while_persisted_lease_is_live(runtime, monkeypatch):
    _, sid, manager, jobs, create = runtime
    run = manager.create_run(session_id=sid, user_input="continue")
    original = jobs.heartbeat
    count = 0

    heartbeat_attempted, finished = Event(), Event()

    def execute(record, guard):
        manager.mark_running(record.run_id)
        assert heartbeat_attempted.wait(2)
        finished.wait(0.05)
        guard.check()
        manager.complete_run(record.run_id)

    def heartbeat(*args):
        nonlocal count
        count += 1
        heartbeat_attempted.set()
        if count == 1:
            raise SqliteWriteQueueError("SQLite write queue wait timed out")
        return original(*args)

    monkeypatch.setattr(jobs, "heartbeat", heartbeat)
    dispatcher = create(execute=execute, lease_seconds=0.15)
    job = dispatcher.enqueue(run.run_id)
    worker = Thread(target=dispatcher.run_one)
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    assert count >= 2
    assert manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
    assert jobs.get(job["job_id"])["status"] == "succeeded"
    assert not manager.is_cancel_requested(run.run_id)


def test_real_expiry_fences_callback_without_turning_run_into_user_cancel(runtime, monkeypatch):
    _, sid, manager, jobs, create = runtime
    run = manager.create_run(session_id=sid, user_input="continue")
    entered, fenced = Event(), Event()
    monkeypatch.setattr(jobs, "heartbeat", lambda *args: False)

    def execute(record, guard):
        manager.mark_running(record.run_id)
        entered.set()
        assert fenced.wait(2)
        with pytest.raises(AgentDispatchFenced):
            guard.check()

    dispatcher = create(execute=execute, lease_seconds=0.08)
    dispatcher.enqueue(run.run_id)
    worker = Thread(target=dispatcher.run_one)
    worker.start()
    assert entered.wait(2)
    guard = next(iter(dispatcher.guards.values()))
    assert guard.lost.wait(2)
    fenced.set()
    worker.join(3)
    assert not worker.is_alive()
    current = manager.get_run(run.run_id)
    assert current.status == AgentRunStatus.RUNNING
    assert current.metadata.get("cancel_requested") is not True


def test_changed_epoch_fences_old_owner_without_cancelling_new_owner(runtime):
    path, sid, manager, jobs, create = runtime
    run = manager.create_run(session_id=sid, user_input="continue")
    entered, release = Event(), Event()

    def execute(record, guard):
        manager.mark_running(record.run_id)
        entered.set()
        assert release.wait(2)
        with pytest.raises(AgentDispatchFenced):
            guard.check()

    dispatcher = create(execute=execute, lease_seconds=10)
    job = dispatcher.enqueue(run.run_id)
    worker = Thread(target=dispatcher.run_one)
    worker.start()
    assert entered.wait(2)
    with closing(connect(path)) as conn:
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        conn.execute("UPDATE background_jobs SET lease_expires_at=? WHERE job_id=?", (expired, job["job_id"]))
        conn.commit()
    claimed = jobs.claim("new-owner", 10, kinds=("agent_dispatch",))
    assert claimed["job_id"] == job["job_id"]
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    current_job = jobs.get(job["job_id"])
    current_run = manager.get_run(run.run_id)
    assert current_job["lease_owner"] == "new-owner"
    assert current_run.status == AgentRunStatus.RUNNING
    assert current_run.metadata.get("cancel_requested") is not True


def test_graph_fence_does_not_terminalize_run_owned_by_another_dispatch(runtime):
    _, sid, manager, _, _ = runtime
    run = manager.create_run(session_id=sid, user_input="continue")
    manager.mark_running(run.run_id)
    runner = object.__new__(AgentGraphRunner)
    runner._lock = threading.Lock()
    runner._errors = {}
    runner.turn_loop = SimpleNamespace(run_manager=manager)

    runner._fail(run.run_id, AgentDispatchFenced("dispatch_lease_lost"))

    assert manager.get_run(run.run_id).status == AgentRunStatus.RUNNING
    assert not manager.is_cancel_requested(run.run_id)


def test_graph_terminal_write_retries_transient_database_failure(runtime, monkeypatch):
    _, sid, manager, _, _ = runtime
    run = manager.create_run(session_id=sid, user_input="fail once")
    manager.mark_running(run.run_id)
    original = manager.fail_run
    calls = 0

    def fail_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SqliteWriteQueueError("SQLite write queue wait timed out")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "fail_run", fail_run)
    runner = object.__new__(AgentGraphRunner)
    runner._lock = threading.Lock()
    runner._errors = {}
    runner.turn_loop = SimpleNamespace(
        run_manager=manager,
        _mark_current_run_failed=lambda kind, message, exc=None: manager.fail_run(
            run.run_id, error_type=kind, error=message,
        ),
    )

    runner._fail(run.run_id, ValueError("execution failed"))

    assert calls == 2
    assert manager.get_run(run.run_id).status == AgentRunStatus.FAILED


def test_graph_does_not_retry_nontransient_persistence_errors(runtime):
    _, sid, manager, _, _ = runtime
    run = manager.create_run(session_id=sid, user_input="fail once")
    manager.mark_running(run.run_id)
    calls = 0

    def fail_terminal(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("invalid terminal payload")

    runner = object.__new__(AgentGraphRunner)
    runner._lock = threading.Lock()
    runner._errors = {}
    runner.turn_loop = SimpleNamespace(
        run_manager=manager,
        _mark_current_run_failed=fail_terminal,
    )

    with pytest.raises(ValueError, match="invalid terminal payload"):
        runner._fail(run.run_id, ValueError("execution failed"))

    assert calls == 1


def test_graph_retries_transient_initial_run_read(runtime, monkeypatch):
    _, sid, manager, _, _ = runtime
    run = manager.create_run(session_id=sid, user_input="read retry")
    manager.mark_running(run.run_id)
    store = manager.durable_store
    original = store.load_run
    calls = 0

    def load_run(run_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SqliteWriteQueueError("SQLite write queue wait timed out")
        return original(run_id)

    monkeypatch.setattr(store, "load_run", load_run)
    runner = object.__new__(AgentGraphRunner)
    runner._lock = threading.Lock()
    runner._errors = {}
    runner.turn_loop = SimpleNamespace(
        run_manager=manager,
        _mark_current_run_failed=lambda kind, message, exc=None: manager.fail_run(
            run.run_id, error_type=kind, error=message,
        ),
    )

    runner._fail(run.run_id, ValueError("execution failed"))

    assert calls >= 2
    assert manager.get_run(run.run_id).status == AgentRunStatus.FAILED


def test_event_writer_error_still_allows_cancelled_run_state_to_converge(runtime, monkeypatch):
    _, sid, manager, _, _ = runtime
    run = manager.create_run(session_id=sid, user_input="cancel")
    manager.mark_running(run.run_id)

    def fail_cancel(*args, **kwargs):
        raise RuntimeError("Agent event persistence failed.") from SqliteWriteQueueError("queue timed out")

    monkeypatch.setattr(manager, "cancel_run", fail_cancel)
    runner = object.__new__(AgentGraphRunner)
    runner._lock = threading.Lock()
    runner._errors = {}
    runner.turn_loop = SimpleNamespace(run_manager=manager)

    runner._fail(run.run_id, AgentRunCancelled("stopped"))

    assert manager.get_run(run.run_id).status == AgentRunStatus.CANCELLED


@pytest.mark.parametrize("initial_status", [AgentRunStatus.QUEUED, AgentRunStatus.RUNNING])
def test_failed_dispatch_orphan_reconciles_to_failed_without_replaying_tools(runtime, initial_status):
    path, sid, manager, _, create = runtime
    run = manager.create_run(session_id=sid, user_input="orphaned run")
    if initial_status == AgentRunStatus.RUNNING:
        manager.mark_running(run.run_id)
    calls = []
    dispatcher = create(execute=lambda *_: calls.append("replayed"))
    job = dispatcher.enqueue(run.run_id)
    with closing(connect(path)) as conn:
        conn.execute(
            "UPDATE background_jobs SET status='failed',error_class='database_operational_error',"
            "finished_at=?,updated_at=? WHERE job_id=?",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), job["job_id"]),
        )
        conn.commit()

    assert dispatcher.reconcile_failed_runs() == 1

    current = manager.get_run(run.run_id)
    assert current.status == AgentRunStatus.FAILED
    assert current.error_type == "dispatch_job_failed"
    assert current.metadata["dispatch_failure_job_id"] == job["job_id"]
    assert current.metadata["dispatch_failure_error_class"] == "database_operational_error"
    assert manager.list_events(run.run_id)[-1].type == "run_failed"
    assert calls == []
    assert manager.list_events(run.run_id)[-1].sequence == len(manager.list_events(run.run_id))
    with closing(connect(path)) as conn:
        assert conn.execute(
            "SELECT run_id FROM session_admissions WHERE session_id=?", (sid,),
        ).fetchone()[0] is None


@pytest.mark.parametrize("initial_status", [AgentRunStatus.QUEUED, AgentRunStatus.RUNNING])
def test_failed_dispatch_reconciliation_rolls_back_run_event_and_admission_together(runtime, initial_status):
    path, sid, manager, jobs, create = runtime
    run = manager.create_run(session_id=sid, user_input="rollback orphan reconciliation")
    if initial_status == AgentRunStatus.RUNNING:
        manager.mark_running(run.run_id)
    execute_calls = []
    dispatcher = create(execute=lambda *_: execute_calls.append("executed"))
    job = dispatcher.enqueue(run.run_id)
    now = datetime.now(UTC).isoformat()
    with closing(connect(path)) as conn:
        conn.execute(
            "UPDATE background_jobs SET status='failed',error_class='database_operational_error',"
            "finished_at=?,updated_at=? WHERE job_id=?",
            (now, now, job["job_id"]),
        )
        conn.execute(
            """CREATE TRIGGER reject_reconciled_run_failed_event
               BEFORE INSERT ON agent_run_events
               WHEN json_extract(NEW.event_payload, '$.type')='run_failed'
               BEGIN SELECT RAISE(ABORT, 'injected reconciliation event failure'); END"""
        )
        conn.commit()

    before_events = manager.list_events(run.run_id)
    assert jobs.get(job["job_id"])["status"] == "failed"
    with pytest.raises(sqlite3.IntegrityError, match="injected reconciliation event failure"):
        dispatcher.reconcile_failed_runs()

    persisted = manager.durable_store.load_run(run.run_id)
    assert persisted["status"] == initial_status.value
    assert manager.list_events(run.run_id) == before_events
    assert not any(event.type == "run_failed" for event in manager.list_events(run.run_id))
    assert jobs.get(job["job_id"])["status"] == "failed"
    with closing(connect(path)) as conn:
        assert conn.execute(
            "SELECT run_id FROM session_admissions WHERE session_id=?", (sid,),
        ).fetchone()[0] == run.run_id
    assert execute_calls == []


@pytest.mark.parametrize("protection", ["parent", "result", "completion", "pause"])
def test_failed_queued_dispatch_keeps_existing_reconciliation_guards(runtime, protection):
    path, sid, manager, jobs, create = runtime
    parent = None
    if protection == "parent":
        parent = manager.create_run(session_id=sid, user_input="parent")
        run = manager.create_run(session_id=sid, user_input="child", parent_run_id=parent.run_id)
    else:
        run = manager.create_run(session_id=sid, user_input=f"protected queued {protection}")

    if protection != "parent":
        record = manager.durable_store.load_run(run.run_id)
        if protection == "result":
            record["result_snapshot"] = {"answer": "already published"}
        elif protection == "completion":
            record["metadata"]["completion_claimed"] = True
        else:
            record["metadata"]["pause_requested"] = True
        now = datetime.now(UTC).isoformat()
        with closing(connect(path)) as conn:
            conn.execute(
                "UPDATE agent_runs SET record_payload=?,updated_at=? WHERE run_id=?",
                (agent_storage._json_dump(record), now, run.run_id),
            )
            conn.commit()

    dispatcher = create(execute=lambda *_: pytest.fail("reconciliation must not replay work"))
    job = jobs.enqueue(
        "agent_dispatch", sid, f"protected-{protection}",
        {"run_id": run.run_id, "mode": "execute"},
    )
    now = datetime.now(UTC).isoformat()
    with closing(connect(path)) as conn:
        conn.execute(
            "UPDATE background_jobs SET status='failed',error_class='database_operational_error',"
            "finished_at=?,updated_at=? WHERE job_id=?",
            (now, now, job["job_id"]),
        )
        conn.commit()

    assert dispatcher.reconcile_failed_runs() == 0
    persisted = manager.durable_store.load_run(run.run_id)
    assert persisted["status"] == AgentRunStatus.QUEUED.value
    assert not any(event.type == "run_failed" for event in manager.list_events(run.run_id))
    if parent is not None:
        with closing(connect(path)) as conn:
            assert conn.execute(
                "SELECT run_id FROM session_admissions WHERE session_id=?", (sid,),
            ).fetchone()[0] == parent.run_id
    else:
        with closing(connect(path)) as conn:
            assert conn.execute(
                "SELECT run_id FROM session_admissions WHERE session_id=?", (sid,),
            ).fetchone()[0] == run.run_id


def test_failed_queued_dispatch_is_not_reconciled_while_any_job_is_active(runtime):
    path, sid, manager, jobs, create = runtime
    run = manager.create_run(session_id=sid, user_input="active retry beside failed latest")
    dispatcher = create(execute=lambda *_: pytest.fail("reconciliation must not replay work"))
    active_job = dispatcher.enqueue(run.run_id, command_id="older-active-job")
    failed_latest = dispatcher.enqueue(run.run_id, command_id="newer-failed-job")
    now = datetime.now(UTC).isoformat()
    older = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    newer = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with closing(connect(path)) as conn:
        conn.execute(
            "UPDATE background_jobs SET created_at=? WHERE job_id=?",
            (older, active_job["job_id"]),
        )
        conn.execute(
            "UPDATE background_jobs SET status='failed',error_class='database_operational_error',"
            "created_at=?,finished_at=?,updated_at=? WHERE job_id=?",
            (newer, now, now, failed_latest["job_id"]),
        )
        conn.commit()

    assert jobs.get(active_job["job_id"])["status"] == "queued"
    assert dispatcher.reconcile_failed_runs() == 0
    assert manager.durable_store.load_run(run.run_id)["status"] == AgentRunStatus.QUEUED.value
    with closing(connect(path)) as conn:
        assert conn.execute(
            "SELECT run_id FROM session_admissions WHERE session_id=?", (sid,),
        ).fetchone()[0] == run.run_id


def test_reconciliation_preserves_paused_completed_and_newer_queued_runs(runtime):
    path, _, manager, _, create = runtime
    sessions = SessionService(lambda: connect(path))
    dispatcher = create(execute=lambda *_: None)
    runs = []
    for index in range(4):
        session_id = sessions.create_session().session.session_id
        run = manager.create_run(session_id=session_id, user_input=f"protected {index}")
        runs.append(run)
    manager._update_run(runs[0].run_id, status=AgentRunStatus.PAUSED)
    manager.complete_run(runs[1].run_id)
    manager.mark_running(runs[2].run_id)

    failed_jobs = []
    for index, run in enumerate(runs):
        failed_jobs.append(dispatcher.enqueue(run.run_id, command_id=f"failed-{index}"))
    # A newer queued owner for the running run prevents reconciliation.
    dispatcher.enqueue(runs[2].run_id, command_id="newer-active")
    # The same latest-job guard applies when the orphan itself never entered running.
    dispatcher.enqueue(runs[3].run_id, command_id="newer-queued-active")
    with closing(connect(path)) as conn:
        now = datetime.now(UTC).isoformat()
        conn.executemany(
            "UPDATE background_jobs SET status='failed',error_class='database_operational_error',"
            "finished_at=?,updated_at=? WHERE job_id=?",
            [(now, now, job["job_id"]) for job in failed_jobs],
        )
        conn.commit()

    assert dispatcher.reconcile_failed_runs() == 0
    assert manager.get_run(runs[0].run_id).status == AgentRunStatus.PAUSED
    assert manager.get_run(runs[1].run_id).status == AgentRunStatus.COMPLETED
    assert manager.get_run(runs[2].run_id).status == AgentRunStatus.RUNNING
    assert manager.get_run(runs[3].run_id).status == AgentRunStatus.QUEUED
    with closing(connect(path)) as conn:
        assert conn.execute(
            "SELECT run_id FROM session_admissions WHERE session_id=?", (runs[3].session_id,),
        ).fetchone()[0] == runs[3].run_id


def test_reconciliation_finds_old_orphan_beyond_many_terminal_failed_jobs(runtime):
    _, sid, manager, _, create = runtime
    sessions = SessionService(lambda: connect(manager.durable_store.db_path))
    orphan = manager.create_run(session_id=sid, user_input="older orphan")
    manager.mark_running(orphan.run_id)
    dispatcher = create(execute=lambda *_: None)
    orphan_job = dispatcher.enqueue(orphan.run_id, command_id="orphan-failed")

    terminal_session = sessions.create_session().session.session_id
    terminal = manager.create_run(session_id=terminal_session, user_input="terminal history")
    manager.fail_run(terminal.run_id, error_type="prior_failure", error="already terminal")
    terminal_jobs = [
        dispatcher.enqueue(terminal.run_id, command_id=f"terminal-history-{index}")
        for index in range(85)
    ]
    now = datetime.now(UTC).isoformat()
    with closing(connect(manager.durable_store.db_path)) as conn:
        conn.executemany(
            "UPDATE background_jobs SET status='failed',error_class='database_operational_error',"
            "finished_at=?,updated_at=? WHERE job_id=?",
            [(now, now, job["job_id"]) for job in [orphan_job, *terminal_jobs]],
        )
        conn.commit()

    assert manager.durable_store.reconcile_failed_dispatch_runs(limit=1)[0]["run_id"] == orphan.run_id
    manager.refresh_after_transaction(manager.durable_store.load_run(orphan.run_id))
    assert manager.get_run(orphan.run_id).status == AgentRunStatus.FAILED


def test_empty_reconciliation_does_not_begin_write_transaction(runtime, monkeypatch):
    _, _, manager, _, _ = runtime
    original_connect = agent_storage.connect

    def connect_without_transactions(path):
        conn = original_connect(path)

        def authorize(action, arg1, arg2, database, source):
            del arg1, arg2, database, source
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION else sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(agent_storage, "connect", connect_without_transactions)
    assert manager.durable_store.reconcile_failed_dispatch_runs() == []


def test_startup_checkpoint_failure_terminalizes_and_releases_session(runtime):
    path, sid, manager, _, _ = runtime
    run = manager.create_run(session_id=sid, user_input='accepted before saver failed')
    runner = object.__new__(AgentGraphRunner)
    runner.turn_loop = SimpleNamespace(run_manager=manager)
    runner._execution_locks_guard = threading.Lock()
    runner._execution_locks = {}
    runner._resolve_run_id = lambda **_: run.run_id
    runner._invoke_sync = lambda **_: (_ for _ in ()).throw(TimeoutError('checkpoint capacity'))
    with pytest.raises(TimeoutError):
        runner.run(existing_run_id=run.run_id)
    with closing(connect(path)) as conn:
        assert conn.execute('SELECT status FROM agent_runs WHERE run_id=?', (run.run_id,)).fetchone()[0] == 'failed'
        assert conn.execute('SELECT run_id FROM session_admissions WHERE session_id=?', (sid,)).fetchone()[0] is None
    assert manager.get_run(run.run_id).error_type == 'execution_start_failed'
    assert [e.type for e in manager.list_events(run.run_id)].count('run_failed') == 1
    assert manager.create_run(session_id=sid, user_input='next request').run_id != run.run_id


def test_dispatch_capacity_is_reserved_before_claim_and_interactive_has_headroom(runtime):
    from app.core.execution_admission import ExecutionAdmission

    path, sid, manager, jobs, create = runtime
    governor = ExecutionAdmission(total=2, interactive_reserved=1)
    held = governor.try_acquire('background')
    background = manager.create_run(session_id=sid, user_input='background work')
    other_sid = SessionService(lambda: connect(path)).create_session().session.session_id
    interactive = manager.create_run(session_id=other_sid, user_input='real user',
                                     metadata={'session_input_id': 'trusted-server-binding'})
    seen = []

    def execute(record, guard):
        seen.append(record.run_id)
        assert governor.snapshot()['interactive'] == 1
        manager.mark_running(record.run_id)
        manager.complete_run(record.run_id)

    dispatcher = create(execute=execute, execution_admission=governor)
    background_job = dispatcher.enqueue(background.run_id)
    foreground_job = dispatcher.enqueue(interactive.run_id)
    assert dispatcher.run_one()
    assert seen == [interactive.run_id]
    assert jobs.get(foreground_job['job_id'])['status'] == 'succeeded'
    assert jobs.get(background_job['job_id'])['attempts'] == 0
    assert dispatcher.run_one() is False
    assert jobs.get(background_job['job_id'])['lease_owner'] is None
    held.close()
    dispatcher.execute = lambda record, guard: (manager.mark_running(record.run_id), manager.complete_run(record.run_id))
    assert dispatcher.run_one()
    assert governor.snapshot()['background'] == 0


def test_empty_governed_dispatch_does_not_make_an_unclassified_claim(runtime, monkeypatch):
    from app.core.execution_admission import ExecutionAdmission

    _, _, _, jobs, create = runtime
    dispatcher = create(execute=lambda *_: None, execution_admission=ExecutionAdmission())
    monkeypatch.setattr(jobs, 'claim', lambda *args, **kwargs: pytest.fail('unclassified empty-scan claim'))
    assert dispatcher.run_one() is False


def test_deadline_reconciliation_releases_slot_even_when_execution_is_full(runtime):
    from app.core.execution_admission import ExecutionAdmission

    path, sid, manager, jobs, create = runtime
    governor = ExecutionAdmission()
    held = [governor.try_acquire() for _ in range(4)]
    run = manager.create_run(session_id=sid, user_input='expired before execution')
    dispatcher = create(execute=lambda *_: pytest.fail('expired job executed'), execution_admission=governor)
    job = dispatcher.enqueue(run.run_id)
    with closing(connect(path)) as conn:
        conn.execute('UPDATE background_jobs SET deadline=? WHERE job_id=?',
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job['job_id']))
        conn.commit()
    try:
        assert dispatcher.reconcile_failed_runs() == 1
        assert manager.get_run(run.run_id).status == AgentRunStatus.FAILED
        assert jobs.get(job['job_id'])['attempts'] == 0
        assert manager.create_run(session_id=sid, user_input='next').run_id != run.run_id
    finally:
        for lease in held:
            lease.close()
