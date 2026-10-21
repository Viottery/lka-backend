"""Progress control across both orchestrators, real tools and checkpoint recovery."""

import asyncio
import json
from hashlib import sha256

import pytest

from app.core.agent_checkpoints import create_sqlite_checkpoint_runtime
from app.core.agent_graph import AgentGraphRunner
from app.core.llm import LLMResponse, LLMToolCall
from app.core.task_progress import PROGRESS_POLICY
from tests.test_unified_entry_quality import operation, runtime_for


def envelope(kind, *, working_state=None, purpose=None, **fields):
    value = {"operation": {"type": kind, **fields}}
    if working_state is not None:
        value["working_state"] = working_state
    if purpose is not None:
        value["purpose"] = purpose
    return json.dumps(value)


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_duplicate_skips_execution_then_allows_edit_and_verification(tmp_path, monkeypatch, orchestrator):
    path = tmp_path / "notes.md"
    path.write_text("Status: red\n")
    read = operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(path)})
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("expand_package", package_name="filesystem"), read, read,
        envelope("tool_call", tool_name="filesystem.edit_file", tool_input={
            "path": str(path), "expected_sha256": sha256(b"Status: red\n").hexdigest(),
            "edits": [{"old_text": "red", "new_text": "green"}]},
            working_state={"focus_gap": "The requested replacement is still pending"},
            purpose={"gap": "Apply the requested replacement", "question": "Can the red value be replaced safely?",
                     "expected_observable": "The targeted edit succeeds before verification"}),
        read, operation("final_answer", reason="Change verified."), "Changed and verified.",
    ])
    try:
        result = runtime.run_agent_turn(session_id="duplicate-recovery", user_input="Change red to green and verify it.")
        assert path.read_text() == "Status: green\n"
        assert [event.tool_name for event in result.tool_events] == [
            "filesystem.read_file", "filesystem.edit_file", "filesystem.read_file"]
        assert result.answer == "Changed and verified."
        assert result.task_outcome == "unknown" and result.stop_reason is None
        assert len(client.calls) == 7
        assert not any(event.source == "local" and event.action == "final_answer" for event in result.decision_events)
        recovery = json.loads(client.calls[3]["user_prompt"])
        assert recovery["task_working_state"]["review_required"] is True
        feedback = next(item for item in recovery["observations"] if item.get("action") == "tool_duplicate_feedback")
        assert feedback["evidence_ids"] == [result.tool_events[0].result["invocation_id"]]
        initial_steps = json.loads(client.calls[0]["user_prompt"])["task_working_state"]["remaining_steps"]
        assert recovery["task_working_state"]["remaining_steps"] == initial_steps - 3
    finally:
        runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_varied_reads_trigger_review_without_forcing_write_or_finish(tmp_path, monkeypatch, orchestrator):
    paths = [tmp_path / f"source-{index}.md" for index in range(4)]
    for index, path in enumerate(paths):
        path.write_text(f"Source fact {index}")
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("expand_package", package_name="filesystem"),
        *(operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(path)}) for path in paths),
        operation("final_answer", reason="Compared the sources."), "Comparison.",
    ])
    try:
        result = runtime.run_agent_turn(session_id="varied-reads", user_input="Compare four sources.")
        assert len(result.tool_events) == 4
        before_fourth = json.loads(client.calls[4]["user_prompt"])
        state = before_fourth["task_working_state"]
        assert state["review_required"] is True
        assert state["tool_calls"] == 3
        assert state["calls_since_milestone"] == 3
        after_fourth = json.loads(client.calls[5]["user_prompt"])
        assert after_fourth["task_working_state"]["tool_calls"] == 4
        assert not any(item.get("action") == "task_progress_review_feedback" for item in after_fourth["observations"])
        reviews = [event for event in result.progress_events if event.type == "task_progress_review_required"]
        assert reviews and all(event.metadata["operation_deferred"] is False for event in reviews)
        assert "instruction" not in state
        assert client.calls[4]["system_prompt"].count(PROGRESS_POLICY) == 1
        assert len(client.calls) == 7  # No extra decision just to restate progress notes.
        assert result.answer == "Comparison."
        assert result.task_outcome == "unknown"
    finally:
        runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_unanswered_review_is_advisory_and_preserves_execution_budget(tmp_path, monkeypatch, orchestrator):
    paths = [tmp_path / f"fact-{index}.md" for index in range(5)]
    for index, path in enumerate(paths):
        path.write_text(f"Required fact {index}")
    reads = [operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(path)})
             for path in paths]
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("expand_package", package_name="filesystem"), *reads,
        operation("final_answer", reason="All five sources read."), "Five facts compared.",
    ])
    try:
        result = runtime.run_agent_turn(session_id="bounded-review", user_input="Compare all five source facts.")
        assert len(result.tool_events) == 5
        assert len(client.calls) == 8
        reviews = [event for event in result.progress_events if event.type == "task_progress_review_required"]
        assert len(reviews) == 2
        assert all(event.metadata["operation_deferred"] is False for event in reviews)
        after_fourth = json.loads(client.calls[5]["user_prompt"])["task_working_state"]
        assert after_fourth["review_required"] is True and after_fourth["review_deferrals"] == 0
        assert after_fourth["calls_since_milestone"] == 4
        assert not any(event.source == "local" and event.action == "final_answer" for event in result.decision_events)
        assert result.answer == "Five facts compared."
    finally:
        runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_complete_but_invalid_operation_gets_field_feedback_and_incomplete_outcome(tmp_path, monkeypatch, orchestrator):
    invalid = '{"operation":{"type":"bash.run","tool_name":"bash.run","tool_input":{"command":"pwd"}}}'
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("expand_package", package_name="bash"), invalid, invalid, "Unfinished task.",
    ])
    try:
        result = runtime.run_agent_turn(session_id="protocol-failure", user_input="Investigate the workspace.")
        assert not result.tool_events
        assert result.task_outcome == "incomplete"
        assert result.stop_reason == "decision_protocol_invalid"
        retry = json.loads(client.calls[2]["user_prompt"])["decision_retry"]
        assert retry["validation_errors"][0]["path"] == "/operation/type"
        assert retry["validation_errors"][0]["received"] == "bash.run"
        assert "tool_call" in retry["validation_errors"][0]["correction"]
        assert any(event.raw_output == invalid for event in result.decision_events)
        run = runtime.agent_run_manager.get_run(result.run_id)
        assert run.status.value == "completed"
        assert run.result_snapshot["task_outcome"] == "incomplete"
    finally:
        runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_native_notes_are_removed_before_executor_schema_validation(tmp_path, monkeypatch, orchestrator):
    path = tmp_path / "evidence.md"
    path.write_text("EVIDENCE_TOKEN")
    runtime, _ = runtime_for(tmp_path, monkeypatch, orchestrator, [])

    class NativeClient:
        supports_function_calling = True
        def __init__(self):
            self.calls = []

        def complete_text(self, **kwargs):
            self.calls.append(kwargs)
            if not kwargs.get("tools"):
                return LLMResponse(provider="script", status="completed", content="EVIDENCE_TOKEN", prompt_summary="answer")
            definitions = kwargs["tools"]
            if len(self.calls) == 1:
                name, arguments = "agent_expand_package", {"package_name": "filesystem", "_agent_progress": None}
            elif len(self.calls) == 2:
                name = next(item.name for item in definitions if item.name.endswith("filesystem_read_file"))
                arguments = {"path": str(path), "_agent_progress": {
                    "working_state": {"remaining_requirements": ["Read evidence"], "focus_gap": "Read the requested identifier"},
                    "purpose": {"gap": "Missing identifier", "question": "Which identifier is present?",
                                "expected_observable": "The file's identifier text"}}}
            else:
                name, arguments = "agent_finish_decision", {"reason": "Requested identifier read."}
            return LLMResponse(provider="script", status="completed", content="", prompt_summary="control", tool_calls=[
                LLMToolCall(id="call", name=name, arguments=arguments, raw_arguments=json.dumps(arguments))])

    client = NativeClient()
    runtime.agent_turn_loop.llm_client = client
    try:
        result = runtime.run_agent_turn(session_id="native-notes", user_input="Read the requested identifier.")
        assert len(result.tool_events) == 1
        assert "_agent_progress" not in result.tool_events[0].input
        assert json.loads(client.calls[2]["user_prompt"])["task_working_state"]["focus_gap"] == "Read the requested identifier"
        assert result.answer == "EVIDENCE_TOKEN"
    finally:
        runtime.stop()


