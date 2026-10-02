from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.multi_agent import EvidenceRef, TaskResult, TaskResultStatus
from app.core.tools import ToolPackageSpec, ToolRegistry, ToolSpec
from app.core.watch_execution import (
    WATCH_MAX_LLM_CALLS,
    WATCH_MAX_TOKENS,
    WATCH_MAX_TOOL_CALLS,
    WATCH_MAX_WALL_TIME_SECONDS,
    WatchExecutionAdapter,
)


class _Runtime:
    def __init__(self) -> None:
        self.agent_run_manager = InMemoryAgentRunManager()
        self.tool_registry = ToolRegistry()
        self.calls = []
        for package in ("mail", "knowledge", "web", "matter", "bash", "filesystem"):
            self.tool_registry.register_package(ToolPackageSpec(name=package, description=package))
        specs = (
            ToolSpec(
                name="mail.search",
                package="mail",
                type="local_tool",
                description="",
                read_only=True,
            ),
            ToolSpec(
                name="mail.load_messages",
                package="mail",
                type="local_tool",
                description="",
                read_only=True,
            ),
            ToolSpec(
                name="mail.sync", package="mail", type="local_tool", description="", read_only=False
            ),
            ToolSpec(
                name="knowledge.search",
                package="knowledge",
                type="local_tool",
                description="",
                read_only=True,
            ),
            ToolSpec(
                name="knowledge.load_chunks",
                package="knowledge",
                type="local_tool",
                description="",
                read_only=True,
            ),
            ToolSpec(
                name="web.search", package="web", type="local_tool", description="", read_only=True
            ),
            ToolSpec(
                name="web.open", package="web", type="local_tool", description="", read_only=True
            ),
            ToolSpec(
                name="matter.search",
                package="matter",
                type="local_tool",
                description="",
                read_only=True,
            ),
            ToolSpec(
                name="matter.list",
                package="matter",
                type="local_tool",
                description="",
                read_only=True,
            ),
            ToolSpec(
                name="matter.create",
                package="matter",
                type="local_tool",
                description="",
                read_only=False,
            ),
            ToolSpec(
                name="bash.run", package="bash", type="local_tool", description="", read_only=False
            ),
            ToolSpec(
                name="filesystem.read_file",
                package="filesystem",
                type="local_tool",
                description="",
                read_only=True,
            ),
        )
        for spec in specs:
            self.tool_registry.register_tool(type("RegisteredTool", (), {"spec": spec})())

    async def run_child_agent_async(self, **kwargs):
        self.calls.append(kwargs)
        refs = (EvidenceRef(evidence_id="ev_1", source_ref="https://example.test/source"),)
        result = TaskResult(
            correlation_id="trace",
            result_id="result_1",
            child_run_id=kwargs["child_run_id"],
            plan_id=kwargs["snapshot"].plan_id,
            step_id=kwargs["snapshot"].step_id,
            snapshot_id=kwargs["snapshot"].snapshot_id,
            status=TaskResultStatus.COMPLETED,
            summary="One relevant update was found.",
            evidence_refs=refs,
            warnings=("Search coverage is bounded.",),
        )
        self.agent_run_manager.mark_child_running(kwargs["child_run_id"])
        self.agent_run_manager.complete_child_run(
            kwargs["child_run_id"], result_snapshot={"answer": result.summary}
        )
        return result


def test_watch_adapter_derives_immutable_allowlisted_view_and_returns_evidence() -> None:
    runtime = _Runtime()
    parent = runtime.agent_run_manager.create_run(
        session_id="watch-parent-session", user_input="parent run"
    )

    result = asyncio.run(
        WatchExecutionAdapter(runtime=runtime).run_async(
            parent_run_id=parent.run_id,
            watch_id="watch_launch",
            goal="Find official launch updates",
            source_ids=("mail_source_1",),
            account_ids=("account_1",),
            web_enabled=True,
            matter_enabled=True,
        )
    )

    call = runtime.calls[0]
    view = call["views"].tool
    allowed = set(view.allowed_tools)
    assert allowed == {
        "mail.search",
        "mail.load_messages",
        "knowledge.search",
        "knowledge.load_chunks",
        "web.search",
        "web.open",
        "matter.search",
        "matter.list",
    }
    assert "mail.sync" not in allowed
    assert "matter.create" not in allowed
    assert "bash.run" not in allowed
    assert "filesystem.read_file" not in allowed
    assert view.allowed_source_ids == ("mail_source_1",)
    assert view.allowed_account_ids == ("account_1",)
    assert view.max_tool_calls == WATCH_MAX_TOOL_CALLS
    assert view.side_effect_level.value == "read"
    assert len(view.allowed_packages) == len(set(view.allowed_packages))
    with pytest.raises(ValueError):
        view.allowed_tools = ("bash.run",)
    assert call["snapshot"].budget.max_tokens == WATCH_MAX_TOKENS
    assert call["snapshot"].budget.max_llm_calls == WATCH_MAX_LLM_CALLS
    assert call["snapshot"].budget.max_wall_time_seconds == WATCH_MAX_WALL_TIME_SECONDS
    child = runtime.agent_run_manager.get_run(result.child_run_id)
    assert child is not None
    assert child.session_id != parent.session_id
    assert child.parent_run_id == parent.run_id
    assert result.status == TaskResultStatus.COMPLETED
    assert result.summary == "One relevant update was found."
    assert result.evidence_refs == ("ev_1",)
    assert result.evidence_sources == ("https://example.test/source",)
    assert result.warnings == ("Search coverage is bounded.",)


