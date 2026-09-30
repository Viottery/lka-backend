from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.main import create_app
from app.api.routes.agent import (
    _sse_event_frame,
    cancel_agent_child_run,
    cancel_agent_run,
    continue_agent_run,
    get_agent_run_snapshot,
    list_agent_run_events,
    reconnect_agent_turn_stream,
    resume_agent_run,
    retry_agent_child_run,
    stream_agent_turn,
)
from app.api.schemas import AgentTurnRequest, ContinueAgentRunRequest
from app.core.agent_runs import AgentRunEvent, AgentRunStatus, InMemoryAgentRunManager
from app.core.config import get_settings
from app.core.llm import LLMResponse, LLMStreamEvent
from app.core.multi_agent import (
    EvidenceRef,
    Plan,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    TaskResult,
    TaskResultStatus,
)
from app.domains.mail import MailAccountInput, MailMessageInput


async def _is_never_disconnected() -> bool:
    return False


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_agent_turn_stream_emits_run_tool_and_final_events(
    tmp_path,
    monkeypatch,
    orchestrator,
):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        f'[agent]\norchestrator = "{orchestrator}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()

    app = create_app()
    app.state.runtime.import_mail(
        account=MailAccountInput(
            provider="local_json",
            email_address="user@example.com",
        ),
        messages=[
            MailMessageInput(
                external_id="agent_stream_ntuso_001",
                folder="Inbox",
                subject="NTUSO Audition stream",
                sender="ntuso@example.com",
                to=["user@example.com"],
                received_at="2026-08-05T09:30:00Z",
                body_text="NTUSO stream endpoint sentinel.",
            )
        ],
    )
    app.state.runtime.agent_turn_loop.llm_client = _StreamingAnswerLLM()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)

    response = _run_async(
        stream_agent_turn(
            AgentTurnRequest(
                session_id="session_agent_stream_ntuso",
                user_input="帮我查询 NTUSO 的乐团考试相关要求",
            ),
            request,
        )
    )
    assert response.media_type == "text/event-stream"
    text = _run_async(_consume_stream_response(response))

    frames = _parse_sse(text)
    event_types = [frame["event"] for frame in frames]

    assert event_types[0] == "run_started"
    assert "package_selected" in event_types
    assert "tool_started" in event_types
    assert "tool_completed" in event_types
    assert "final_answer" in event_types
    assert event_types[-1] == "run_completed"

    run_ids = {frame["data"]["run_id"] for frame in frames}
    assert len(run_ids) == 1
    run_id = run_ids.pop()
    sequences = [frame["data"]["sequence"] for frame in frames]
    assert sequences == list(range(1, len(sequences) + 1))
    assert all(frame["id"] == f"{run_id}:{frame['data']['sequence']}" for frame in frames)

    run = app.state.runtime.agent_run_manager.get_run(run_id)
    assert run is not None
    assert run.status == "completed"
    assert run.session_id == "session_agent_stream_ntuso"
    assert run.result_snapshot["selected_package"] == "mail"


def test_sse_event_frame_marks_safety_review_events():
    event = AgentRunEvent(
        event_id="evt_safety_review_required",
        run_id="run_safety_review",
        sequence=3,
        type="safety_review_required",
        stage="safety_review",
        message="Safety review required.",
        payload={"review": {"review_id": "review_001"}},
        created_at="2026-08-22T00:00:00Z",
    )

    [frame] = _parse_sse(_sse_event_frame(event))

    assert frame["event"] == "safety_review_required"
    assert frame["data"]["stream_part"] == "safety_review"


