"""Independent counterexamples for access-path authority and transaction fences."""
from __future__ import annotations

import asyncio
import threading
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest

from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.sessions import SessionRunPolicy
from app.domains.management import ManagementConflictError
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_tasks import manager_context as manager_context_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    generator = runtime_fixture.__wrapped__(tmp_path, monkeypatch)
    instance = next(generator)
    yield instance
    try:
        next(generator)
    except StopIteration:
        pass


def preflight(runtime, sid, **overrides):
    arguments = {"session_id": sid, "attachment_ids": [], "llm_client_name": None,
                 "llm_model": None, "llm_answer_client_name": None,
                 "llm_answer_model": None, "existing_run_id": None,
                 "llm_reasoning_level": None}
    return runtime._preflight_agent_turn(**(arguments | overrides))


@pytest.mark.parametrize("level", ["off", "low", "high", "max"])
def test_explicit_request_precedes_reused_policy(runtime, monkeypatch, level):
    sid = runtime.session_service.create_session(title="override").session.session_id
    policy = SessionRunPolicy(llm_reasoning_level="max", llm_answer_client_name="preference",
                              llm_answer_model="preference-model")
    monkeypatch.setattr(runtime.session_service, "run_policy", lambda _: policy)
    prepared, snapshot = preflight(runtime, sid, llm_answer_client_name="explicit",
                                   llm_answer_model="explicit-model", llm_reasoning_level=level)
    assert prepared[3:] == ("explicit", "explicit-model")
    assert runtime.resolve_agent_reasoning_level(
        session_id=sid, requested=level, frozen_policy=snapshot) == (level, "request")
    assert snapshot is not policy


def test_existing_run_never_reads_changed_policy(runtime, monkeypatch):
    sid = runtime.session_service.create_session(title="frozen").session.session_id
    run = runtime.create_agent_run(session_id=sid, user_input="freeze", llm_reasoning_level="high",
                                    llm_answer_client_name="frozen", llm_answer_model="frozen-model")

    def forbidden(_):
        raise AssertionError("existing-run preflight read mutable session policy")

    monkeypatch.setattr(runtime.session_service, "run_policy", forbidden)
    prepared, snapshot = preflight(runtime, sid, existing_run_id=run.run_id,
                                   llm_answer_client_name="replacement", llm_answer_model="replacement-model")
    assert snapshot is None
    assert prepared[3:] == ("frozen", "frozen-model")
    assert runtime.resolve_agent_reasoning_level(
        session_id=sid, requested="low", existing_run_id=run.run_id) == ("high", "request")


def test_async_preflight_runs_away_from_event_loop(monkeypatch):
    event_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    observed_threads = []

    def blocking_preflight(**_):
        observed_threads.append(threading.get_ident())
        entered.set()
        if not release.wait(2):
            raise AssertionError("event loop could not release synchronous policy work")
        return ([], None, None, None, None), None

    async def result(**_):
        return "finished"

    fake = SimpleNamespace(
        _preflight_agent_turn=blocking_preflight,
        resolve_agent_reasoning_level=lambda **_: ("low", "default"),
        _runner_for_turn=lambda **_: SimpleNamespace(run_async=result),
        _management_workload_for_run=lambda _: nullcontext(),
    )

    async def exercise():
        turn = asyncio.create_task(LocalKnowledgeAgentRuntime.run_agent_turn_async.__wrapped__(
            fake, session_id="session", user_input="hello"))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        release.set()
        assert await turn == "finished"

    asyncio.run(exercise())
    assert observed_threads and observed_threads[0] != event_thread


def test_missing_run_still_fences_existing_queue_relationship(runtime):
    context = manager_context_fixture.__wrapped__(runtime)
    receipt = runtime.management_queue.queue_work(
        context, command_id="orphan-queue-review", purpose="must remain held", read_only_work=True)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("DELETE FROM agent_runs WHERE run_id=?", (receipt["run_id"],))
    with pytest.raises(ManagementConflictError, match="not been released"):
        runtime.management_coordinator.check_run(receipt["run_id"])


def test_owner_cancellation_is_not_hidden_by_ordinary_run_snapshot(runtime):
    parent = runtime.management_service.create()["session"]["session_id"]
    child = runtime.management_service.create()["session"]["session_id"]
    for sid in (parent, child):
        runtime.management_service.update(sid, expected_revision=1, status="active")
    sid = runtime.session_service.create_session(title="owned task").session.session_id
    run = runtime.create_agent_run(session_id=sid, user_input="owned task")
    stamp = "2026-10-30T00:00:00+00:00"
    with closing(runtime._conn()) as conn, conn:
        conn.execute("INSERT INTO manager_delegates(delegate_id,session_id,parent_session_id,objective,"
                     "allowed_tools,workspace,kickoff_envelope,state,revision,created_at,updated_at) "
                     "VALUES('review-owner',?,?,?,'[]',NULL,'{}','active',1,?,?)",
                     (child, parent, "owned", stamp, stamp))
        conn.execute("INSERT INTO managed_session_links(manager_session_id,task_session_id,run_id,"
                     "command_id,created_at) VALUES(?,?,?,'review-owner',?)", (child, sid, run.run_id, stamp))
    runtime.management_coordinator.check_run(run.run_id)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE manager_delegates SET state='cancelled' WHERE session_id=?", (child,))
    with pytest.raises(ManagementConflictError, match="Delegate and parent"):
        runtime.management_coordinator.check_run(run.run_id)


def test_save_in_transaction_rollback_does_not_leak_admission(runtime):
    sid = runtime.session_service.create_session(title="rollback").session.session_id
    store = runtime.agent_run_manager.durable_store
    record = {"run_id": "rollback-review", "session_id": sid, "status": "queued", "user_input": "hello",
              "trace_id": "trace", "created_at": "2026-10-30", "metadata": {}, "parent_run_id": None}
    with closing(runtime._conn()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        store.save_run_in_transaction(conn, record, updated_at="2026-10-30")
        conn.rollback()
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT 1 FROM agent_runs WHERE run_id=?", (record["run_id"],)).fetchone() is None
        assert conn.execute("SELECT 1 FROM session_admissions WHERE session_id=?", (sid,)).fetchone() is None
    assert runtime.create_agent_run(session_id=sid, user_input="next").session_id == sid
