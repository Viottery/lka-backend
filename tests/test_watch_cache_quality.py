"""Watch-derived scopes must permit reading their own gated tool results.

The real adapter, tool lifecycle, file reader, cache and executor are used.
The child invocation is intercepted to exercise its immutable view directly;
this does not measure model planning or daily briefing quality.
"""

import asyncio
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.core import agent_turn
from app.core.config import get_settings
from app.core.multi_agent import TaskResult, TaskResultStatus
from app.core.tools import ToolContext, ToolRegistry, ToolSpec
from app.core.watch_execution import WatchExecutionAdapter


def test_watch_can_recover_long_result_without_foreign_cache_or_file_access(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "daily.txt"
    source.write_text("a" * 6500 + "CURRENT-WATCH-739" + "b" * 6500, encoding="utf-8")
    outside = tmp_path / "ungranted.txt"
    outside.write_text("OUTSIDE-PRIVATE", encoding="utf-8")
    original_hash = sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    previous_artifacts = []

    async def exercise_child(**kwargs):
        child_id = kwargs["child_run_id"]
        runtime.agent_run_manager.mark_child_running(child_id)
        child = runtime.agent_run_manager.get_run(child_id)
        context = ToolContext(session_id=child.session_id, run_id=child_id,
            trace_id=child.trace_id, workspace_root=str(workspace), tool_view=kwargs["views"].tool)
        tokens = [(variable, variable.set(value)) for variable, value in (
            (agent_turn._turn_run_id, child_id),
            (agent_turn._turn_run_manager, runtime.agent_run_manager),
        )]
        try:
            loop = runtime.agent_turn_loop
            result = loop._execute_tool(tool_name="filesystem.read_file",
                tool_input={"path": str(source)}, context=context, tool_events=[])
            assert result.status == "completed", result.error
            observation = loop._observation_for_decision_prompt(tool_name=result.tool_name,
                tool_input={"path": str(source)}, tool_result=result, feedback={}, run_id=child_id)
            artifact_id = observation["_result_cache"]["artifact_id"]
            found = runtime.tool_executor.execute(invocation_id="recover", tool_name="observation.search",
                tool_input={"artifact_id": artifact_id, "path": "/output", "query": "CURRENT-WATCH-739"},
                context=context)
            assert found.status == "completed", found.error
            assert any("CURRENT-WATCH-739" in item["snippet"] for item in found.output["matches"])
            page = runtime.tool_executor.execute(invocation_id="page", tool_name="observation.read",
                tool_input={"artifact_id": artifact_id, "path": "/output"}, context=context)
            assert page.status == "completed", page.error
            for other in previous_artifacts:
                rejected = runtime.tool_executor.execute(invocation_id="foreign", tool_name="observation.read",
                    tool_input={"artifact_id": other}, context=context)
                assert rejected.status == "rejected"
            previous_artifacts.append(artifact_id)
            forbidden = runtime.tool_executor.execute(invocation_id="outside", tool_name="filesystem.read_file",
                tool_input={"path": str(outside)}, context=context)
            assert forbidden.status == "rejected"
            write = runtime.tool_executor.execute(invocation_id="write", tool_name="filesystem.edit_file",
                tool_input={"path": str(source), "old_text": "a", "new_text": "x",
                            "expected_sha256": original_hash}, context=context)
            assert write.status == "rejected"
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)
        return TaskResult(correlation_id=child.trace_id, result_id=f"result-{child_id}",
            child_run_id=child_id, plan_id=kwargs["snapshot"].plan_id,
            step_id=kwargs["snapshot"].step_id, snapshot_id=kwargs["snapshot"].snapshot_id,
            status=TaskResultStatus.COMPLETED, summary="CURRENT-WATCH-739")

    runtime.run_child_agent_async = exercise_child
    try:
        for _ in range(2):
            parent = runtime.create_agent_run(session_id="watch-root", user_input="Read granted note.")
            runtime.agent_run_manager.mark_running(parent.run_id)
            result = asyncio.run(WatchExecutionAdapter(runtime=runtime).run_async(
                parent_run_id=parent.run_id, watch_id="daily-note", goal="Read the current granted note.",
                workspace_paths=(str(workspace),), instruction_tools_enabled=False))
            assert result.summary == "CURRENT-WATCH-739"
        assert sha256(source.read_bytes()).hexdigest() == original_hash
    finally:
        runtime.stop()
        get_settings.cache_clear()


@pytest.mark.parametrize("requested_web", [False, True], ids=["no-grants", "unregistered-source"])
def test_cache_helpers_alone_cannot_authorize_a_watch(tmp_path, monkeypatch, requested_web):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    runtime.tool_registry = ToolRegistry()
    for name in ("observation.read", "observation.search", "observation.group"):
        runtime.tool_registry.register_tool(SimpleNamespace(spec=ToolSpec(
            name=name, package="observation", type="local_tool", description="Cache helper.", read_only=True)))
    parent = runtime.create_agent_run(session_id="no-source", user_input="No grants.")
    try:
        with pytest.raises(ValueError, match="no authorized|no registered"):
            asyncio.run(WatchExecutionAdapter(runtime=runtime).run_async(
                parent_run_id=parent.run_id, watch_id="empty", goal="No source available.",
                web_enabled=requested_web))
        assert runtime.agent_run_manager.get_run(parent.run_id).child_run_ids == ()
    finally:
        runtime.stop()
        get_settings.cache_clear()