def test_watch_adapter_omits_mail_without_explicit_source_and_account_grants() -> None:
    runtime = _Runtime()
    parent = runtime.agent_run_manager.create_run(
        session_id="watch-parent-session", user_input="parent run"
    )

    asyncio.run(
        WatchExecutionAdapter(runtime=runtime).run_async(
            parent_run_id=parent.run_id,
            watch_id="watch_web",
            goal="Find official updates",
            web_enabled=True,
        )
    )

    view = runtime.calls[0]["views"].tool
    assert "mail.search" not in view.allowed_tools
    assert "mail.load_messages" not in view.allowed_tools
    assert "matter.search" not in view.allowed_tools
    assert "matter.list" not in view.allowed_tools
    assert view.allowed_source_ids == ()
    assert view.allowed_account_ids == ()
    assert set(view.allowed_tools) == {"web.search", "web.open"}


def test_watch_adapter_rejects_blank_goal_before_creating_child() -> None:
    runtime = _Runtime()
    parent = runtime.agent_run_manager.create_run(
        session_id="watch-parent-session", user_input="parent run"
    )

    with pytest.raises(ValueError, match="goal"):
        asyncio.run(
            WatchExecutionAdapter(runtime=runtime).run_async(
                parent_run_id=parent.run_id, watch_id="watch_1", goal="   "
            )
        )
    assert runtime.agent_run_manager.get_run(parent.run_id).child_run_ids == ()


def test_watch_adapter_uses_real_runtime_registry_and_scoped_child(tmp_path, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    from app.api.main import create_app

    runtime = create_app().state.runtime
    parent = runtime.agent_run_manager.create_run(
        session_id="watch-real-runtime",
        user_input="Check updates",
    )
    runtime.agent_run_manager.mark_running(parent.run_id)

    async def fake_child(**kwargs):
        view = kwargs["views"].tool
        assert set(view.allowed_tools) == {
            "web.search",
            "web.open",
            "instructions.read",
            "instructions.search",
        }
        assert view.side_effect_level.value == "read"
        child = runtime.agent_run_manager.get_run(kwargs["child_run_id"])
        assert child is not None and child.parent_run_id == parent.run_id
        runtime.session_service.ensure_session(session_id=child.session_id)
        runtime.session_service.append_message(
            session_id=child.session_id,
            role="agent",
            content="Found page",
            payload={
                "run_id": child.run_id,
                "tool_events": [
                    {
                        "tool_name": "web.open",
                        "result": {
                            "status": "completed",
                            "output": {
                                "url": "https://example.org/event",
                                "text": "Event details",
                            },
                        },
                    }
                ],
            },
        )
        return TaskResult(
            correlation_id=parent.trace_id,
            result_id="watch-result",
            child_run_id=child.run_id,
            plan_id=kwargs["snapshot"].plan_id,
            step_id=kwargs["snapshot"].step_id,
            snapshot_id=kwargs["snapshot"].snapshot_id,
            status=TaskResultStatus.COMPLETED,
            summary="No confirmed change.",
        )

    monkeypatch.setattr(runtime, "run_child_agent_async", fake_child)
    result = asyncio.run(
        WatchExecutionAdapter(runtime=runtime).run_async(
            parent_run_id=parent.run_id,
            watch_id="watch-test",
            goal="Check public news",
            web_enabled=True,
        )
    )
    assert result.summary == "No confirmed change."
    assert result.evidence_sources == ("https://example.org/event",)


def test_watch_adapter_grants_only_requested_workspace_read_and_sets_io_workload():
    from app.core.llm_workloads import _workload

    runtime = _Runtime()
    runtime.settings = SimpleNamespace(parsed_workspace_roots=lambda: [Path("/workspace")])
    parent = runtime.agent_run_manager.create_run(session_id="watch-workspace", user_input="parent")
    inside = []

    async def inspect_workload(**kwargs):
        workload = _workload.get()
        inside.append((workload.pool, workload.task_id, workload.max_tokens))
        return await _original(**kwargs)

    _original = runtime.run_child_agent_async
    runtime.run_child_agent_async = inspect_workload
    asyncio.run(
        WatchExecutionAdapter(runtime=runtime).run_async(
            parent_run_id=parent.run_id,
            watch_id="watch_workspace",
            goal="Review project notes",
            workspace_paths=("/workspace/research",),
        )
    )
    view = runtime.calls[0]["views"].tool
    assert view.allowed_tools == ("filesystem.read_file",)
    assert view.allowed_paths == ("/workspace/research",)
    assert inside == [
        (
            "background_io",
            f"watch:watch_workspace:{runtime.agent_run_manager.get_run(parent.run_id).child_run_ids[0]}",
            WATCH_MAX_TOKENS,
        )
    ]


def test_watch_adapter_rejects_workspace_scope_outside_configured_roots():
    runtime = _Runtime()
    runtime.settings = SimpleNamespace(parsed_workspace_roots=lambda: [Path("/workspace/research")])
    parent = runtime.agent_run_manager.create_run(session_id="watch-outside", user_input="parent")
    with pytest.raises(ValueError, match="configured workspace roots"):
        asyncio.run(
            WatchExecutionAdapter(runtime=runtime).run_async(
                parent_run_id=parent.run_id,
                watch_id="watch_outside",
                goal="Read a file",
                workspace_paths=("/etc",),
            )
        )
    assert runtime.calls == []
