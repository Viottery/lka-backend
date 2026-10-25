"""Post-commit hints, root completion and recovery use the same durable input identity."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from contextlib import closing

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.scheduling_signals import KeyedScheduler
from app.platform.runtime_owner import RuntimeOwner
from tests.test_session_input_queue import runtime as _input_runtime
from tests.test_session_input_queue import sid, submit


@pytest.fixture(name="input_runtime")
def _runtime_fixture(tmp_path, monkeypatch):
    yield from _input_runtime.__wrapped__(tmp_path, monkeypatch)


def test_input_notification_observes_committed_message_and_receipt(input_runtime, monkeypatch):
    runtime = input_runtime
    session_id = sid(runtime)
    observed = []

    def notified(namespace, key="*"):
        if namespace == "inputs":
            with closing(runtime._conn()) as db:
                item = db.execute(
                    "SELECT message_id FROM session_inputs WHERE session_id=?", (key,)
                ).fetchone()
                assert item is not None
                assert db.execute(
                    "SELECT 1 FROM agent_session_messages WHERE message_id=?", (item[0],)
                ).fetchone()
            observed.append(key)
        return True

    monkeypatch.setattr(runtime.scheduler, "notify", notified)
    first = submit(runtime, session_id, "same")
    assert submit(runtime, session_id, "same")["input"] == first["input"]
    assert observed == [session_id, session_id]


def test_rollback_and_failed_notifier_preserve_acceptance_contract(input_runtime, monkeypatch):
    runtime = input_runtime
    session_id = sid(runtime)
    hints = []
    original = runtime.session_service.append_message_in_transaction

    def abort(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic rollback")

    monkeypatch.setattr(runtime.scheduler, "notify", lambda *a: hints.append(a))
    monkeypatch.setattr(runtime.session_service, "append_message_in_transaction", abort)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        submit(runtime, session_id, "rollback")
    assert hints == []
    with closing(runtime._conn()) as db:
        assert db.execute("SELECT count(*) FROM session_inputs").fetchone()[0] == 0
    monkeypatch.setattr(runtime.session_service, "append_message_in_transaction", original)

    def broken(*args):
        raise RuntimeError("synthetic missed signal")

    monkeypatch.setattr(runtime.scheduler, "notify", broken)
    receipt = submit(runtime, session_id, "accepted")
    assert receipt["input"]["status"] == "queued"
    assert submit(runtime, session_id, "accepted")["input"] == receipt["input"]


def test_event_queue_wakes_next_input_after_root_commit(input_runtime):
    runtime = input_runtime
    session_id = sid(runtime)
    first_started, release, finished = threading.Event(), threading.Event(), threading.Event()
    runs = []

    def execute(run, guard):
        guard.check()
        runtime.agent_run_manager.mark_running(run.run_id)
        runs.append(run.run_id)
        if len(runs) == 1:
            first_started.set()
            assert release.wait(5)
        runtime.agent_run_manager.complete_run(run.run_id, result_snapshot={"answer": "synthetic"})
        if len(runs) == 2:
            finished.set()

    runtime.agent_dispatch.execute = execute
    runtime.start()
    try:
        submit(runtime, session_id, "one")
        assert first_started.wait(5)
        submit(runtime, session_id, "two")
        release.set()
        assert finished.wait(5)
        assert len(set(runs)) == 2
        snapshot = runtime.session_inputs.snapshot(session_id)
        assert [i["run_id"] for i in snapshot["inputs"]] == runs
        assert snapshot["pending_count"] == 0
    finally:
        release.set()


def test_data_owner_refuses_second_running_backend(input_runtime):
    first = input_runtime
    second = LocalKnowledgeAgentRuntime(first.settings)
    try:
        first.start()
        with pytest.raises(RuntimeError, match="already owned"):
            second.start()
        assert first.scheduler.status()["started"]
        assert not second._runtime_started
    finally:
        second.stop()


def test_running_owner_fences_second_runtime_before_schema_initialization(input_runtime, monkeypatch):
    first = input_runtime
    first.start()

    def forbidden(_path):
        raise AssertionError('second runtime must not begin schema initialization')

    monkeypatch.setattr('app.core.runtime.init_db', forbidden)
    with pytest.raises(RuntimeError, match='already owned'):
        LocalKnowledgeAgentRuntime(first.settings)


def test_empty_input_namespace_fifteen_virtual_minutes_is_bounded(
    input_runtime, monkeypatch, record_property
):
    runtime = input_runtime
    session_id = sid(runtime)
    with closing(runtime._conn()) as db, db:
        db.execute("INSERT INTO session_input_queues(session_id) VALUES(?)", (session_id,))
    now, calls = [0.0], [0]
    condition = threading.Condition()
    scheduler = KeyedScheduler(
        reconcile_seconds=30, clock=lambda: now[0], random_source=lambda: 0.5
    )
    runtime.session_inputs.attach_scheduler(scheduler)
    # Count every SQL wrapper phase, including connection initialization PRAGMAs.
    from app.storage import sqlite_timing

    measure, counts = sqlite_timing._measure, Counter()

    def counted(database, phase, operation, statement_id, call):
        if threading.current_thread().name.startswith("keyed-scheduler"):
            counts[phase] += 1
        return measure(database, phase, operation, statement_id, call)

    monkeypatch.setattr(sqlite_timing, "_measure", counted)
    recover = runtime.session_inputs._recover_inputs

    def recovered(cursor, limit):
        result = recover(cursor, limit)
        with condition:
            calls[0] += 1
            condition.notify_all()
        return result

    scheduler._spaces["inputs"].recover = recovered
    scheduler.start()
    try:
        with condition:
            assert condition.wait_for(lambda: calls[0] >= 1, timeout=3)
        # Synchronize after result application, before moving the virtual clock.
        with scheduler._cv:
            assert scheduler._cv.wait_for(
                lambda: not scheduler._spaces["inputs"].recovery_active, timeout=3
            )
        counts.clear()
        for tick in range(1, 31):
            now[0] = tick * 30
            with scheduler._cv:
                scheduler._cv.notify_all()
            with condition:
                assert condition.wait_for(lambda tick=tick: calls[0] >= tick + 1, timeout=3)
            with scheduler._cv:
                assert scheduler._cv.wait_for(
                    lambda: not scheduler._spaces["inputs"].recovery_active, timeout=3
                )
        assert counts["connect"] <= 35
        assert counts["execute"] <= 200
        assert counts["acquire_write"] == 0
        for phase in ("connect", "execute", "acquire_write"):
            record_property(phase, counts[phase])
    finally:
        assert scheduler.stop(timeout=3)


@pytest.mark.parametrize("mode", ["poll", "shadow", "event"])
def test_scheduling_settings_accept_only_supported_modes(mode):
    assert Settings(_env_file=None, LKA_SCHEDULING_MODE=mode).scheduling_mode == mode


def test_shadow_input_observation_is_read_only_and_poll_remains_consumer(
    input_runtime, monkeypatch
):
    runtime = input_runtime
    session_id = sid(runtime)
    submit(runtime, session_id, "shadow")
    scheduler = KeyedScheduler()
    runtime.session_inputs.attach_scheduler(scheduler, mode="shadow")
    statements = []
    connect = runtime.session_inputs._conn

    def traced():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(runtime.session_inputs, "_conn", traced)
    runtime.session_inputs._recover_inputs(None, 128)
    runtime.session_inputs._scheduled_input(session_id)
    assert not any(
        sql.lstrip().upper().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE"))
        for sql in statements
    )
    assert runtime.session_inputs.snapshot(session_id)["inputs"][0]["run_id"] is None
    runtime.session_inputs.tick()
    assert runtime.session_inputs.snapshot(session_id)["inputs"][0]["run_id"] is not None


def test_dropped_hint_recovers_without_http_polling(input_runtime, monkeypatch):
    runtime = input_runtime
    session_id = sid(runtime)
    scheduler = KeyedScheduler(reconcile_seconds=0.1, random_source=lambda: 0.5)
    runtime.session_inputs.attach_scheduler(scheduler)
    completed = threading.Event()

    def execute(run, guard):
        guard.check()
        runtime.agent_run_manager.mark_running(run.run_id)
        runtime.agent_run_manager.complete_run(run.run_id)
        completed.set()

    runtime.agent_dispatch.execute = execute
    monkeypatch.setattr(runtime.session_inputs, "notify", lambda _session: None)
    scheduler.start()
    runtime.agent_dispatch.start()
    try:
        receipt = submit(runtime, session_id, "lost-wake")
        assert completed.wait(3)
        item = runtime.session_inputs.snapshot(session_id)["inputs"][0]
        assert item["input_id"] == receipt["input"]["input_id"]
        assert item["status"] == "completed"
    finally:
        assert scheduler.stop(timeout=3)


def test_stop_timeout_retains_owner_until_binding_handler_drains(input_runtime):
    runtime = input_runtime
    runtime.settings.scheduling_stop_seconds = 1
    entered, release = threading.Event(), threading.Event()

    def blocked(_key):
        entered.set()
        assert release.wait(5)

    runtime.scheduler.register("blocked-binding", blocked)
    runtime.start()
    runtime.scheduler.notify("blocked-binding")
    contender = RuntimeOwner(runtime.settings.data_dir)
    try:
        assert entered.wait(3)
        with pytest.raises(RuntimeError, match="ownership retained"):
            runtime.stop()
        assert not contender.acquire()
        assert not runtime._runtime_closed
    finally:
        release.set()
        runtime.stop()
    assert contender.acquire()
    contender.release()


def test_manager_binding_notifies_after_durable_dispatch(input_runtime, monkeypatch):
    from tests.test_management_coordinator import timer

    runtime = input_runtime
    timer(runtime)
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    observed = []

    def notified():
        with closing(runtime._conn()) as conn:
            row = conn.execute(
                "SELECT r.run_id FROM agent_runs r JOIN background_jobs j "
                "ON json_extract(j.payload_json,'$.run_id')=r.run_id "
                "WHERE j.kind='agent_dispatch'"
            ).fetchone()
            assert row is not None
            observed.append(row[0])

    monkeypatch.setattr(runtime.agent_dispatch, "notify", notified)
    assert runtime.management_coordinator.run_one()
    assert len(observed) == 1


def test_management_observes_waiting_and_terminal_edges_only(input_runtime, monkeypatch):
    runtime = input_runtime
    observed = []
    monkeypatch.setattr(runtime.management_coordinator, 'notify_run_state', lambda run: observed.append(run.status.value))
    run = runtime.create_agent_run(session_id=sid(runtime), user_input='synthetic', resumable=True)
    runtime.agent_run_manager.mark_running(run.run_id)
    assert observed == []
    runtime.agent_run_manager.mark_waiting_confirmation(run.run_id, 'synthetic-confirmation')
    runtime.agent_run_manager.complete_run(run.run_id)
    assert observed == ['waiting_confirmation', 'completed']


def test_unrelated_manager_root_does_not_trigger_management_capture(input_runtime):
    from datetime import UTC, datetime

    runtime = input_runtime
    session_id = sid(runtime)
    run = runtime.create_agent_run(session_id=session_id, user_input='synthetic', resumable=True)
    assert runtime.management_coordinator._affected_management_sessions(run.run_id) == set()
    with closing(runtime._conn()) as conn, conn:
        runtime.management_goals._event(conn, session_id, 'synthetic-event', 'timer', {}, datetime.now(UTC).isoformat())
    assert runtime.management_coordinator._affected_management_sessions(run.run_id) == {session_id}


def test_background_health_exposes_scheduling_under_control_auth(input_runtime, monkeypatch):
    from app.api.routes.memories import router

    monkeypatch.setenv('LKA_MEMORY_API_TOKEN', 'synthetic-control-token')
    app = FastAPI()
    app.state.runtime = input_runtime
    app.include_router(router)

    async def check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            assert (await client.get('/background/health')).status_code == 401
            response = await client.get('/background/health', headers={'Authorization': 'Bearer synthetic-control-token'})
            assert response.status_code == 200
            scheduling = response.json()['scheduling']
            assert scheduling['mode'] == 'event'
            assert scheduling['handler_failures'] == 0
            assert 'session_id' not in scheduling

    asyncio.run(check())