def test_agent_turn_stream_emits_provider_token_delta_events(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    app.state.runtime.import_mail(
        account=MailAccountInput(
            provider="local_json",
            email_address="user@example.com",
        ),
        messages=[
            MailMessageInput(
                external_id="agent_stream_delta_001",
                folder="Inbox",
                subject="NTUSO Delta stream",
                sender="ntuso@example.com",
                to=["user@example.com"],
                received_at="2026-08-05T09:30:00Z",
                body_text="Token delta stream source mail.",
            )
        ],
    )
    app.state.runtime.agent_turn_loop.llm_client = _StreamingAnswerLLM()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)

    response = _run_async(
        stream_agent_turn(
            AgentTurnRequest(
                session_id="session_agent_stream_delta",
                user_input="帮我查询 NTUSO 的乐团考试相关要求",
                llm={"response_mode": "stream"},
            ),
            request,
        )
    )
    text = _run_async(_consume_stream_response(response))
    frames = _parse_sse(text)
    event_types = [frame["event"] for frame in frames]

    assert "tool_completed" in event_types
    assert "llm_delta" in event_types
    assert event_types[-1] == "run_completed"
    assert {frame["data"]["stream_part"] for frame in frames}.issuperset(
        {"lifecycle", "tool_result", "llm_delta", "llm_audit", "final_answer"}
    )

    delta_frames = [frame for frame in frames if frame["event"] == "llm_delta"]
    answer_delta_frames = [
        frame
        for frame in delta_frames
        if frame["data"]["payload"]["content_role"] == "final_answer"
    ]
    assert [frame["data"]["payload"]["delta"] for frame in answer_delta_frames] == [
        "流式",
        "回答",
    ]
    assert all(
        frame["data"]["payload"]["display_target"] == "assistant_answer"
        for frame in answer_delta_frames
    )
    assert answer_delta_frames[-1]["data"]["payload"]["content_snapshot"] == "流式回答"
    assert {frame["data"]["payload"]["content_role"] for frame in delta_frames}.issuperset(
        {"route_decision", "agent_decision", "final_answer"}
    )
    assert all(
        frame["data"]["payload"]["display_target"] == "agent_process"
        for frame in delta_frames
        if frame["data"]["payload"]["content_role"] != "final_answer"
    )

    final_answer = next(frame for frame in frames if frame["event"] == "final_answer")
    assert final_answer["data"]["stream_part"] == "final_answer"
    assert "流式回答" in final_answer["data"]["message"]
    first_answer_delta_index = frames.index(answer_delta_frames[0])
    final_answer_index = frames.index(final_answer)
    assert first_answer_delta_index < final_answer_index


def test_agent_turn_stream_defaults_llm_options_without_response_mode_to_stream(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    app.state.runtime.import_mail(
        account=MailAccountInput(
            provider="local_json",
            email_address="user@example.com",
        ),
        messages=[
            MailMessageInput(
                external_id="agent_stream_llm_options_001",
                folder="Inbox",
                subject="NTUSO Delta stream with model override",
                sender="ntuso@example.com",
                to=["user@example.com"],
                received_at="2026-08-05T09:30:00Z",
                body_text="Token delta stream source mail with model override.",
            )
        ],
    )
    app.state.runtime.agent_turn_loop.llm_client = _StreamingAnswerLLM()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)

    response = _run_async(
        stream_agent_turn(
            AgentTurnRequest(
                session_id="session_agent_stream_llm_options",
                user_input="帮我查询 NTUSO 的乐团考试相关要求",
                llm={"model": "streaming-model"},
            ),
            request,
        )
    )
    frames = _parse_sse(_run_async(_consume_stream_response(response)))

    answer_delta_frames = [
        frame
        for frame in frames
        if frame["event"] == "llm_delta"
        and frame["data"]["payload"]["display_target"] == "assistant_answer"
    ]
    assert [frame["data"]["payload"]["delta"] for frame in answer_delta_frames] == [
        "流式",
        "回答",
    ]
    final_answer = next(frame for frame in frames if frame["event"] == "final_answer")
    assert frames.index(answer_delta_frames[0]) < frames.index(final_answer)


