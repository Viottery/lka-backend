"""Generation failure is structured, not inferred from an abstention's prose."""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.agent_graph import AgentGraphRunner
from app.core.agent_runs import AgentRunStatus
from app.core.agent_turn import AgentTurnResult
from app.core.child_agent import ChildAgentExecutor
from app.core.llm import LLMClientError
from app.core.multi_agent import TaskResultStatus
from app.core.sessions import SessionService
from app.core.tools import ToolExecutor, ToolRegistry
from tests.test_answer_generation_recovery_quality import make_loop, response
from tests.test_child_agent import _initialize_session_database, _session_connection
from tests.test_child_completion_quality import child_context, decisions

PLACEHOLDER = "我还没有获得足够的有效证据来完成这个请求。请重试或提供更多上下文。"


@pytest.mark.parametrize("generation", ["refused", "blank", "abstain"])
def test_actual_graph_failed_route_and_context_answer_vs_identical_generated_abstention(
    tmp_path, generation,
):
    manager, child, context = child_context()
    generated = generation == "abstain"
    responses = [LLMClientError("Local dispatch cap refused routing.")]
    responses.append(response(PLACEHOLDER) if generated else response(" \n ")
                     if generation == "blank" else LLMClientError("Local cap refused answer."))
    loop, provider, _ = make_loop(tmp_path, responses)
    db = tmp_path / "sessions.sqlite3"
    _initialize_session_database(db)
    loop.session_service = SessionService(lambda: _session_connection(db))
    loop.tool_executor = ToolExecutor(ToolRegistry())
    loop.run_manager = manager
    runner = AgentGraphRunner(loop)
    executor = ChildAgentExecutor(runner=runner, run_manager=manager)
    kwargs = {"child_run_id": child.run_id, "snapshot": context.snapshot,
              "views": context.views, "llm_client_name": "selected"}

    result = asyncio.run(executor.execute(**kwargs))

    assert len(provider.requests) == 2
    assert [request.prompt_summary.split()[0] for request in provider.requests] == [
        "agent_turn_route", "agent_turn_context_answer",
    ]
    assert manager.get_run(child.run_id).status == AgentRunStatus.COMPLETED
    assert result.summary == PLACEHOLDER
    assert result.status == (TaskResultStatus.COMPLETED if generated else TaskResultStatus.PARTIAL)
    assert result.missing_requirements == (() if generated else ("answer_generation_failed",))
    assert result.verification is None
    final = next(e for e in manager.list_events(child.run_id) if e.type == "final_answer")
    assert final.payload["status"] == ("completed" if generated else "failed")
    if not generated:
        assert final.payload["metadata"]["missing_requirements"] == ["answer_generation_failed"]

    replay = asyncio.run(executor.execute(**kwargs))
    assert replay.status == result.status
    assert replay.missing_requirements == result.missing_requirements
    assert len(provider.requests) == 2


def test_immutable_result_artifact_recovers_explicit_failure_without_wrapper_event():
    manager, child, context = child_context()
    manager.mark_child_running(child.run_id)
    payload = AgentTurnResult(
        run_id=child.run_id, session_id=child.session_id, trace_id=child.trace_id,
        answer=PLACEHOLDER, decision_events=decisions("answer_generation_failed"),
    ).model_dump(mode="json")
    manager.complete_child_run(child.run_id, result_snapshot={
        "answer": "Stale successful snapshot.", "result_artifact_ref": {"payload": payload},
    })
    result = asyncio.run(ChildAgentExecutor(runner=SimpleNamespace(), run_manager=manager).execute(
        child_run_id=child.run_id, snapshot=context.snapshot, views=context.views,
    ))
    assert result.status == TaskResultStatus.PARTIAL
    assert result.missing_requirements == ("answer_generation_failed",)
    assert result.summary == PLACEHOLDER


def test_persisted_server_failure_event_survives_missing_result_artifact():
    manager, child, context = child_context()
    manager.mark_child_running(child.run_id)
    manager.append_event(child.run_id, "answer_generation_failed", "No generated answer.",
                         stage="answer", payload={"missing_requirements": ["answer_generation_failed"]})
    manager.complete_child_run(child.run_id, result_snapshot={"answer": PLACEHOLDER})
    result = asyncio.run(ChildAgentExecutor(runner=SimpleNamespace(), run_manager=manager).execute(
        child_run_id=child.run_id, snapshot=context.snapshot, views=context.views,
    ))
    assert result.status == TaskResultStatus.PARTIAL
    assert result.missing_requirements == ("answer_generation_failed",)


def test_failure_action_from_model_text_is_not_a_server_failure_marker():
    manager, child, context = child_context()
    manager.mark_child_running(child.run_id)
    event = decisions("answer_generation_failed")[0].model_copy(update={"source": "llm"})
    payload = AgentTurnResult(
        run_id=child.run_id, session_id=child.session_id, trace_id=child.trace_id,
        answer=PLACEHOLDER, decision_events=[event],
    ).model_dump(mode="json")
    manager.complete_child_run(child.run_id, result_snapshot={"result_artifact_ref": {"payload": payload}})
    result = asyncio.run(ChildAgentExecutor(runner=SimpleNamespace(), run_manager=manager).execute(
        child_run_id=child.run_id, snapshot=context.snapshot, views=context.views,
    ))
    assert result.status == TaskResultStatus.COMPLETED
    assert result.missing_requirements == ()