def test_working_notes_and_stall_counter_survive_sqlite_checkpoint_reopen(tmp_path, monkeypatch):
    path = tmp_path / "evidence.md"
    path.write_text("CHECKPOINT_EVIDENCE")
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("expand_package", package_name="filesystem"),
        envelope("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(path)},
                 working_state={"remaining_requirements": ["Read then report"], "focus_gap": "Missing requested evidence"}),
        operation("final_answer", reason="Read complete."), "CHECKPOINT_EVIDENCE",
    ])
    checkpoint_path = tmp_path / "progress-checkpoints.sqlite3"
    first = AgentGraphRunner(runtime.agent_turn_loop, artifact_store=runtime.agent_run_store,
        checkpoint_runtime=create_sqlite_checkpoint_runtime(checkpoint_path), interrupt_after=["build_observation"])
    try:
        run = first.create_run_for_turn(session_id="progress-reopen", user_input="Read then report.")
        asyncio.run(first._invoke(run_id=run.run_id, session_id=run.session_id, user_input=run.user_input))
        paused = first.get_state(run.run_id)
        state = paused.values["working_set"]["task_working_state"]
        assert state["tool_calls"] == 1
        assert state["focus_gap"] == "Missing requested evidence"
        first.close()
        second = AgentGraphRunner(runtime.agent_turn_loop, artifact_store=runtime.agent_run_store,
            checkpoint_runtime=create_sqlite_checkpoint_runtime(checkpoint_path))
        try:
            result = second.resume(run.run_id)
            state = second.get_state(run.run_id).values["working_set"]["task_working_state"]
            assert state["tool_calls"] == 1 and len(state["evidence"]) == 1
            assert len(result.tool_events) == 1 and len(client.calls) == 4
        finally:
            second.close()
    finally:
        runtime.stop()


