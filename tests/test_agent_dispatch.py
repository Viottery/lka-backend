"""Durable dispatch uses disposable SQLite state and injected offline callbacks."""
from contextlib import closing
from threading import Event, Thread

import pytest

from app.core.agent_dispatch import AgentDispatcher, AgentDispatchLeaseLost
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.background_jobs import BackgroundJobFailure, BackgroundJobStore
from app.core.llm_workloads import BackgroundBudgetDeferred
from app.core.sessions import SessionService
from app.storage.db import connect, init_db


@pytest.fixture
def dispatch_state(tmp_path):
    path = tmp_path / "dispatch.sqlite3"
    init_db(path)
    sessions = SessionService(lambda: connect(path))
    sid = sessions.create_session().session.session_id
    manager = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(path))
    queue = BackgroundJobStore(path)
    queue.ensure_schema()
    dispatchers = []

    def make(**overrides):
        calls = []

        def complete(run, guard):
            guard.check()
            calls.append(run.run_id)
            manager.mark_running(run.run_id)
            manager.complete_run(run.run_id)

        options = {"execute": complete, "resume": complete, "has_checkpoint": lambda _: False}
        options.update(overrides)
        dispatcher = AgentDispatcher(queue, manager, **options)
        dispatchers.append(dispatcher)
        return dispatcher, calls

    yield path, sid, manager, queue, make
    for dispatcher in dispatchers:
        dispatcher.stop()
    manager.close()


def test_queued_dispatch_is_durable_and_idempotent(dispatch_state):
    path, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="A persisted task")
    dispatcher, calls = make()
    job = dispatcher.enqueue(run.run_id)
    assert dispatcher.enqueue(run.run_id)["job_id"] == job["job_id"]
    restarted_queue = BackgroundJobStore(path)
    assert restarted_queue.get(job["job_id"], include_payload=True)["payload"] == {
        "run_id": run.run_id, "mode": "execute",
    }
    assert calls == []
    assert dispatcher.run_one()
    assert calls == [run.run_id]
    assert queue.get(job["job_id"])["status"] == "succeeded"
    assert manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
    assert dispatcher.run_one() is False


def test_dispatch_enqueue_can_join_caller_transaction_and_roll_back(dispatch_state):
    path, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    dispatcher, _ = make()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = dispatcher.enqueue(run.run_id, conn=conn)
        conn.rollback()
    assert queue.get(job["job_id"]) is None
    job = dispatcher.enqueue(run.run_id, command_id="same-command")
    with pytest.raises(ValueError, match="different request"):
        dispatcher.enqueue(run.run_id, mode="resume", command_id="same-command")
    assert queue.get(job["job_id"], include_payload=True)["payload"]["mode"] == "execute"


@pytest.mark.parametrize("status", ["paused", "waiting_user", "waiting_confirmation", "completed", "failed", "cancelled", "timed_out"])
def test_normal_dispatch_never_restarts_paused_waiting_or_terminal_run(dispatch_state, status):
    _, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    manager._update_run(run.run_id, status=AgentRunStatus(status))
    dispatcher, calls = make(has_checkpoint=lambda _: True)
    job = dispatcher.enqueue(run.run_id)
    assert dispatcher.run_one()
    assert calls == []
    assert manager.get_run(run.run_id).status.value == status
    assert queue.get(job["job_id"])["status"] == "succeeded"


@pytest.mark.parametrize("checkpoint", [True, False])
def test_running_after_crash_resumes_only_with_checkpoint(dispatch_state, checkpoint):
    _, sid, manager, _, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    manager.mark_running(run.run_id)
    execute_calls, resume_calls = [], []

    def resume(record, guard):
        guard.check()
        resume_calls.append(record.run_id)
        manager.complete_run(record.run_id)

    dispatcher, _ = make(execute=lambda *args: execute_calls.append(args), resume=resume,
                         has_checkpoint=lambda _: checkpoint)
    dispatcher.enqueue(run.run_id)
    assert dispatcher.run_one()
    assert execute_calls == []
    current = manager.get_run(run.run_id)
    if checkpoint:
        assert resume_calls == [run.run_id]
        assert current.status == AgentRunStatus.COMPLETED
    else:
        assert resume_calls == []
        assert current.status == AgentRunStatus.FAILED
        assert current.error_type == "execution_uncertain"


