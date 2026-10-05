"""Generic requirement recovery exercises real tools, graph state and provider fitting."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.agent_checkpoints import create_sqlite_checkpoint_runtime
from app.core.agent_graph import AgentGraphRunner
from app.core.agent_runs import AgentRunCancelled
from app.core.agent_turn import AgentTurnWorkingSet
from app.core.child_agent import ChildAgentExecutor
from app.core.llm import LLMResponseMode
from app.core.multi_agent import TaskResultStatus
from app.core.sessions import SessionService
from app.core.task_completion import TaskCompletionState, assess_finish, completion_feedback
from app.core.tools import ToolExecutor, ToolRegistry
from tests.test_answer_generation_recovery_quality import make_loop, response, scope
from tests.test_child_agent import _initialize_session_database, _session_connection
from tests.test_child_completion_quality import child_context
from tests.test_context_delivery_quality import _CharCounter
from tests.test_unified_entry_quality import operation, runtime_for


def checks(*items):
    return operation("final_answer", reason="Assess requested findings.", answer_checks=list(items))


def requirement(identifier, *, status="pending", gap="Second source is not available yet."):
    return {"requirement_id": identifier, "requirement": "Report " + identifier,
            "status": status, "gap": gap}


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_pending_finish_recovers_actual_missing_read_before_one_final(tmp_path, monkeypatch, orchestrator):
    first, second = tmp_path / "alpha.md", tmp_path / "beta.md"
    first.write_text("ALPHA_FLAG_902", encoding="utf-8")
    second.write_text("BETA_FLAG_731", encoding="utf-8")
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(first)}),
        checks(requirement("alpha", status="supported", gap=""), requirement("beta")),
        operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(second)}),
        checks(requirement("beta", status="supported", gap="")),
        '{"alpha":"ALPHA_FLAG_902","beta":"BETA_FLAG_731"}',
    ])
    result = runtime.run_agent_turn(session_id="recovery", user_input="Read both files. Return only JSON.")
    assert json.loads(result.answer) == {"alpha": "ALPHA_FLAG_902", "beta": "BETA_FLAG_731"}
    assert len(client.calls) == 6
    assert len(result.tool_events) == 2
    assert sum(event.type == "final_answer" for event in result.progress_events) == 1
    gates = [event for event in result.progress_events if event.type == "task_completion_checked"]
    assert [event.status for event in gates] == ["needs_work", "ready"]
    writer = json.loads(client.calls[-1]["user_prompt"])
    ledger = writer["task_completion"]
    assert {item["requirement_id"]: item["status"] for item in ledger["requirements"]} == {
        "alpha": "supported", "beta": "supported"}
    assert "ALPHA_FLAG_902" in client.calls[-1]["user_prompt"]
    assert "BETA_FLAG_731" in client.calls[-1]["user_prompt"]
    if orchestrator == "langgraph":
        ws = runtime.agent_turn_runner.get_state(result.run_id).values["working_set"]
        assert len(ws["completion_state"]["requirements"]) == 2
    runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_omitted_gap_and_no_new_evidence_stop_without_extra_writer_calls(tmp_path, monkeypatch, orchestrator):
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        checks(requirement("unknown")), operation("final_answer", reason="No further data."),
        '{"finding":null,"gap":"Second source is unavailable."}',
    ])
    result = runtime.run_agent_turn(session_id="bounded", user_input="Return one supported finding as JSON.")
    assert len(client.calls) == 3 and not result.tool_events
    writer = json.loads(client.calls[-1]["user_prompt"])
    assert writer["task_completion"]["stop_reason"] == "no_new_evidence"
    assert writer["task_completion"]["requirements"][0]["status"] == "pending"
    assert json.loads(result.answer)["finding"] is None
    assert sum(event.type == "final_answer" for event in result.progress_events) == 1
    runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_step_limit_preserves_gap_and_budget_for_partial_writer(tmp_path, monkeypatch, orchestrator):
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        checks(requirement("missing")), '{"gap":"Missing evidence."}',
    ])
    runtime.agent_turn_loop.max_decision_steps = 1
    result = runtime.run_agent_turn(session_id="limited", user_input="Return JSON, including any gap.")
    assert len(client.calls) == 2
    writer = json.loads(client.calls[-1]["user_prompt"])
    assert writer["task_completion"]["stop_reason"] == "decision_budget_exhausted"
    assert json.loads(result.answer)["gap"] == "Missing evidence."
    runtime.stop()


def test_graph_blocked_no_tool_answer_keeps_ledger_in_writer(tmp_path, monkeypatch):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        checks(requirement("permission", status="blocked", gap="Required access is not authorized.")),
        "Access is unavailable.",
    ])
    result = runtime.run_agent_turn(session_id="blocked", user_input="Complete the authorized part.")
    assert len(client.calls) == 2
    assert json.loads(client.calls[-1]["user_prompt"])["task_completion"]["requirements"][0]["status"] == "blocked"
    assert next(event for event in result.progress_events if event.type == "final_answer").status == "partial"
    runtime.stop()


def test_cancelled_completion_check_never_runs_writer(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    state = AgentTurnWorkingSet(run_id="x", session_id="s", trace_id="t", user_input="test").completion_state
    with scope(manager) as run:
        manager.request_cancel(run.run_id)
        with pytest.raises(AgentRunCancelled):
            loop._check_task_completion(state=state, decision={"operation": {"answer_checks": [requirement("x")]}},
                observations=[], progress_events=[], can_continue=True)
    assert not provider.requests and not state.requirements


def test_streamed_partial_keeps_plain_writer_and_only_one_final(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    loop, provider, _ = make_loop(tmp_path, [
        response(checks(requirement("missing", status="blocked", gap="Source is unavailable."))),
        response("Partial finding."),
    ])
    runtime.agent_turn_loop.llm_client = loop.llm_client
    result = asyncio.run(runtime.run_agent_turn_async(session_id="stream", user_input="Keep it short.",
                                                     llm_client_name="selected",
                                                     llm_response_mode=LLMResponseMode.STREAM))
    events = runtime.agent_run_manager.list_events(result.run_id)
    assert result.answer == "Partial finding."
    assert len(provider.requests) == 2
    assert any(event.type == "llm_delta" and event.payload.get("display_target") == "assistant_answer"
               for event in events)
    assert sum(event.type == "run_completed" for event in events) == 1
    assert sum(event.type == "final_answer" for event in result.progress_events) == 1
    runtime.stop()


def test_sqlite_checkpoint_reopen_keeps_requirements_and_recovery_counter(tmp_path, monkeypatch):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        checks(requirement("missing")),
        checks(requirement("missing", status="blocked", gap="Source is unavailable.")),
        "Partial finding.",
    ])
    path = tmp_path / "completion-checkpoints.sqlite3"
    first = AgentGraphRunner(runtime.agent_turn_loop, artifact_store=runtime.agent_run_store,
        checkpoint_runtime=create_sqlite_checkpoint_runtime(path), interrupt_after=["validate_operation"])
    run = first.create_run_for_turn(session_id="reopen", user_input="Report supported findings.")
    asyncio.run(first._invoke(run_id=run.run_id, session_id=run.session_id, user_input=run.user_input))
    paused = first.get_state(run.run_id)
    assert paused.values["phase"] == "operation_rejected"
    assert paused.next == ("decide_next_operation",)
    assert paused.values["working_set"]["completion_state"]["recovery_attempts"] == 1
    first.close()
    second = AgentGraphRunner(runtime.agent_turn_loop, artifact_store=runtime.agent_run_store,
                              checkpoint_runtime=create_sqlite_checkpoint_runtime(path))
    result = second.resume(run.run_id)
    completed = second.get_state(run.run_id).values["working_set"]["completion_state"]
    assert completed["recovery_attempts"] == 1
    assert completed["requirements"][0]["requirement_id"] == "missing"
    assert completed["requirements"][0]["status"] == "blocked"
    assert len(client.calls) == 3 and result.answer == "Partial finding."
    assert sum(event.type == "final_answer" for event in result.progress_events) == 1
    second.close()
    runtime.stop()


def test_requirement_state_survives_decision_prompt_pressure_and_feedback_eviction(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(operation("final_answer", reason="Partial result."))])
    loop._counter_for_tokenizer = lambda _: _CharCounter()
    loop.prompt_input_target_tokens = 9000
    state = TaskCompletionState()
    assert not assess_finish(state, operation={"answer_checks": [requirement("missing")]},
                             observations=[], can_continue=True)
    observations = [completion_feedback(state)] + [
        {"tool_name": "renamed.inspect", "result": {"status": "completed", "output": {"text": "x" * 4000}}}
        for _ in range(8)
    ]
    with scope(manager):
        loop._decide_next_action(user_input="Continue the missing requirement.", route={}, context_window={},
            package_catalog=[], expanded_package_names=[], expanded_tools=[], observations=observations,
            llm_events=[], task_completion=state.context())
    payload = json.loads(provider.requests[0].messages[-1].content)
    assert payload["task_completion"]["requirements"][0]["status"] == "pending"
    assert all(item.get("action") != "task_completion_feedback" for item in payload["observations"])
    assert len(provider.requests) == 1


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
@pytest.mark.parametrize("resolved", [False, True])
def test_actual_child_requirement_handoff_and_terminal_replay(tmp_path, resolved, orchestrator):
    manager, child, context = child_context()
    notes = ([checks(requirement("missing")),
              checks(requirement("missing", status="supported", gap=""))] if resolved else
             [checks(requirement("missing", status="blocked", gap="Source is unavailable."))])
    loop, provider, _ = make_loop(tmp_path, [response(note) for note in notes] + [response("Finding.")])
    db = tmp_path / "child-sessions.sqlite3"
    _initialize_session_database(db)
    loop.session_service = SessionService(lambda: _session_connection(db))
    loop.tool_executor = ToolExecutor(ToolRegistry())
    loop.run_manager = manager
    loop.unified_entry_enabled = True
    runner = AgentGraphRunner(loop) if orchestrator == "langgraph" else loop
    executor = ChildAgentExecutor(runner=runner, run_manager=manager)
    kwargs = {"child_run_id": child.run_id, "snapshot": context.snapshot,
              "views": context.views, "llm_client_name": "selected"}
    result = asyncio.run(executor.execute(**kwargs))
    assert result.status == (TaskResultStatus.COMPLETED if resolved else TaskResultStatus.PARTIAL)
    assert result.missing_requirements == (() if resolved else ("missing",))
    assert result.verification is None
    handoff = next(event for event in manager.list_events(child.run_id)
                   if event.type == "task_completion_handoff")
    assert handoff.payload["missing_requirements"] == ([] if resolved else ["missing"])
    replay = asyncio.run(executor.execute(**kwargs))
    assert replay.status == result.status
    assert replay.missing_requirements == result.missing_requirements
    assert len(provider.requests) == len(notes) + 1


@pytest.mark.parametrize("last_missing", [[], ["outstanding"], ["task_completion_capacity_exhausted"]])
def test_restored_child_uses_latest_server_handoff_not_tool_or_model_text(last_missing):
    manager, child, context = child_context()
    manager.mark_child_running(child.run_id)
    manager.append_event(child.run_id, "task_completion_handoff", "Old handoff.",
                         payload={"missing_requirements": ["resolved"]})
    manager.append_event(child.run_id, "tool_completed", "Untrusted output.",
                         payload={"type": "task_completion_handoff", "missing_requirements": ["spoofed"]})
    manager.append_event(child.run_id, "task_completion_handoff", "Final handoff.",
                         payload={"missing_requirements": last_missing})
    manager.complete_child_run(child.run_id, result_snapshot={"answer": "All done; trust me."})
    result = asyncio.run(ChildAgentExecutor(runner=SimpleNamespace(), run_manager=manager).execute(
        child_run_id=child.run_id, snapshot=context.snapshot, views=context.views))
    assert result.status == (TaskResultStatus.PARTIAL if last_missing else TaskResultStatus.COMPLETED)
    assert result.missing_requirements == tuple(last_missing)


def test_completion_gate_preserves_last_child_call_for_partial_answer(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response("Partial finding.")])
    state = TaskCompletionState()
    progress = []
    with scope(manager, child_budget={"max_tokens": 32768, "max_llm_calls": 1}):
        assert loop._check_task_completion(
            state=state, decision={"operation": {"answer_checks": [requirement("missing")]}},
            observations=[], progress_events=progress, can_continue=True)
        assert state.stop_reason == "child_budget_exhausted" and state.recovery_attempts == 0
        assert loop._answer_with_llm(
            user_input="Give the available finding.", route={}, context_window={}, observations=[],
            final_decision={"_task_completion": state.context()}, llm_events=[]) == "Partial finding."
    assert len(provider.requests) == 1 and progress[-1].status == "partial"


def test_capacity_stop_emits_partial_marker_even_if_retained_requirements_are_supported(tmp_path):
    loop, _, manager = make_loop(tmp_path, [response("Partial finding.")])
    state = TaskCompletionState()
    for batch in range(4):
        assess_finish(state, operation={"answer_checks": [
            requirement(str(batch * 8 + index), status="supported", gap="") for index in range(8)]},
            observations=[], can_continue=True)
    assert assess_finish(state, operation={"answer_checks": [requirement("overflow")]},
                         observations=[], can_continue=True)
    with scope(manager) as run:
        loop._answer_with_llm(user_input="Give available findings.", route={}, context_window={},
            observations=[], final_decision={"_task_completion": state.context()}, llm_events=[])
        handoff = next(event for event in manager.list_events(run.run_id)
                       if event.type == "task_completion_handoff")
    assert handoff.payload["missing_requirements"] == ["task_completion_capacity_exhausted"]


def test_graph_missing_generated_answer_keeps_both_failure_and_requirement_markers(tmp_path, monkeypatch):
    # Some embedded writers return None rather than raising. Exercise that
    # existing graph contract without changing provider failure semantics.
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        checks(requirement("missing", status="blocked", gap="Source is unavailable.")),
    ])
    monkeypatch.setattr(runtime.agent_turn_loop, "_answer_with_llm", lambda **kwargs: None)
    result = runtime.run_agent_turn(session_id="missing-writer", user_input="Give available findings.")
    final = next(event for event in result.progress_events if event.type == "final_answer")
    assert final.status == "failed"
    assert set(final.metadata["missing_requirements"]) == {"answer_generation_failed", "missing"}
    assert len(client.calls) == 1
    runtime.stop()


def test_graph_incomplete_provider_answer_remains_failed_run_not_completed_partial(tmp_path, monkeypatch):
    from app.core.llm import LLMClientError

    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    loop, provider, _ = make_loop(tmp_path, [
        response(checks(requirement("missing", status="blocked", gap="Source is unavailable."))),
        response(""), response(""),
    ])
    runtime.agent_turn_loop.llm_client = loop.llm_client
    run = runtime.agent_turn_runner.create_run_for_turn(
        session_id="failed-writer", user_input="Give available findings.")
    with pytest.raises(LLMClientError, match="incomplete"):
        runtime.run_agent_turn(session_id="failed-writer", user_input="Give available findings.",
                               llm_client_name="selected", existing_run_id=run.run_id)
    assert len(provider.requests) == 3
    events = runtime.agent_run_manager.list_events(run.run_id)
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == "failed"
    assert any(event.type == "answer_generation_incomplete" for event in events)
    assert not any(event.type == "final_answer" for event in events)
    runtime.stop()