def test_agent_turn_stream_endpoint_is_registered_without_changing_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    openapi = app.openapi()["paths"]

    assert "/agent/turn" in openapi
    assert "/agent/turn/stream" in openapi
    assert "/agent/runs/{run_id}" in openapi
    assert "/agent/runs/{run_id}/events" in openapi
    assert "/agent/runs/{run_id}/stream" in openapi
    assert "/agent/runs/{run_id}/cancel" in openapi
    assert "/agent/runs/{run_id}/resume" in openapi
    assert "/agent/runs/{run_id}/snapshot" in openapi
    assert "/agent/runs/{parent_run_id}/children/{child_run_id}/cancel" in openapi
    assert "/agent/runs/{parent_run_id}/children/{child_run_id}/retry" in openapi


def test_parent_snapshot_projects_plan_children_results_and_hides_private_context(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    manager = runtime.agent_run_manager
    parent = runtime.create_agent_run(session_id="session_snapshot", user_input="private parent prompt")
    manager.mark_running(parent.run_id)
    plan = Plan(
        correlation_id=parent.trace_id,
        plan_id="plan_snapshot",
        parent_run_id=parent.run_id,
        session_id=parent.session_id,
        objective="Review a local project.",
        status=PlanStatus.RUNNING,
        steps=(PlanStep(
            correlation_id=parent.trace_id,
            step_id="inspect",
            objective="Inspect selected files.",
            output_contract="summary",
            status=PlanStepStatus.COMPLETED,
        ),),
    )
    manager.record_multi_agent_plan(
        parent.run_id,
        event_type="multi_agent_plan_validated",
        payload={"plan_id": plan.plan_id},
        plan=plan.model_dump(mode="json"),
    )
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id=plan.plan_id,
        step_id="inspect",
        attempt=1,
        user_input="private child prompt",
    )
    manager._update_run(
        child.run_id,
        status=child.status,
        metadata_patch={"context_snapshot": {"prompt": "PRIVATE_CONTEXT_PROMPT"}},
    )
    manager.mark_child_running(child.run_id)
    manager.complete_child_run(
        child.run_id,
        result_snapshot={"answer": "PRIVATE_CHILD_RESULT"},
    )
    runtime.agent_run_store.put_artifact(
        artifact_id="artifact_snapshot",
        run_id=child.run_id,
        kind="report",
        payload={"content": "PRIVATE_ARTIFACT_CONTENT"},
        summary="A safe artifact summary.",
        created_at=child.created_at,
    )
    task_result = TaskResult(
        correlation_id=parent.trace_id,
        result_id="result_snapshot",
        child_run_id=child.run_id,
        plan_id=plan.plan_id,
        step_id="inspect",
        snapshot_id="snapshot_1",
        status=TaskResultStatus.COMPLETED,
        summary="A concise completed result.",
        artifact_refs=("artifact_snapshot",),
        evidence_refs=(EvidenceRef(
            evidence_id="evidence_1",
            source_ref="workspace://readme.md",
            content_hash="abc123",
        ),),
    )
    manager.append_event(
        parent.run_id,
        "subtask_result",
        "Child result recorded.",
        stage="subtask",
        payload={"task_result": task_result.model_dump(mode="json")},
        parent_run_id=parent.run_id,
        child_run_id=child.run_id,
        plan_id=plan.plan_id,
        step_id="inspect",
        attempt=1,
    )
    request = SimpleNamespace(app=app)

    snapshot = _run_async(get_agent_run_snapshot(parent.run_id, request))
    assert snapshot.plan["plan_id"] == plan.plan_id
    [child_view] = snapshot.children
    assert child_view.attempt == 1
    assert child_view.status.value == "completed"
    assert child_view.step_status == "completed"
    assert child_view.result.summary == "A concise completed result."
    assert child_view.result.artifacts[0]["artifact_id"] == "artifact_snapshot"
    assert child_view.result.evidence_refs[0]["evidence_id"] == "evidence_1"
    serialized = snapshot.model_dump_json()
    assert "PRIVATE_CONTEXT_PROMPT" not in serialized
    assert "PRIVATE_CHILD_RESULT" not in serialized
    assert "PRIVATE_ARTIFACT_CONTENT" not in serialized
    assert "private child prompt" not in serialized