@pytest.mark.parametrize("status", ["paused", "waiting_user", "waiting_confirmation"])
def test_explicit_resume_requires_original_waiting_workflow(dispatch_state, status):
    _, sid, manager, _, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    manager._update_run(run.run_id, status=AgentRunStatus(status))
    dispatcher, calls = make(has_checkpoint=lambda _: True)
    dispatcher.enqueue(run.run_id, mode="resume", command_id="manual-resume")
    assert dispatcher.run_one()
    assert calls == ([run.run_id] if status == "paused" else [])


def test_guard_loss_prevents_later_effect_and_requests_durable_cancel(dispatch_state):
    _, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    effects = []

    def execute(record, guard):
        manager.mark_running(record.run_id)
        effects.append("before")
        queue.cancel(guard.job["job_id"])
        dispatcher.check_run(record.run_id)
        effects.append("must-not-run")

    dispatcher, _ = make(execute=execute)
    dispatcher.enqueue(run.run_id)
    assert dispatcher.run_one()
    assert effects == ["before"]
    assert manager.is_cancel_requested(run.run_id)
    assert manager.get_run(run.run_id).metadata["cancel_reason"] == "dispatch_lease_lost"
    assert dispatcher.guards == {}
    dispatcher.check_run("unrelated-http-run")


def test_heartbeat_loss_fences_inflight_callback(dispatch_state, monkeypatch):
    _, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    lost = []

    def execute(record, guard):
        manager.mark_running(record.run_id)
        assert guard.lost.wait(2), "heartbeat did not report lease loss"
        with pytest.raises(AgentDispatchLeaseLost):
            guard.check()
        lost.append(True)

    monkeypatch.setattr(queue, "heartbeat", lambda *args: False)
    dispatcher, _ = make(execute=execute, lease_seconds=0.1)
    dispatcher.enqueue(run.run_id)
    assert dispatcher.run_one()
    assert lost == [True]
    assert manager.is_cancel_requested(run.run_id)


@pytest.mark.parametrize("pool", [False, True])
def test_stop_drains_active_handler_without_cancelling_or_closing_stores(dispatch_state, pool):
    _, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    entered, release, stopping, stopped = Event(), Event(), Event(), Event()

    def execute(record, guard):
        manager.mark_running(record.run_id)
        entered.set()
        assert release.wait(3), "test failed to release handler"
        guard.check()  # Shutdown itself must not invalidate the execution.
        manager.complete_run(record.run_id)

    dispatcher, _ = make(execute=execute, request_pause=lambda _: stopping.set(), poll_seconds=0.01)
    job = dispatcher.enqueue(run.run_id)
    if pool:
        dispatcher.start()
        running = None
    else:
        running = Thread(target=dispatcher.run_one)
        running.start()
    assert entered.wait(2)

    def stop():
        dispatcher.stop()
        stopped.set()

    stopper = Thread(target=stop)
    stopper.start()
    try:
        assert stopping.wait(2)
        assert not stopped.is_set()
    finally:
        release.set()
        stopper.join(3)
        if running:
            running.join(3)
    assert stopped.is_set()
    assert not manager.is_cancel_requested(run.run_id)
    assert manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
    assert queue.get(job["job_id"])["status"] == "succeeded"
    assert dispatcher.run_one() is False


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_real_harness_guard_blocks_late_model_result_before_publication(tmp_path, monkeypatch, orchestrator):
    from tests.test_unified_entry_quality import operation, runtime_for

    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("final_answer", reason="Direct reply."), "MUST_NOT_BE_PUBLISHED",
    ])
    queue = BackgroundJobStore(runtime.db_path)
    queue.ensure_schema()
    run = runtime.create_agent_run(session_id="guarded", user_input="Hello")
    dispatcher = AgentDispatcher(
        queue, runtime.agent_run_manager,
        execute=lambda record, guard: runtime.run_agent_turn(session_id=record.session_id,
                                                             user_input=record.user_input,
                                                             existing_run_id=record.run_id),
        resume=lambda *_: pytest.fail("Unexpected resume"), has_checkpoint=lambda _: False,
    )
    runtime.agent_turn_loop.execution_guard = dispatcher.check_run
    job = dispatcher.enqueue(run.run_id)
    complete = client.complete_text

    def lose_lease_after_response(**kwargs):
        result = complete(**kwargs)
        queue.cancel(job["job_id"])
        return result

    monkeypatch.setattr(client, "complete_text", lose_lease_after_response)
    try:
        assert dispatcher.run_one()
        assert runtime.agent_run_manager.is_cancel_requested(run.run_id)
        assert not any(message.role == "agent" for message in runtime.session_service.get_session(
            session_id=run.session_id).messages)
        assert not any(event.type in {"final_answer", "run_completed"}
                       for event in runtime.agent_run_manager.list_events(run.run_id))
    finally:
        dispatcher.stop()
        runtime.stop()


