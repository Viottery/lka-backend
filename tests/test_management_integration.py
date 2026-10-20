"""Offline harness-to-tool-to-durable-dispatch acceptance checks."""
import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.safety import SafetyReviewMode
from app.core.tools import ToolContext
from tests.test_management_api import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def test_manager_creates_independent_task_and_reads_its_result(runtime):
    manager = runtime.management_service.create()
    sid = manager["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.start_task", tool_input={
            "command_id": "task-1", "purpose": "Produce a short result", "title": "First task",
        }),
        operation("final_answer", reason="Task accepted."), "Task queued.",
    ])
    result = runtime.run_agent_turn(session_id=sid, user_input="请创建一个任务，稍后我会询问结果。",
                                    safety_review_mode=SafetyReviewMode.SKIP)
    assert result.answer == "Task queued."
    event = next(event for event in result.tool_events if event.tool_name == "management.start_task")
    assert event.result["status"] == "completed", event.model_dump()
    receipt = event.result["output"]
    task = runtime.agent_run_manager.get_run(receipt["run_id"])
    assert task.parent_run_id is None and task.status.value == "queued"
    assert task.metadata["orchestrator"] == "langgraph"
    assert task.metadata["input_envelope"]["original_user_quotes"] == ["请创建一个任务，稍后我会询问结果。"]
    assert task.session_id != sid
    assert runtime.agent_run_manager.list_safety_reviews(result.run_id)
    # Finishing/pausing the manager does not stop the independent root.
    runtime.management_service.update(sid, expected_revision=2, status="paused")
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Independent task."), "TASK_RESULT_402",
    ])
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(task.run_id).status.value == "completed"
    messages = runtime.get_session(session_id=task.session_id).messages
    assert messages[0].role == "tool" and messages[0].payload["input_origin"] == "manager"
    assert messages[-1].content == "TASK_RESULT_402"
    # A later explicit user question can read the persisted result.
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.read_session", tool_input={"session_id": task.session_id}),
        operation("final_answer", reason="Read task output."), "TASK_RESULT_402",
    ])
    answer = runtime.run_agent_turn(session_id=sid, user_input="现在任务结果如何？")
    assert answer.answer == "TASK_RESULT_402"
    assert "TASK_RESULT_402" in json.dumps(answer.tool_events[0].result["output"])


def test_http_busy_conflict_and_device_independent_run_discovery(runtime, monkeypatch):
    from app.api import main

    monkeypatch.setattr(main, "LocalKnowledgeAgentRuntime", lambda settings: runtime)
    monkeypatch.setattr(main, "get_settings", lambda: runtime.settings)
    app = main.create_app()
    sid = runtime.create_session(title="Cross device").session.session_id
    run = runtime.create_agent_run(session_id=sid, user_input="First accepted task", resumable=True)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for path in ("/agent/turn", "/agent/turn/stream"):
                response = await client.post(path, json={"session_id": sid, "user_input": "Competing task"})
                assert response.status_code == 409, response.text
                assert response.json()["run_id"] == run.run_id
            response = await client.get(f"/sessions/{sid}/runs")
            assert response.status_code == 200
            assert response.json()["runs"][0]["run_id"] == run.run_id
            assert "user_input" not in response.text and "session_policy" not in response.text
            assert (await client.get("/sessions/missing/runs")).status_code == 404

    asyncio.run(exercise())


def test_accepted_dispatch_never_silently_downgrades_to_memory_checkpoint(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    manager_run = runtime.create_agent_run(session_id=sid, user_input="Create a task")
    runtime.agent_run_manager.mark_running(manager_run.run_id)
    context = ToolContext(session_id=sid, run_id=manager_run.run_id)
    receipt = runtime.managed_tasks.start_task(context, command_id="config-case", purpose="Task")
    client = ScriptedClient([])
    runtime.agent_turn_loop.llm_client = client
    runtime.local_app_config.agent.checkpoint_backend = "memory"
    assert runtime.agent_dispatch.run_one()
    failed = runtime.agent_run_manager.get_run(receipt["run_id"])
    assert failed.status.value == "failed" and failed.error_type == "checkpoint_configuration_required"
    assert not client.calls and not runtime.get_session(session_id=failed.session_id).messages