def test_parent_scheduler_sse_replays_progress_after_sequence_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)
    run = app.state.runtime.create_agent_run(
        session_id="session_parent_sse", user_input="parent task"
    )
    app.state.runtime.agent_run_manager.append_event(
        run.run_id,
        "subtask_started",
        "Scheduler started a child step.",
        stage="subtask",
        payload={"step_id": "inspect", "attempt": 1},
        parent_run_id=run.run_id,
        child_run_id="child_sse",
        step_id="inspect",
        attempt=1,
    )
    app.state.runtime.agent_run_manager.append_event(
        run.run_id,
        "subtask_result",
        "Scheduler recorded a child result.",
        stage="subtask",
        payload={
            "task_result": TaskResult(
                correlation_id=run.trace_id,
                result_id="result_sse",
                child_run_id="child_sse",
                plan_id="plan_sse",
                step_id="inspect",
                snapshot_id="snapshot_sse",
                status=TaskResultStatus.COMPLETED,
                summary="Safe scheduler result.",
            ).model_dump(mode="json"),
            "prompt": "PRIVATE_SCHEDULER_PROMPT",
        },
        parent_run_id=run.run_id,
        child_run_id="child_sse",
        plan_id="plan_sse",
        step_id="inspect",
        attempt=1,
    )
    app.state.runtime.agent_run_manager.append_event(
        run.run_id,
        "subtask_completed",
        "Scheduler completed a child step.",
        stage="subtask",
        payload={"step_id": "inspect", "attempt": 1},
        parent_run_id=run.run_id,
        child_run_id="child_sse",
        step_id="inspect",
        attempt=1,
    )
    app.state.runtime.agent_run_manager.complete_run(run.run_id)

    replay = _run_async(reconnect_agent_turn_stream(run.run_id, request, after_sequence=1))
    frames = _parse_sse(_run_async(_consume_stream_response(replay)))
    assert [frame["event"] for frame in frames] == ["subtask_result", "subtask_completed"]
    assert [frame["data"]["sequence"] for frame in frames] == [2, 3]
    assert frames[0]["data"]["payload"]["task_result"]["summary"] == "Safe scheduler result."
    assert "PRIVATE_SCHEDULER_PROMPT" not in json.dumps(frames, ensure_ascii=False)


def test_child_cancel_api_checks_ownership_and_uses_scheduler_hook():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_child_cancel_api", user_input="parent")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan_cancel_api",
        step_id="inspect",
        attempt=1,
        user_input="child task",
    )
    calls = []

    async def resume_parent(parent_run_id):
        calls.append(parent_run_id)

    runtime = SimpleNamespace(
        agent_run_manager=manager,
        cancel_multi_agent_child=lambda **kwargs: manager.cancel_run(
            kwargs["child_run_id"], reason="Child run cancelled by user."
        ),
        resume_multi_agent_parent_async=resume_parent,
        _agent_turn_tasks={},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))

    async def cancel():
        response = await cancel_agent_child_run(parent.run_id, child.run_id, request)
        await asyncio.sleep(0)
        return response

    response = _run_async(cancel())
    assert response.status == AgentRunStatus.CANCELLED
    assert calls == [parent.run_id]
    with pytest.raises(HTTPException) as error:
        _run_async(cancel_agent_child_run("different_parent", child.run_id, request))
    assert getattr(error.value, "status_code", None) == 404