def test_child_incomplete_handoff_keeps_specific_reason(tmp_path):
    from app.core.child_agent import ChildAgentExecutor
    from tests.test_answer_generation_recovery_quality import make_loop, scope

    loop, _provider, manager = make_loop(tmp_path, [])
    with scope(manager) as run:
        loop._record_incomplete([], "decision_protocol_invalid")
        missing = ChildAgentExecutor(runner=loop, run_manager=manager)._completion_missing_requirements(run.run_id)
    assert missing == ("decision_protocol_invalid",)


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_soft_deadline_stops_decisions_and_preserves_incomplete_result(tmp_path, monkeypatch, orchestrator):
    import time

    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, ["Existing evidence is insufficient."])
    try:
        run = runtime.agent_turn_loop.create_run_for_turn(session_id="soft-limit", user_input="Inspect the workspace.")
        runtime.agent_run_manager._update_run(run.run_id, status=run.status,
            metadata_patch={"execution_deadline_unix_seconds": time.time() + 10})
        result = runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        assert not result.tool_events
        assert result.task_outcome == "incomplete" and result.stop_reason == "execution_deadline"
        assert all(call["metadata"]["stage"] in {"answer", "context_answer"} for call in client.calls)
    finally:
        runtime.stop()


def test_progress_notes_never_cross_public_event_boundary(tmp_path):
    from app.api.routes.agent import _public_agent_event, _sse_event_frame
    from app.api.schemas import AgentTurnResponse
    from app.core.agent_turn import AgentTurnResult
    from tests.test_answer_generation_recovery_quality import make_loop

    _loop, _provider, manager = make_loop(tmp_path, [])
    run = manager.create_run(session_id="private-progress", user_input="test")
    event = manager.append_event(run.run_id, "task_working_state_updated", "Internal notes",
                                 payload={"task_working_state": {"focus_gap": "PRIVATE_TASK_NOTE"}})
    public = _public_agent_event(event)
    assert public.payload == {} and public.message == ""
    assert "PRIVATE_TASK_NOTE" not in _sse_event_frame(public)
    result = AgentTurnResult(run_id=run.run_id, session_id=run.session_id, trace_id=run.trace_id,
                             answer="Incomplete", task_outcome="incomplete", stop_reason="decision_protocol_invalid")
    response = AgentTurnResponse.from_result(result).model_dump()
    assert response["task_outcome"] == "incomplete"
    assert response["stop_reason"] == "decision_protocol_invalid"
    assert "task_working_state" not in response


@pytest.mark.parametrize("stage", ["decision", "decision_repair", "route", "tool_result_check",
                                   "context_summarize", None])
def test_public_control_deltas_hide_private_notes(stage):
    from app.api.routes.agent import _public_agent_event, _sse_event_frame
    from app.core.agent_runs import InMemoryAgentRunManager

    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="private-delta", user_input="test")
    note = '{"working_state":{"focus_gap":"PRIVATE_TASK_NOTE"}}'
    event = manager.append_event(run.run_id, "llm_delta", note, stage=stage, payload={
        "delta": note, "content_snapshot": note, "extra_content": note,
        "content_role": "agent_decision", "display_target": "agent_process", "llm_call_id": "call-1",
    })
    public = _public_agent_event(event)
    assert "PRIVATE_TASK_NOTE" not in public.model_dump_json()
    assert "PRIVATE_TASK_NOTE" not in _sse_event_frame(public)
    assert public.message == ""
    assert public.payload == {"content_role": "agent_decision", "display_target": "agent_process",
                              "llm_call_id": "call-1"}
    assert event.payload["delta"] == note  # The local audit remains complete.


@pytest.mark.parametrize("stage", ["answer", "context_answer"])
def test_public_answer_deltas_keep_streamed_text(stage):
    from app.api.routes.agent import _public_agent_event, _sse_event_frame
    from app.core.agent_runs import InMemoryAgentRunManager

    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="answer-delta", user_input="test")
    event = manager.append_event(run.run_id, "llm_delta", "Answer fragment", stage=stage,
                                 payload={"delta": "Answer fragment", "content_snapshot": "Answer fragment"})
    public = _public_agent_event(event)
    assert public == event
    assert "Answer fragment" in _sse_event_frame(public)
