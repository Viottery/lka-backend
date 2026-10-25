"""Offline regressions for correlated admission and responsive read transports."""
import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.routes import agent
from app.api.routes.sessions import router as sessions_router
from app.api.schemas import AgentTurnRequest
from app.core.agent_runs import AgentRunRecord, AgentRunStatus
from tests.test_management_api import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def request_for(runtime):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))


@pytest.mark.parametrize("value", ["", "space id", "x/y", "x?y", "x" * 129, "ümlaut", "x\n"])
def test_client_request_id_validation(value):
    with pytest.raises(ValidationError):
        AgentTurnRequest(user_input="Hello", client_request_id=value)


def test_admission_persists_correlation_before_any_followup_update(runtime, monkeypatch):
    persisted = []
    original = runtime.agent_run_manager._persist_run

    def capture(record):
        persisted.append(record.model_dump(mode="json"))
        return original(record)

    monkeypatch.setattr(runtime.agent_run_manager, "_persist_run", capture)
    run = runtime.create_agent_run(session_id=None, user_input="Hello", client_request_id="deviceA:request_1")
    assert persisted[0]["metadata"]["client_request_id"] == "deviceA:request_1"
    assert runtime.agent_run_store.load_run(run.run_id)["metadata"]["client_request_id"] == "deviceA:request_1"