def test_child_resume_requests_share_the_runner_resume_task():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_child_resume_api", user_input="parent")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan_resume_api",
        step_id="inspect",
        attempt=1,
        user_input="child task",
    )
    manager.mark_child_running(child.run_id)
    resume_calls = []
    parent_resume_calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def resume_agent_run_async(run_id):
        assert runtime.agent_turn_runner is runtime.child_agent_executor.runner
        resume_calls.append(run_id)
        started.set()
        await release.wait()
        manager.complete_child_run(run_id, result_snapshot={"answer": "done"})

    async def resume_parent(parent_run_id):
        parent_resume_calls.append(parent_run_id)

    runner = SimpleNamespace(orchestrator_name="langgraph")
    runtime = SimpleNamespace(
        agent_run_manager=manager,
        agent_turn_runner=runner,
        child_agent_executor=SimpleNamespace(runner=runner),
        resume_agent_run_async=resume_agent_run_async,
        resume_multi_agent_parent_async=resume_parent,
        _agent_turn_tasks={},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))

    async def concurrent_resume_requests():
        first = await resume_agent_run(child.run_id, request)
        await started.wait()
        second = await resume_agent_run(child.run_id, request)
        assert first.run_id == child.run_id
        assert second.run_id == child.run_id
        assert resume_calls == [child.run_id]
        task = runtime._agent_turn_tasks[child.run_id]
        release.set()
        await task

    _run_async(concurrent_resume_requests())
    assert resume_calls == [child.run_id]
    assert parent_resume_calls == [parent.run_id]
    assert manager.get_run(child.run_id).status == AgentRunStatus.COMPLETED


def test_child_retry_api_checks_ownership_and_delegates_retry_policy(monkeypatch):
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_child_retry_api", user_input="parent")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan_retry_api",
        step_id="inspect",
        attempt=1,
        user_input="child task",
    )
    manager.fail_child_run(child.run_id, error_type="test", error="failed")
    retry_calls = []
    resume_calls = []

    async def retry(**kwargs):
        retry_calls.append(kwargs)

    async def resume_parent(parent_run_id):
        resume_calls.append(parent_run_id)

    runtime = SimpleNamespace(
        agent_run_manager=manager,
        retry_multi_agent_child=retry,
        resume_multi_agent_parent_async=resume_parent,
        _agent_turn_tasks={},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    monkeypatch.setattr(
        "app.api.routes.agent._start_parent_scheduler_resume_task",
        lambda *, runtime, parent_run_id: resume_calls.append(parent_run_id),
    )

    response = _run_async(retry_agent_child_run(parent.run_id, child.run_id, request))
    assert response.run.run_id == parent.run_id
    assert response.children[0].run_id == child.run_id
    assert retry_calls == [{"parent_run_id": parent.run_id, "child_run_id": child.run_id}]
    assert resume_calls == [parent.run_id]

    with pytest.raises(HTTPException) as error:
        _run_async(retry_agent_child_run("different_parent", child.run_id, request))
    assert getattr(error.value, "status_code", None) == 404
    assert len(retry_calls) == 1

    async def reject_retry(**_kwargs):
        raise ValueError("Only failed or timed-out child runs can be retried.")

    runtime.retry_multi_agent_child = reject_retry
    with pytest.raises(HTTPException) as error:
        _run_async(retry_agent_child_run(parent.run_id, child.run_id, request))
    assert getattr(error.value, "status_code", None) == 409


def test_agent_turn_stream_reconnects_from_sequence_and_cancel_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    app.state.runtime.agent_turn_loop.llm_client = _StreamingAnswerLLM()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)

    response = _run_async(
        stream_agent_turn(
            AgentTurnRequest(session_id="session_reconnect", user_input="直接回答。"),
            request,
        )
    )
    frames = _parse_sse(_run_async(_consume_stream_response(response)))
    run_id = frames[0]["data"]["run_id"]
    reconnect_after = frames[2]["data"]["sequence"]
    replay = _run_async(reconnect_agent_turn_stream(run_id, request, reconnect_after))
    replay_frames = _parse_sse(_run_async(_consume_stream_response(replay)))

    assert replay_frames
    assert [frame["data"]["sequence"] for frame in replay_frames] == list(
        range(reconnect_after + 1, frames[-1]["data"]["sequence"] + 1)
    )

    queued = app.state.runtime.create_agent_run(
        session_id="session_cancel", user_input="cancel this run"
    )
    cancelled = _run_async(cancel_agent_run(queued.run_id, request))
    assert cancelled.run_id == queued.run_id
    assert cancelled.status == "cancelled"
    assert app.state.runtime.agent_run_manager.is_cancel_requested(queued.run_id) is True


