"""Isolated real graph/tool/SQLite controls; no provider network or daily service."""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.routes.agent import (
    _agent_turn_event_stream,
    interrupt_agent_run,
    replace_agent_run,
    resume_agent_run,
    router,
    stream_agent_turn,
)
from app.api.schemas import AgentRunResponse, AgentTurnRequest, ReplaceAgentRunRequest
from app.core.agent_graph import AgentTurnPaused, AgentTurnWaitingForConfirmation
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewQueueConflict
from tests.test_unified_entry_quality import ScriptedClient, operation, runtime_for


def request_for(runtime):
    async def connected():
        return False
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
                           is_disconnected=connected)


def run_existing(runtime, run):
    return runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input,
                                  existing_run_id=run.run_id)


def pause_after_write(tmp_path, monkeypatch, *, orchestrator="langgraph", resumable=False):
    path = tmp_path / "created.txt"
    path.write_text("BEFORE_WRITE", encoding="utf-8")
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.edit_file",
                  tool_input={"path": str(path),
                              "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "edits": [{"old_text": "BEFORE_WRITE", "new_text": "WRITE_ONCE"}]}),
        operation("final_answer", reason="Write completed."), "File updated.",
    ])
    run = runtime.create_agent_run(session_id="isolated", user_input="Create the requested file.", resumable=resumable)
    tool = runtime.tool_registry.get_tool("filesystem.edit_file")
    original = tool.invoke
    calls = []

    def write_and_pause(**kwargs):
        result = original(**kwargs)
        calls.append(result)
        runtime.interrupt_agent_run(run.run_id)
        return result

    monkeypatch.setattr(tool, "invoke", write_and_pause)
    with pytest.raises(AgentTurnPaused):
        run_existing(runtime, run)
    return runtime, client, run, path, calls


def test_completed_write_is_checkpointed_and_not_replayed(tmp_path, monkeypatch):
    runtime, client, run, path, calls = pause_after_write(tmp_path, monkeypatch)
    try:
        paused = runtime.agent_run_manager.get_run(run.run_id)
        assert paused.status == AgentRunStatus.PAUSED
        assert path.read_text() == "WRITE_ONCE"
        assert runtime.agent_turn_runner.get_state(run.run_id).values["phase"] == "tool_executed"
        assert len(client.calls) == 2
        result = runtime.resume_agent_run(run.run_id)
        assert result.answer == "File updated."
        assert len(calls) == 1
        assert len(result.tool_events) == 1
        assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
        events = runtime.agent_run_manager.list_events(run.run_id)
        assert sum(e.type == "run_paused" for e in events) == 1
        assert sum(e.type == "run_resumed" for e in events) == 1
    finally:
        runtime.stop()


def test_sqlite_pause_survives_runtime_restart(tmp_path, monkeypatch):
    runtime, _, run, path, calls = pause_after_write(tmp_path, monkeypatch)
    runtime.stop()
    reopened, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("final_answer", reason="Write already completed."), "Recovered result.",
    ])
    try:
        assert reopened.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.PAUSED
        result = reopened.resume_agent_run(run.run_id)
        assert result.answer == "Recovered result."
        assert len(result.tool_events) == 1
        assert len(calls) == 1 and path.read_text() == "WRITE_ONCE"
        assert len(client.calls) == 2
    finally:
        reopened.stop()


def test_pause_before_initialization_is_resumable(tmp_path, monkeypatch):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("final_answer", reason="No tools needed."), "Hello.",
    ])
    try:
        run = runtime.create_agent_run(session_id="early", user_input="Hello")
        assert runtime.interrupt_agent_run(run.run_id).metadata["pause_requested"] is True
        with pytest.raises(AgentTurnPaused):
            run_existing(runtime, run)
        assert not client.calls
        assert runtime.agent_run_manager.get_run(run.run_id).metadata["paused_from_status"] == "queued"
        assert runtime.resume_agent_run(run.run_id).answer == "Hello."
    finally:
        runtime.stop()