def test_disconnect_before_first_sse_frame_still_discovers_and_starts_run(runtime, monkeypatch):
    started = []

    async def execute(**kwargs):
        started.append(kwargs["existing_run_id"])

    monkeypatch.setattr(runtime, "run_agent_turn_async", execute)
    sid = runtime.session_service.create_session(title="Chat").session.session_id

    async def exercise():
        response = await agent.stream_agent_turn(
            AgentTurnRequest(session_id=sid, user_input="Hello", client_request_id="deviceA:lost"),
            request_for(runtime),
        )
        # The transport dies before response.body_iterator is ever consumed.
        await asyncio.sleep(0)
        app = FastAPI()
        app.state.runtime = runtime
        app.include_router(sessions_router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            found = await client.get(f"/sessions/{sid}/runs", params={"client_request_id": "deviceA:lost"})
            assert found.status_code == 200
            runs = found.json()["runs"]
            assert len(runs) == 1 and runs[0]["client_request_id"] == "deviceA:lost"
            assert started == [runs[0]["run_id"]]
            other = await client.get(f"/sessions/{sid}/runs", params={"client_request_id": "deviceB:lost"})
            assert other.json()["runs"] == []
        await response.body_iterator.aclose()

    asyncio.run(exercise())


def test_exact_correlation_filter_precedes_limit_and_stays_in_session(runtime):
    sid = runtime.session_service.create_session(title="Chat").session.session_id
    first = runtime.create_agent_run(session_id=sid, user_input="First", client_request_id="A:1")
    runtime.agent_run_manager._update_run(first.run_id, status=AgentRunStatus.COMPLETED)
    second = runtime.create_agent_run(session_id=sid, user_input="Second", client_request_id="A:10")
    runtime.agent_run_manager._update_run(second.run_id, status=AgentRunStatus.COMPLETED)
    other = runtime.create_agent_run(session_id=None, user_input="Another session", client_request_id="A:1")
    found = runtime.managed_tasks.list_runs(sid, limit=1, client_request_id="A:1")
    assert [run["run_id"] for run in found["runs"]] == [first.run_id]
    assert found["next_offset"] is None
    assert other.run_id != first.run_id
    legacy = runtime.create_agent_run(session_id=sid, user_input="Legacy")
    assert "client_request_id" not in legacy.metadata
    assert "client_request_id" not in runtime.managed_tasks.list_runs(sid, limit=1)["runs"][0]


@pytest.mark.parametrize("path,slow_method", [
    ("/agent/runs/missing/events", "get_run"),
    ("/agent/runs/missing/snapshot", "get_run"),
    ("/agent/runs/missing/stream", "get_run"),
    ("/agent/safety-reviews", "list_pending_safety_reviews"),
    ("/agent/runs/missing/safety-reviews", "get_run"),
    ("/agent/safety-reviews/missing", "get_safety_review"),
    ("/agent/turn/stream", "create_agent_run"),
])
def test_slow_manager_reads_and_admission_leave_health_responsive(path, slow_method):
    entered, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(2), "event loop failed to service the health request"
        if slow_method == "create_agent_run":
            raise HTTPException(503, "Synthetic admission unavailable")
        return [] if slow_method == "list_pending_safety_reviews" else None

    manager = SimpleNamespace(**{slow_method: slow})
    runtime = SimpleNamespace(agent_run_manager=manager, create_agent_run=slow)
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(agent.router)

    @app.get("/health")
    async def health():
        release.set()
        return {"status": "ok"}

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            pending = asyncio.create_task(client.post(path, json={"user_input": "Hello"})
                                          if path.endswith("/turn/stream") else client.get(path))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                healthy = await asyncio.wait_for(client.get("/health"), 0.5)
                assert healthy.json() == {"status": "ok"}
            finally:
                release.set()
                response = await pending
                assert response.status_code in (200, 404, 503)

    asyncio.run(exercise())


def test_sse_polling_keeps_event_loop_responsive():
    entered, release = threading.Event(), threading.Event()

    def poll(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return []

    manager = SimpleNamespace(list_events=poll, get_run=lambda _: SimpleNamespace(status=AgentRunStatus.COMPLETED))
    request = request_for(SimpleNamespace(agent_run_manager=manager))

    async def connected():
        return False

    request.is_disconnected = connected

    async def exercise():
        async def consume():
            return [frame async for frame in agent._agent_turn_event_stream(request=request, run_id="r")]
        pending = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            # This callback cannot run if SQLite polling occupies the loop.
            await asyncio.wait_for(asyncio.sleep(0), 0.5)
        finally:
            release.set()
            assert await pending == []

    asyncio.run(exercise())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_nonstream_runtime_correlates_before_execution(runtime, monkeypatch, asynchronous):
    runner = runtime._runner_for_turn()
    seen = []
    sentinel = object()

    def execute(**kwargs):
        run = runtime.agent_run_store.load_run(kwargs["existing_run_id"])
        seen.append(run["metadata"]["client_request_id"])
        return sentinel

    async def execute_async(**kwargs):
        return execute(**kwargs)

    monkeypatch.setattr(runner, "run", execute)
    monkeypatch.setattr(runner, "run_async", execute_async)
    kwargs = {"session_id": None, "user_input": "Hello", "client_request_id": "deviceC:ordinary"}
    result = asyncio.run(runtime.run_agent_turn_async(**kwargs)) if asynchronous else runtime.run_agent_turn(**kwargs)
    assert result is sentinel
    assert seen == ["deviceC:ordinary"]


@pytest.mark.parametrize("graph", [False, True])
def test_admission_needs_no_post_commit_metadata_write(runtime, monkeypatch, graph):
    # Permit admission, then reject any second run write as if contention began
    # immediately after COMMIT. The accepted run must still reach dispatch.
    writes = []
    original = runtime.agent_run_store.save_run

    def save_once(record, **kwargs):
        writes.append(record)
        if len(writes) > 1:
            raise sqlite3.OperationalError("database is locked")
        return original(record, **kwargs)

    monkeypatch.setattr(runtime.agent_run_store, "save_run", save_once)
    monkeypatch.setattr(runtime, "validate_agent_answer_selection", lambda **_: None)
    started = []

    async def execute(**kwargs):
        started.append(kwargs["existing_run_id"])

    monkeypatch.setattr(runtime, "run_agent_turn_async", execute)
    if not graph:
        monkeypatch.setattr(runtime, "agent_turn_runner", runtime.agent_turn_loop)
    sid = runtime.session_service.create_session(title="Atomic admission").session.session_id
    original_policy = runtime.session_service.run_policy(sid).model_dump(mode="json")

    async def exercise():
        response = await agent.stream_agent_turn(
            AgentTurnRequest(session_id=sid, user_input="Hello", client_request_id="atomic:1",
                             resumable=graph, llm={"reasoning_level": "high",
                                                   "answer_client_name": "writer", "answer_model": "writer-v2"}),
            request_for(runtime),
        )
        await asyncio.sleep(0)
        assert len(writes) == 1
        frozen = writes[0]["metadata"]
        assert frozen["client_request_id"] == "atomic:1"
        assert frozen.get("orchestrator") == ("langgraph" if graph else None)
        assert frozen["llm_reasoning_level"] == "high"
        assert frozen["llm_reasoning_source"] == "request"
        assert frozen["llm_answer_client_name"] == "writer"
        assert frozen["llm_answer_model"] == "writer-v2"
        for key in ("llm_reasoning_level", "llm_reasoning_source", "llm_answer_client_name", "llm_answer_model"):
            assert frozen["session_policy"][key] == frozen[key]
        for key in ("allowed_tools", "read_only_tools", "require_high_risk_approval"):
            assert frozen["session_policy"].get(key) == original_policy.get(key)
        assert started == [writes[0]["run_id"]]
        assert runtime.agent_run_store.load_run(started[0])["metadata"] == frozen
        await response.body_iterator.aclose()

    asyncio.run(exercise())


def test_failed_initial_admission_does_not_reserve_session(runtime, monkeypatch):
    sid = runtime.session_service.create_session(title="Retry admission").session.session_id
    original = runtime.agent_run_store.save_run

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime.agent_run_store, "save_run", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        runtime.create_agent_run(session_id=sid, user_input="Hello", client_request_id="failed:1")
    assert runtime.managed_tasks.list_runs(sid)["runs"] == []
    monkeypatch.setattr(runtime.agent_run_store, "save_run", original)
    run = runtime.create_agent_run(session_id=sid, user_input="Hello", client_request_id="accepted:1")
    assert runtime.managed_tasks.list_runs(sid)["runs"][0]["run_id"] == run.run_id



def test_cancelled_stream_admission_retains_dispatch_ownership(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = runtime.create_agent_run
    created, started = [], []

    def slow_admission(**kwargs):
        entered.set()
        assert release.wait(3)
        run = original(**kwargs)
        created.append(run.run_id)
        return run

    async def execute(**kwargs):
        started.append(kwargs["existing_run_id"])

    monkeypatch.setattr(runtime, "create_agent_run", slow_admission)
    monkeypatch.setattr(runtime, "run_agent_turn_async", execute)
    sid = runtime.session_service.create_session(title="Cancelled transport").session.session_id

    async def exercise():
        request_task = asyncio.create_task(agent.stream_agent_turn(
            AgentTurnRequest(session_id=sid, user_input="Hello", client_request_id="cancelled:admission"),
            request_for(runtime),
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            retained = list(runtime._agent_admission_tasks)
            assert len(retained) == 1
            assert not retained[0].cancelled()
            release.set()
            await asyncio.gather(*retained)
            await asyncio.sleep(0)
            assert len(created) == 1
            assert started == created
            assert runtime._agent_admission_tasks == set()
            discovered = runtime.managed_tasks.list_runs(sid, client_request_id="cancelled:admission")["runs"]
            assert [run["run_id"] for run in discovered] == created
        finally:
            release.set()

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["cancel", "continue", "review"])
def test_slow_control_writes_leave_health_responsive(operation):
    entered, release = threading.Event(), threading.Event()
    run = AgentRunRecord(run_id="r", session_id="s", trace_id="t", user_input="Hello",
                         status=AgentRunStatus.RUNNING, created_at="now")

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(2), "control write blocked the event loop"
        if operation == "continue":
            raise ValueError("Synthetic stale answer")
        if operation == "review":
            raise KeyError("Synthetic missing review")

    manager = SimpleNamespace(get_run=lambda _: run, cancel_run=slow,
                              continue_user_question=slow, get_safety_review=lambda _: None,
                              decide_safety_review_with_transition=slow)
    app = FastAPI()
    app.state.runtime = SimpleNamespace(agent_run_manager=manager)
    app.include_router(agent.router)

    @app.get("/health")
    async def health():
        release.set()
        return {"status": "ok"}

    path, body = {
        "cancel": ("/agent/runs/r/cancel", {}),
        "continue": ("/agent/runs/r/continue", {"command_id": "answer:1", "answer": "Continue"}),
        "review": ("/agent/safety-reviews/review/decision", {"decision": "approve"}),
    }[operation]

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            pending = asyncio.create_task(client.post(path, json=body))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                healthy = await asyncio.wait_for(client.get("/health"), 0.5)
                assert healthy.json() == {"status": "ok"}
            finally:
                release.set()
                response = await pending
                assert response.status_code == {"cancel": 200, "continue": 409, "review": 404}[operation]

    asyncio.run(exercise())