def test_agent_run_event_query_and_sse_hide_raw_tool_result_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)
    run = app.state.runtime.create_agent_run(
        session_id="session_public_events", user_input="public event test"
    )
    app.state.runtime.agent_run_manager.append_event(
        run.run_id,
        "tool_completed",
        "Tool completed.",
        stage="tool_execute",
        payload={
            "tool_name": "mail.load_messages",
            "status": "completed",
            "metadata": {"result": {"body_text": "PRIVATE_TOOL_RESULT_SENTINEL"}},
        },
    )

    queried = _run_async(list_agent_run_events(run.run_id, request))
    assert queried.events[0].payload == {
        "tool_name": "mail.load_messages",
        "status": "completed",
    }
    [frame] = _parse_sse(_sse_event_frame(queried.events[0]))
    assert "PRIVATE_TOOL_RESULT_SENTINEL" not in json.dumps(frame, ensure_ascii=False)


def test_agent_run_event_query_and_sse_hide_internal_log_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)
    run = app.state.runtime.create_agent_run(
        session_id="session_public_lifecycle", user_input="public lifecycle event test"
    )
    app.state.runtime.agent_run_manager.append_event(
        run.run_id,
        "run_completed",
        "Agent run completed.",
        stage="run",
        payload={
            "answer_length": 12,
            "log_path": "/private/data/agent_logs/PRIVATE_LOG_PATH.md",
        },
    )

    queried = _run_async(list_agent_run_events(run.run_id, request))
    assert queried.events[0].payload == {"answer_length": 12}
    [frame] = _parse_sse(_sse_event_frame(queried.events[0]))
    assert "PRIVATE_LOG_PATH" not in json.dumps(frame, ensure_ascii=False)


def test_agent_run_event_query_and_sse_hide_raw_safety_review_input(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)
    run = app.state.runtime.create_agent_run(
        session_id="session_public_review", user_input="public review event test"
    )
    app.state.runtime.agent_run_manager.append_event(
        run.run_id,
        "safety_review_required",
        "Safety review required.",
        stage="safety_review",
        payload={
            "review": {
                "review_id": "review_public_event",
                "run_id": run.run_id,
                "session_id": run.session_id,
                "trace_id": run.trace_id,
                "invocation_id": "invocation_public_event",
                "tool_name": "filesystem.edit_file",
                "tool_input": {"replacement": "PRIVATE_REVIEW_INPUT_SENTINEL"},
                "tool_risk": "high",
                "side_effects": ["writes files"],
                "read_only": False,
                "mode": "manual",
                "reason": "Write requires confirmation.",
                "created_at": run.created_at,
                "status": "pending",
                "llm_output": "PRIVATE_REVIEW_LLM_SENTINEL",
            }
        },
    )

    queried = _run_async(list_agent_run_events(run.run_id, request))
    public_review = queried.events[0].payload["review"]
    assert "tool_input" not in public_review
    assert "llm_output" not in public_review
    [frame] = _parse_sse(_sse_event_frame(queried.events[0]))
    serialized = json.dumps(frame, ensure_ascii=False)
    assert "PRIVATE_REVIEW_INPUT_SENTINEL" not in serialized
    assert "PRIVATE_REVIEW_LLM_SENTINEL" not in serialized


def test_final_answer_sse_frame_uses_full_answer_from_metadata():
    answer = "完整答案" * 220
    event = AgentRunEvent(
        event_id="agent_run_long_event_000001",
        run_id="agent_run_long",
        sequence=1,
        type="final_answer",
        stage="answer",
        message=answer[:497] + "...",
        payload={"metadata": {"answer": answer}},
        created_at="2026-08-15T00:00:00+00:00",
    )

    frame = _parse_sse(_sse_event_frame(event))[0]

    assert frame["event"] == "final_answer"
    assert frame["data"]["message"] == answer
    assert not frame["data"]["message"].endswith("...")
    assert frame["data"]["stream_part"] == "final_answer"