def test_interrupt_during_model_call_is_nonblocking_and_stops_before_tool(tmp_path, monkeypatch):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.edit_file",
                  tool_input={"path": str(tmp_path / "never.txt"), "expected_sha256": "not-dispatched",
                              "edits": [{"old_text": "before", "new_text": "should not run"}]}),
    ])
    entered, release = Event(), Event()
    original = client.complete_text

    def blocked(**kwargs):
        entered.set()
        assert release.wait(5), "Test did not release the isolated provider"
        return original(**kwargs)

    monkeypatch.setattr(client, "complete_text", blocked)
    run = runtime.create_agent_run(session_id="blocking", user_input="Create a file.")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_existing, runtime, run)
            try:
                assert entered.wait(5)
                first = asyncio.run(interrupt_agent_run(run.run_id, request_for(runtime)))
                assert first.pause_requested and first.status == AgentRunStatus.RUNNING
                runtime.interrupt_agent_run(run.run_id)  # idempotent request
                with pytest.raises(ValueError, match="run_paused"):
                    runtime.replace_agent_run(run.run_id, user_input="New", command_id="early", request_fingerprint="f")
                with pytest.raises(HTTPException) as too_early:
                    asyncio.run(resume_agent_run(run.run_id, request_for(runtime)))
                assert too_early.value.status_code == 409
            finally:
                release.set()
            with pytest.raises(AgentTurnPaused):
                future.result(timeout=5)
        assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.PAUSED
        assert len(client.calls) == 1
        assert not (tmp_path / "never.txt").exists()
    finally:
        release.set()
        runtime.stop()


def test_replace_is_atomic_idempotent_and_keeps_the_same_session(tmp_path, monkeypatch):
    runtime, _, old, _, _ = pause_after_write(tmp_path, monkeypatch)
    try:
        request = request_for(runtime)
        launches = []
        monkeypatch.setattr("app.api.routes.agent._start_agent_turn_task",
                            lambda **values: launches.append(values))
        payload = ReplaceAgentRunRequest(command_id="replace-1", user_input="Do something different.")
        first = asyncio.run(replace_agent_run(old.run_id, payload, request))
        second = asyncio.run(replace_agent_run(old.run_id, payload, request))
        assert first.run_id == second.run_id and first.session_id == old.session_id
        assert runtime.agent_run_manager.get_run(old.run_id).status == AgentRunStatus.CANCELLED
        assert AgentRunResponse.from_record(runtime.agent_run_manager.get_run(old.run_id)).superseded_by_run_id == first.run_id
        with pytest.raises(RuntimeError, match="terminal"):
            runtime.resume_agent_run(old.run_id)
        with pytest.raises(HTTPException) as conflict:
            asyncio.run(replace_agent_run(old.run_id, payload.model_copy(update={"user_input": "Changed"}), request))
        assert conflict.value.status_code == 409
        with pytest.raises(HTTPException) as cross_session:
            asyncio.run(replace_agent_run(first.run_id, payload.model_copy(update={"session_id": "other"}), request))
        assert cross_session.value.status_code == 409
        assert all(item["run_id"] == first.run_id for item in launches)
        # New turn executes normally; the abandoned worker does not answer later.
        runtime.agent_turn_loop.llm_client = ScriptedClient([
            operation("final_answer", reason="New message only."), "New answer.",
        ])
        new = runtime.agent_run_manager.get_run(first.run_id)
        assert run_existing(runtime, new).answer == "New answer."
    finally:
        runtime.stop()


def test_pending_review_is_not_approved_by_pause_resume(tmp_path, monkeypatch):
    path = tmp_path / "review.txt"
    path.write_text("BEFORE_REVIEW", encoding="utf-8")
    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.edit_file",
                  tool_input={"path": str(path),
                              "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "edits": [{"old_text": "BEFORE_REVIEW", "new_text": "approved later"}]}),
        operation("final_answer", reason="Write completed."), "Done.",
    ])
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.MANUAL
    run = runtime.create_agent_run(session_id="review", user_input="Update a file.")
    try:
        with pytest.raises(AgentTurnWaitingForConfirmation) as waiting:
            run_existing(runtime, run)
        assert runtime.interrupt_agent_run(run.run_id).status == AgentRunStatus.PAUSED
        with pytest.raises(SafetyReviewQueueConflict):
            runtime.agent_run_manager.decide_safety_review_with_transition(
                review_id=waiting.value.review_id, decision=SafetyReviewDecision.APPROVE,
                decided_by="test", reason="Not while paused",
            )
        with pytest.raises(AgentTurnWaitingForConfirmation):
            runtime.resume_agent_run(run.run_id)
        assert path.read_text() == "BEFORE_REVIEW"
        assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.WAITING_CONFIRMATION
        runtime.agent_run_manager.decide_safety_review_with_transition(
            review_id=waiting.value.review_id, decision=SafetyReviewDecision.APPROVE,
            decided_by="test", reason="Explicitly approved",
        )
        assert runtime.resume_agent_run(run.run_id).answer == "Done."
        assert path.read_text() == "approved later"
    finally:
        runtime.stop()