def test_transaction_guard_is_read_only_and_invalid_lease_does_not_self_lock(dispatch_state, monkeypatch):
    path, sid, manager, _, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    checks = []

    def execute(record, guard):
        manager.mark_running(record.run_id)
        with closing(connect(path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            dispatcher.check_run(record.run_id, conn=conn)
            conn.execute("UPDATE background_jobs SET lease_epoch=lease_epoch+1 WHERE job_id=?",
                         (guard.job["job_id"],))
            # Calling request_cancel here would need a second write connection
            # and deadlock behind this transaction. It must never be attempted.
            with monkeypatch.context() as local_patch:
                local_patch.setattr(manager, "request_cancel", lambda *args, **kwargs: pytest.fail(
                    "Transaction guard attempted a database write"))
                with pytest.raises(AgentDispatchLeaseLost):
                    dispatcher.check_run(record.run_id, conn=conn)
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            assert not guard.lost.is_set()
            conn.rollback()
        guard.check()  # Rolled-back provisional changes do not poison the live guard.
        checks.append(True)
        manager.complete_run(record.run_id)

    dispatcher, _ = make(execute=execute)
    dispatcher.enqueue(run.run_id)
    assert dispatcher.run_one()
    assert checks == [True]
    assert not manager.is_cancel_requested(run.run_id)


def test_stop_tolerates_task_finishing_between_snapshot_and_pause_request(dispatch_state):
    _, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Task")
    entered, release = Event(), Event()
    pauses = []

    def execute(record, guard):
        manager.mark_running(record.run_id)
        entered.set()
        assert release.wait(3), "test failed to release finishing task"
        guard.check()

    def pause_after_completion(record):
        assert record.status == AgentRunStatus.RUNNING
        # The provider finishes after stop's running snapshot, just before the
        # pause command reaches the graph. Its terminal-state conflict is benign.
        manager.complete_run(record.run_id)
        pauses.append(record.run_id)
        release.set()
        raise ValueError("Terminal runs cannot be paused")

    dispatcher, _ = make(execute=execute, request_pause=pause_after_completion)
    job = dispatcher.enqueue(run.run_id)
    running = Thread(target=dispatcher.run_one)
    running.start()
    try:
        assert entered.wait(2)
        dispatcher.stop()
    finally:
        release.set()
        running.join(3)
    assert not running.is_alive()
    assert pauses == [run.run_id]
    assert manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
    assert not manager.is_cancel_requested(run.run_id)
    assert queue.get(job["job_id"])["status"] == "succeeded"


def test_child_inherits_ancestor_dispatch_guard_in_and_outside_transaction(dispatch_state):
    path, sid, manager, queue, make = dispatch_state
    root = manager.create_run(session_id=sid, user_input="Root")
    child = manager.create_run(session_id=sid, user_input="Child", parent_run_id=root.run_id)
    grandchild = manager.create_run(session_id=sid, user_input="Grandchild", parent_run_id=child.run_id)
    checked = []

    def execute(record, guard):
        manager.mark_running(record.run_id)
        # The child lookup must traverse the same transaction and validate the
        # ancestor without trying to persist cancellation behind its write lock.
        with closing(connect(path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            dispatcher.check_run(grandchild.run_id, conn=conn)
            conn.execute("UPDATE background_jobs SET lease_epoch=lease_epoch+1 WHERE job_id=?",
                         (guard.job["job_id"],))
            with pytest.raises(AgentDispatchLeaseLost):
                dispatcher.check_run(grandchild.run_id, conn=conn)
            assert not guard.lost.is_set()
            conn.rollback()
        queue.cancel(guard.job["job_id"])
        # No scheduler poll is needed: even a nested child sees the root's
        # revoked lease at its next tool/node/publication guard.
        with pytest.raises(AgentDispatchLeaseLost):
            dispatcher.check_run(grandchild.run_id)
        checked.append(True)

    dispatcher, _ = make(execute=execute)
    dispatcher.enqueue(root.run_id)
    assert dispatcher.run_one()
    assert checked == [True]
    assert manager.is_cancel_requested(root.run_id)
    dispatcher.check_run("ordinary-root-without-dispatch")


def test_default_dispatch_starts_independent_runs_and_stop_pauses_and_drains_all(dispatch_state):
    path, _, manager, queue, make = dispatch_state
    sessions = SessionService(lambda: connect(path))
    runs = [manager.create_run(session_id=sessions.create_session().session.session_id,
                               user_input=f"Independent task {index}") for index in range(4)]
    entered = {run.run_id: Event() for run in runs}
    paused = {run.run_id: Event() for run in runs}
    release, stopped = Event(), Event()

    def execute(record, guard):
        manager.mark_running(record.run_id)
        entered[record.run_id].set()
        assert release.wait(4), "test failed to release parallel callbacks"
        guard.check()
        assert paused[record.run_id].is_set()
        manager._update_run(record.run_id, status=AgentRunStatus.PAUSED)

    def pause(record):
        paused[record.run_id].set()

    dispatcher, _ = make(execute=execute, request_pause=pause, poll_seconds=0.01)
    jobs = [dispatcher.enqueue(run.run_id) for run in runs]
    dispatcher.start()
    dispatcher.start()  # Repeated starts cannot create duplicate claim pumps.
    stopper = None
    try:
        assert all(event.wait(2) for event in entered.values()), "default dispatch serialized independent jobs"
        assert all(queue.get(job["job_id"])["status"] == "running" for job in jobs)
        stopper = Thread(target=lambda: (dispatcher.stop(), stopped.set()))
        stopper.start()
        assert all(event.wait(2) for event in paused.values())
        assert not stopped.is_set()
    finally:
        release.set()
        if stopper:
            stopper.join(4)
    assert stopped.is_set()
    assert all(manager.get_run(run.run_id).status == AgentRunStatus.PAUSED for run in runs)
    assert all(not manager.is_cancel_requested(run.run_id) for run in runs)
    assert all(queue.get(job["job_id"])["status"] == "succeeded" for job in jobs)
    assert dispatcher.guards == {}


def test_shutdown_pauses_handler_that_enters_after_stop_snapshot(dispatch_state):
    _, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Claimed just before shutdown")
    claimed, release_handler, paused, stopped = Event(), Event(), Event(), Event()

    def execute(record, guard):
        assert paused.is_set(), "late handler missed shutdown pause"
        guard.check()
        manager._update_run(record.run_id, status=AgentRunStatus.PAUSED)

    dispatcher, _ = make(execute=execute, request_pause=lambda _: paused.set(), poll_seconds=0.01)
    handler = dispatcher.worker.handlers["agent_dispatch"]

    def delayed_handler(job):
        claimed.set()
        assert release_handler.wait(4)
        handler(job)

    dispatcher.worker.handlers["agent_dispatch"] = delayed_handler
    job = dispatcher.enqueue(run.run_id)
    dispatcher.start()
    assert claimed.wait(2)
    stopper = Thread(target=lambda: (dispatcher.stop(), stopped.set()))
    stopper.start()
    try:
        assert dispatcher._stopping.wait(2)
        assert not stopped.is_set()
    finally:
        release_handler.set()
        stopper.join(4)
    assert stopped.is_set() and paused.is_set()
    assert queue.get(job["job_id"])["status"] == "succeeded"


def test_parallel_dispatch_heartbeat_loss_fences_each_execution(dispatch_state, monkeypatch):
    path, _, manager, queue, make = dispatch_state
    sessions = SessionService(lambda: connect(path))
    runs = [manager.create_run(session_id=sessions.create_session().session.session_id,
                               user_input=f"Lease loss {index}") for index in range(2)]
    entered = {run.run_id: Event() for run in runs}
    fenced = {run.run_id: Event() for run in runs}
    release = Event()

    def execute(record, guard):
        manager.mark_running(record.run_id)
        entered[record.run_id].set()
        assert release.wait(4)
        assert guard.lost.wait(2)
        with pytest.raises(AgentDispatchLeaseLost):
            guard.check()
        fenced[record.run_id].set()

    monkeypatch.setattr(queue, "heartbeat", lambda *args: False)
    dispatcher, _ = make(execute=execute, lease_seconds=0.2, poll_seconds=0.01)
    jobs = [dispatcher.enqueue(run.run_id) for run in runs]
    dispatcher.start()
    try:
        assert all(event.wait(2) for event in entered.values())
        release.set()
        assert all(event.wait(2) for event in fenced.values())
    finally:
        release.set()
        dispatcher.stop()
    assert all(manager.is_cancel_requested(run.run_id) for run in runs)
    assert all(queue.get(job["job_id"])["status"] != "succeeded" for job in jobs)


@pytest.mark.parametrize("error,status,error_class", [
    (BackgroundJobFailure("dispatch_provider_busy", retryable=True), "retry_wait", "dispatch_provider_busy"),
    (BackgroundJobFailure("dispatch_invalid", retryable=False), "failed", "dispatch_invalid"),
    (BackgroundBudgetDeferred("wait for budget"), "retry_wait", "background_budget_deferred"),
])
def test_dynamic_dispatch_preserves_shared_worker_failure_outcomes(dispatch_state, monkeypatch,
                                                                  error, status, error_class):
    _, sid, manager, queue, make = dispatch_state
    run = manager.create_run(session_id=sid, user_input="Failure classification")
    recorded = Event()

    def execute(*_):
        raise error

    method_name = "defer" if isinstance(error, BackgroundBudgetDeferred) else "fail"
    original = getattr(queue, method_name)

    def outcome(*args, **kwargs):
        result = original(*args, **kwargs)
        recorded.set()
        return result

    monkeypatch.setattr(queue, method_name, outcome)
    dispatcher, _ = make(execute=execute, poll_seconds=0.01)
    job = dispatcher.enqueue(run.run_id)
    dispatcher.start()
    try:
        assert recorded.wait(2)
    finally:
        dispatcher.stop()
    current = queue.get(job["job_id"])
    assert current["status"] == status
    assert current["error_class"] == error_class
    assert current["lease_owner"] is None