def _parse_sse(text: str) -> list[dict]:
    frames = []
    for raw_frame in text.strip().split("\n\n"):
        if not raw_frame.strip():
            continue
        fields = {}
        for line in raw_frame.splitlines():
            name, value = line.split(": ", 1)
            fields[name] = value
        frames.append(
            {
                "id": fields["id"],
                "event": fields["event"],
                "data": json.loads(fields["data"]),
            }
        )
    return frames


async def _consume_stream_response(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return "".join(chunks)


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)


def test_user_continuation_route_journals_without_hook_then_replays_and_schedules():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="session_continue_route", user_input="clarify")
    manager.mark_running(run.run_id)
    manager.mark_waiting_user(
        run.run_id,
        question_id="question_route_1",
        patch_id="patch_route_1",
        question="Which date range should I use?",
    )
    runtime = SimpleNamespace(agent_run_manager=manager, _agent_turn_tasks={})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    payload = ContinueAgentRunRequest(
        command_id="continue_route_cmd_1",
        answer="Use the last quarter.",
    )

    with pytest.raises(HTTPException) as waiting_resume_error:
        _run_async(resume_agent_run(run.run_id, request))
    assert waiting_resume_error.value.status_code == 409

    async def first_submit_without_runtime_hook():
        response = await continue_agent_run(run.run_id, payload, request)
        assert response.status == AgentRunStatus.RUNNING
        assert response.resume_scheduled is False
        assert response.replayed is False
        assert manager.get_user_continuation(run.run_id, payload.command_id) == "Use the last quarter."
        snapshot = await get_agent_run_snapshot(run.run_id, request)
        assert snapshot.run.pending_user_question is None
        assert "Use the last quarter." not in snapshot.model_dump_json()

    _run_async(first_submit_without_runtime_hook())

    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def resume_multi_agent_user_question_async(run_id, command_id):
        calls.append((run_id, command_id, manager.get_user_continuation(run_id, command_id)))
        started.set()
        await release.wait()

    runtime.resume_multi_agent_user_question_async = resume_multi_agent_user_question_async

    async def retry_same_command_recovers_dispatch_once():
        response = await continue_agent_run(run.run_id, payload, request)
        assert response.resume_scheduled is True
        assert response.replayed is True
        await started.wait()
        duplicate = await continue_agent_run(run.run_id, payload, request)
        assert duplicate.resume_scheduled is True
        assert duplicate.replayed is True
        assert calls == [(run.run_id, payload.command_id, "Use the last quarter.")]
        task = runtime._agent_turn_tasks[run.run_id]
        release.set()
        await task

    _run_async(retry_same_command_recovers_dispatch_once())


def test_agent_snapshot_exposes_pending_user_question():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_user_snapshot", user_input="parent")
    manager.mark_running(parent.run_id)
    manager.mark_waiting_user(
        parent.run_id,
        question_id="question_parent_already_answered",
        question="Old question that should not reappear.",
    )
    manager.continue_user_question(
        run_id=parent.run_id,
        command_id="command_parent_already_answered",
        answer="The old answer.",
    )
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan_user_snapshot",
        step_id="clarify",
        attempt=1,
        user_input="child",
    )
    manager.mark_child_running(child.run_id)
    manager.mark_waiting_user(
        child.run_id,
        question_id="question_snapshot_1",
        question="Which date range should I use?",
    )
    manager.mark_waiting_for_child_user(parent.run_id, [child.run_id])
    runtime = SimpleNamespace(agent_run_manager=manager)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))

    response = _run_async(get_agent_run_snapshot(parent.run_id, request))
    pending = response.children[0].pending_user_question
    assert pending is not None
    assert pending.question_id == "question_snapshot_1"
    assert pending.question == "Which date range should I use?"
    assert response.children[0].status == AgentRunStatus.WAITING_USER
    assert response.run.pending_user_question is None
    assert response.run.status == AgentRunStatus.WAITING_USER
    assert "Old question that should not reappear." not in response.model_dump_json()
    assert "The old answer." not in response.model_dump_json()
    with pytest.raises(HTTPException) as parent_continue_error:
        _run_async(continue_agent_run(
            parent.run_id,
            ContinueAgentRunRequest(command_id="wrong_parent_continue", answer="No parent question."),
            request,
        ))
    assert parent_continue_error.value.status_code == 409
    with pytest.raises(HTTPException) as parent_resume_error:
        _run_async(resume_agent_run(parent.run_id, request))
    assert parent_resume_error.value.status_code == 409

    parent_question_run = manager.create_run(
        session_id="session_root_user_snapshot",
        user_input="parent clarification",
    )
    manager.mark_running(parent_question_run.run_id)
    manager.mark_waiting_user(
        parent_question_run.run_id,
        question_id="question_root_snapshot",
        question="Should I include archived items?",
    )
    parent_response = _run_async(get_agent_run_snapshot(parent_question_run.run_id, request))
    assert parent_response.run.status == AgentRunStatus.WAITING_USER
    assert parent_response.run.pending_user_question.question_id == "question_root_snapshot"