def test_paused_sse_drains_and_closes_without_cancelling(tmp_path, monkeypatch):
    runtime, _, run, _, _ = pause_after_write(tmp_path, monkeypatch)
    try:
        async def consume():
            return "".join([part async for part in _agent_turn_event_stream(
                request=request_for(runtime), run_id=run.run_id)])
        text = asyncio.run(asyncio.wait_for(consume(), timeout=2))
        assert "event: run_paused" in text
        assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.PAUSED
    finally:
        runtime.stop()


def test_replace_transaction_failure_preserves_paused_run(tmp_path, monkeypatch):
    runtime, _, run, _, _ = pause_after_write(tmp_path, monkeypatch)
    store = runtime.agent_run_manager.durable_store
    original = store.save_run_control_batch

    def fail(**kwargs):
        assert kwargs["created"]
        raise OSError("Simulated disk failure")

    monkeypatch.setattr(store, "save_run_control_batch", fail)
    try:
        with pytest.raises(OSError, match="disk failure"):
            runtime.replace_agent_run(run.run_id, user_input="Replacement", command_id="fail", request_fingerprint="hash")
        assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.PAUSED
        assert store.load_run(run.run_id)["status"] == "paused"
        assert not runtime.agent_run_manager.is_cancel_requested(run.run_id)
    finally:
        monkeypatch.setattr(store, "save_run_control_batch", original)
        runtime.stop()


def test_durable_control_restores_pending_intent_and_replacement(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "controls.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="local", user_input="Old")
    manager.mark_running(run.run_id)
    manager.request_pause(run.run_id)
    manager.close()
    restored = InMemoryAgentRunManager(durable_store=store)
    try:
        assert restored.get_run(run.run_id).metadata["pause_requested"]
        restored.acknowledge_pause(run.run_id)
        new = restored.replace_paused(run.run_id, user_input="New", command_id="r", request_fingerprint="f")
        third = InMemoryAgentRunManager(durable_store=store)
        try:
            replay = third.replace_paused(run.run_id, user_input="New", command_id="r", request_fingerprint="f")
            assert replay.run_id == new.run_id
            assert third.get_run(run.run_id).status == AgentRunStatus.CANCELLED
        finally:
            third.close()
    finally:
        restored.close()


def test_immediate_resume_replaces_old_task_registry_entry_once():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="racing", user_input="Old")
    manager.mark_running(run.run_id)
    manager.request_pause(run.run_id)
    manager.acknowledge_pause(run.run_id)
    resumes = []

    async def scenario():
        release = asyncio.Event()
        old = asyncio.create_task(release.wait())  # Previous worker's task has not returned yet.

        async def resume(identifier):
            resumes.append(identifier)
            await release.wait()
            manager.resume_paused(identifier)
            manager.complete_run(identifier, result_snapshot={"answer": "resumed"}, log_path="isolated")

        runtime = SimpleNamespace(agent_run_manager=manager, _agent_turn_tasks={run.run_id: old},
                                  agent_turn_runner=SimpleNamespace(orchestrator_name="langgraph"),
                                  resume_agent_run_async=resume)
        request = request_for(runtime)
        await resume_agent_run(run.run_id, request)
        scheduled = runtime._agent_turn_tasks[run.run_id]
        assert scheduled is not old
        await resume_agent_run(run.run_id, request)
        assert runtime._agent_turn_tasks[run.run_id] is scheduled
        release.set()
        await asyncio.gather(old, scheduled)

    asyncio.run(scenario())
    assert resumes == [run.run_id]


def test_resume_and_replace_have_one_winner(tmp_path, monkeypatch):
    runtime, _, run, path, calls = pause_after_write(tmp_path, monkeypatch)
    barrier = Barrier(2)

    def resume():
        barrier.wait(timeout=5)
        return runtime.resume_agent_run(run.run_id)

    def replace():
        barrier.wait(timeout=5)
        return runtime.replace_agent_run(run.run_id, user_input="New", command_id="race", request_fingerprint="f")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(resume), pool.submit(replace)]
            results, errors = [], []
            for future in futures:
                try:
                    results.append(future.result(timeout=5))
                except (RuntimeError, ValueError) as exc:
                    errors.append(exc)
        assert len(results) == 1 and len(errors) == 1
        assert runtime.agent_run_manager.get_run(run.run_id).status in {
            AgentRunStatus.COMPLETED, AgentRunStatus.CANCELLED}
        assert len(calls) == 1 and path.read_text() == "WRITE_ONCE"
    finally:
        runtime.stop()