class _StreamingAnswerLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        client_name: str | None = None,
        model: str | None = None,
        response_mode=None,
        require_json: bool = False,
        metadata: dict | None = None,
    ) -> LLMResponse:
        self.calls += 1
        content = self._content_for_prompts(system_prompt=system_prompt, user_prompt=user_prompt)
        return LLMResponse(
            provider="streaming_fake",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )

    async def stream(self, request):
        stage = str(request.metadata.get("stage") or "answer")
        messages = {message.role: message.content for message in request.messages}
        content = self._content_for_prompts(
            system_prompt=messages.get("system", ""),
            user_prompt=messages.get("user", ""),
        )
        yield LLMStreamEvent(
            event_type="llm_started",
            stage=stage,
            client_name=request.client_name or "streaming_fake",
            provider="streaming_fake",
            model=request.model or "streaming-model",
        )
        snapshot = ""
        deltas = ["流式", "回答"] if stage == "answer" else [content]
        for delta in deltas:
            snapshot += delta
            yield LLMStreamEvent(
                event_type="llm_delta",
                stage=stage,
                client_name=request.client_name or "streaming_fake",
                provider="streaming_fake",
                model=request.model or "streaming-model",
                delta=delta,
                content_snapshot=snapshot,
            )
        yield LLMStreamEvent(
            event_type="llm_completed",
            stage=stage,
            client_name=request.client_name or "streaming_fake",
            provider="streaming_fake",
            model=request.model or "streaming-model",
            content_snapshot=snapshot,
        )

    def _content_for_prompts(self, *, system_prompt: str, user_prompt: str) -> str:
        if "Choose at most one tool package" in system_prompt:
            return (
                '{"selected_package":"mail","reason":"test route",'
                '"search_query":"NTUSO"}'
            )
        if "Tool Result Checker" in system_prompt:
            return '{"status":"accepted","message":"ok","remaining_work":""}'
        if "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            if not observations:
                return json.dumps(
                    {
                        "operation": {
                            "type": "tool_call",
                            "tool_name": "mail.search",
                            "tool_input": {"query": "NTUSO", "limit": 8},
                            "reason": "Search mail.",
                        },
                        "assistant_message": "检索邮件。",
                    }
                )
            if observations[-1]["tool_name"] == "mail.search":
                message_ids = [
                    message["message_id"]
                    for message in observations[-1]["result"]["output"]["messages"][:1]
                ]
                return json.dumps(
                    {
                        "operation": {
                            "type": "tool_call",
                            "tool_name": "mail.load_messages",
                            "tool_input": {"message_ids": message_ids},
                            "reason": "Load message.",
                        },
                        "assistant_message": "读取邮件。",
                    }
                )
            return json.dumps(
                {
                    "operation": {
                        "type": "final_answer",
                        "final_answer": "decision text must not stream as the answer",
                        "reason": "Loaded mail evidence is sufficient.",
                    },
                    "assistant_message": "准备生成最终回答。",
                }
            )
        return "流式回答"