def test_cancel_paused_run_is_permanent(tmp_path, monkeypatch):
    runtime, client, run, _, _ = pause_after_write(tmp_path, monkeypatch)
    try:
        runtime.agent_run_manager.cancel_run(run.run_id)
        assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.CANCELLED
        with pytest.raises(RuntimeError, match="terminal"):
            runtime.resume_agent_run(run.run_id)
        assert len(client.calls) == 2
    finally:
        runtime.stop()


def test_store_rolls_back_inserted_replacement_if_event_validation_fails(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "rollback.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="local", user_input="Original")
    manager.request_pause(run.run_id)
    manager.acknowledge_pause(run.run_id)
    current = manager.get_run(run.run_id)
    previous = current.model_dump(mode="json")
    replacement = {**previous, "run_id": "new_failed", "status": "queued"}
    event = manager.list_events(run.run_id)[-1].model_dump(mode="json")
    event["sequence"] = 9999
    try:
        with pytest.raises(ValueError, match="sequence"):
            store.save_run_control_batch(previous=[previous], updated=[{**previous, "status": "cancelled"}],
                                         created=[replacement], events=[event], updated_at=current.created_at)
        assert store.load_run("new_failed") is None
        assert store.load_run(run.run_id)["status"] == "paused"
    finally:
        manager.close()


def test_legacy_and_active_children_refuse_resumable_interrupt(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, "legacy", [])
    try:
        run = runtime.create_agent_run(session_id="legacy", user_input="Old")
        with pytest.raises(HTTPException) as unsupported:
            asyncio.run(interrupt_agent_run(run.run_id, request_for(runtime)))
        assert unsupported.value.status_code == 409
        assert not runtime.agent_run_manager.get_run(run.run_id).metadata.get("pause_requested")
    finally:
        runtime.stop()
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Fork")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(parent_run_id=parent.run_id, plan_id="plan", step_id="child",
                                    attempt=1, user_input="Child")
    with pytest.raises(ValueError, match="child tasks"):
        manager.request_pause(parent.run_id)
    assert manager.get_run(child.run_id).status == AgentRunStatus.QUEUED
    assert not manager.get_run(parent.run_id).metadata.get("pause_requested")


def test_http_replacement_runs_new_message_and_does_not_publish_old_answer(tmp_path, monkeypatch):
    runtime, _, old, _, calls = pause_after_write(tmp_path, monkeypatch)
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Follow the new request."), "Replacement answer.",
    ])
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = runtime

    async def scenario():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated") as client:
            response = await client.post(f"/agent/runs/{old.run_id}/interrupt")
            assert response.status_code == 202
            assert response.json()["status"] == "paused" and response.json()["paused_at"]
            assert "user_input" not in response.json()
            invalid = await client.post(f"/agent/runs/{old.run_id}/replace",
                                        json={"command_id": "   ", "user_input": "New"})
            assert invalid.status_code == 422
            assert runtime.agent_run_manager.get_run(old.run_id).status == AgentRunStatus.PAUSED
            response = await client.post(f"/agent/runs/{old.run_id}/replace", json={
                "command_id": "http-replace", "user_input": "Follow the new request instead.",
                "llm": {"response_mode": "text"},
            })
            assert response.status_code == 202
            new_id = response.json()["run_id"]
            task = runtime._agent_turn_tasks.get(new_id)
            if task is not None:
                await asyncio.wait_for(task, timeout=5)
            old_response = await client.get(f"/agent/runs/{old.run_id}")
            assert old_response.json()["status"] == "cancelled"
            assert old_response.json()["superseded_by_run_id"] == new_id
            assert old_response.json()["pause_requested"] is False
            new_response = await client.get(f"/agent/runs/{new_id}")
            assert new_response.json()["status"] == "completed"
            assert new_response.json()["session_id"] == old.session_id
            assert runtime.agent_run_manager.get_run(new_id).result_snapshot["answer"] == "Replacement answer."
            assert not any(event.type == "run_completed" for event in runtime.agent_run_manager.list_events(old.run_id))
            assert len(calls) == 1
            assert (await client.post(f"/agent/runs/{old.run_id}/resume")).status_code == 409
            assert (await client.post("/agent/runs/missing/interrupt")).status_code == 404

    try:
        asyncio.run(scenario())
    finally:
        runtime.stop()


def test_restarted_pending_pause_can_be_acknowledged_and_resumed_by_api(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    runner = runtime.agent_turn_runner
    runner.interrupt_after = ["initialize_run"]
    run = runtime.create_agent_run(session_id="pending-restart", user_input="Continue this request.")
    runner._invoke_sync(run_id=run.run_id, session_id=run.session_id, user_input=run.user_input)
    runtime.agent_run_manager.request_pause(run.run_id)
    runtime.stop()
    reopened, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("final_answer", reason="Continue saved task."), "Continued after restart.",
    ])

    async def scenario():
        await resume_agent_run(run.run_id, request_for(reopened))
        task = reopened._agent_turn_tasks.get(run.run_id)
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        result = reopened.agent_run_manager.get_run(run.run_id)
        assert result.status == AgentRunStatus.COMPLETED
        assert result.result_snapshot["answer"] == "Continued after restart."

    try:
        asyncio.run(scenario())
    finally:
        reopened.stop()


def test_completion_claim_wins_and_waiting_user_state_is_preserved():
    manager = InMemoryAgentRunManager()
    completing = manager.create_run(session_id="completion", user_input="Finish")
    manager.mark_running(completing.run_id)
    assert manager.claim_completion(completing.run_id)
    with pytest.raises(ValueError, match="no longer"):
        manager.request_pause(completing.run_id)
    assert manager.acknowledge_pause(completing.run_id).status == AgentRunStatus.RUNNING
    waiting = manager.create_run(session_id="question", user_input="Need a choice")
    manager.mark_running(waiting.run_id)
    manager.mark_waiting_user(waiting.run_id, question="Which option?", question_id="q")
    manager.request_pause(waiting.run_id)
    manager.acknowledge_pause(waiting.run_id)
    restored = manager.resume_paused(waiting.run_id)
    assert restored.status == AgentRunStatus.WAITING_USER
    assert restored.metadata["pending_user_question"]["question_id"] == "q"


def test_legacy_default_can_opt_into_a_resumable_turn_without_config_changes(tmp_path, monkeypatch):
    runtime, _, run, path, calls = pause_after_write(tmp_path, monkeypatch, orchestrator="legacy", resumable=True)
    assert runtime.agent_turn_runner is runtime.agent_turn_loop
    assert runtime.local_app_config.agent.orchestrator == "legacy"
    assert runtime.agent_run_orchestrator(run.run_id) == "langgraph"
    runtime.stop()
    reopened, _ = runtime_for(tmp_path, monkeypatch, "legacy", [
        operation("final_answer", reason="Saved write already completed."), "Opt-in resumed.",
    ])
    try:
        assert reopened.resume_agent_run(run.run_id).answer == "Opt-in resumed."
        assert len(calls) == 1 and path.read_text() == "WRITE_ONCE"
        assert reopened.agent_turn_runner is reopened.agent_turn_loop
        assert reopened._resumable_runner.turn_loop is reopened.agent_turn_loop
    finally:
        reopened.stop()


def test_opt_in_stream_binding_and_replacement_keep_legacy_default(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, "legacy", [
        operation("final_answer", reason="New task."), "Replacement under opt-in.",
    ])

    async def scenario():
        request = request_for(runtime)
        response = await stream_agent_turn(AgentTurnRequest(
            session_id="opt-in-stream", user_input="Old task", resumable=True,
        ), request)
        run = next(item for item in runtime.agent_run_manager._runs.values() if item.session_id == "opt-in-stream")
        assert runtime.agent_run_orchestrator(run.run_id) == "langgraph"
        await interrupt_agent_run(run.run_id, request)
        frames = "".join([frame async for frame in response.body_iterator])
        assert "event: run_paused" in frames
        replacement = await replace_agent_run(run.run_id, ReplaceAgentRunRequest(
            command_id="opt-in-replace", user_input="New task", llm={"response_mode": "text"},
        ), request)
        task = runtime._agent_turn_tasks.get(replacement.run_id)
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        assert runtime.agent_run_orchestrator(replacement.run_id) == "langgraph"
        assert runtime.agent_run_manager.get_run(replacement.run_id).result_snapshot["answer"] == "Replacement under opt-in."
        assert runtime.agent_turn_runner is runtime.agent_turn_loop
        normal = runtime.create_agent_run(session_id="ordinary", user_input="No opt-in")
        assert runtime.agent_run_orchestrator(normal.run_id) == "legacy"
        with pytest.raises(ValueError, match="retroactively"):
            runtime.run_agent_turn(session_id=normal.session_id, user_input=normal.user_input,
                                   existing_run_id=normal.run_id, resumable=True)

    try:
        asyncio.run(scenario())
    finally:
        runtime.stop()
