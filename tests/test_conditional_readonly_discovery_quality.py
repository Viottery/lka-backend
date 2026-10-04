"""Conditional reader discovery never grants a non-read-only invocation."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_turn import AgentTurnLoop
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolPackageSpec,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from app.tool_packages.bash import BASH_PACKAGE, BashAccessPolicy, BashRunTool, BashSessionManager


class ConditionalTool:
    def __init__(self, *, declared=True):
        self.spec = ToolSpec(
            name="sample.action", package="sample", type="local_tool",
            description="Argument-dependent reader", read_only=False,
            supports_read_only_invocations=declared,
            input_schema={"type": "object", "required": ["mode"],
                          "properties": {"mode": {"type": "string"}}},
        )
        self.invocations = 0

    def is_read_only_invocation(self, inputs):
        return inputs.get("mode") == "read"

    def invoke(self, *, invocation, context):
        self.invocations += 1
        return ToolResult(invocation_id=invocation.invocation_id,
                          tool_name=self.spec.name, status="completed")


class StaticWriteTool:
    spec = ToolSpec(name="sample.write", package="sample", type="local_tool",
                    description="Static writer", read_only=False)

    def invoke(self, *, invocation, context):
        raise AssertionError("A READ view must not execute a static writer")


def loop_for(*tools):
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="sample", description="Generic sample"))
    for tool in tools:
        registry.register_tool(tool)
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.tool_executor = ToolExecutor(registry)
    return loop


def sample_view(level=SideEffectLevel.READ):
    return ToolView(snapshot_id="snapshot_sample", allowed_packages=("sample",),
                    allowed_tools=("sample.action",), side_effect_level=level)


def test_declared_conditional_reader_is_discovered_without_changing_its_static_contract():
    tool = ConditionalTool()
    loop = loop_for(tool)
    view = sample_view()
    before = view.model_dump(mode="json")
    assert [p["name"] for p in loop._package_catalog(view)] == ["sample"]
    payload = loop._tool_payloads_for_package("sample", tool_view=view)[0]
    assert payload["read_only"] is False
    assert payload["supports_read_only_invocations"] is True
    assert view.model_dump(mode="json") == before
    assert tool.is_read_only_invocation({}) is False


def test_undeclared_conditional_and_static_writers_stay_hidden_at_read():
    conditional = ConditionalTool(declared=False)
    static = StaticWriteTool()
    loop = loop_for(conditional, static)
    view = sample_view().model_copy(update={"allowed_tools": ("sample.action", "sample.write")})
    assert loop._package_catalog(view) == []
    assert loop._tool_payloads_for_package("sample", tool_view=view) == []
    assert getattr(static.spec, "supports_read_only_invocations", False) is False


def test_declaration_alone_cannot_authorize_execution_without_a_read_classifier():
    tool = StaticWriteTool()
    tool.spec = tool.spec.model_copy(update={"supports_read_only_invocations": True})
    loop = loop_for(tool)
    view = sample_view().model_copy(update={"allowed_tools": ("sample.write",)})
    assert loop._package_exists("sample", tool_view=view)
    result = loop.tool_executor.execute(invocation_id="bad-declaration", tool_name=tool.spec.name,
        tool_input={}, context=ToolContext(session_id="sample", tool_view=view,
        safety_review_approved=True, safety_review_id="approved"))
    assert result.status == "rejected" and "ToolView" in result.error


@pytest.mark.parametrize("update", [
    {"side_effect_level": SideEffectLevel.NONE},
    {"allowed_tools": ("other.action",)},
    {"allowed_packages": ("other",)},
    {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
])
def test_declaration_does_not_bypass_none_identity_or_expiry(update):
    loop = loop_for(ConditionalTool())
    view = sample_view().model_copy(update=update)
    assert loop._package_catalog(view) == []


def test_generic_invocation_still_enforces_actual_arguments_even_after_review():
    tool = ConditionalTool()
    loop = loop_for(tool)
    view = sample_view()
    assert loop._package_exists("sample", tool_view=view)
    context = ToolContext(session_id="sample_session", tool_view=view,
                          safety_review_approved=True, safety_review_id="approved")
    read = loop.tool_executor.execute(invocation_id="read", tool_name=tool.spec.name,
                                     tool_input={"mode": "read"}, context=context)
    write = loop.tool_executor.execute(invocation_id="write", tool_name=tool.spec.name,
                                      tool_input={"mode": "write"}, context=context)
    assert read.status == "completed"
    assert write.status == "rejected" and "ToolView" in write.error
    assert tool.invocations == 1
    assert context.tool_view.side_effect_level == SideEffectLevel.READ


def test_external_and_unscoped_discovery_keep_legacy_safety_review():
    tool = ConditionalTool(declared=False)
    loop = loop_for(tool)
    view = sample_view(SideEffectLevel.EXTERNAL)
    assert loop._package_exists("sample", tool_view=view)
    assert loop._package_exists("sample")
    rejected = loop.tool_executor.execute(invocation_id="unapproved", tool_name=tool.spec.name,
        tool_input={"mode": "write"}, context=ToolContext(session_id="external", tool_view=view))
    assert rejected.status == "rejected" and rejected.output["safety_review_required"] is True
    approved = loop.tool_executor.execute(invocation_id="approved", tool_name=tool.spec.name,
        tool_input={"mode": "write"}, context=ToolContext(session_id="external", tool_view=view,
        safety_review_approved=True, safety_review_id="approved"))
    assert approved.status == "completed" and tool.invocations == 1


def test_read_child_discovers_and_executes_real_cat_but_rejects_write(tmp_path):
    source = tmp_path / "evidence.txt"
    source.write_text("direct evidence\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register_package(BASH_PACKAGE.model_copy(deep=True))
    sessions = BashSessionManager()
    tool = BashRunTool(policy=BashAccessPolicy.from_workspace_roots([tmp_path]), session_manager=sessions)
    registry.register_tool(tool)
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Read evidence")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(parent_run_id=parent.run_id, plan_id="plan_read",
                                    step_id="replacement", attempt=1, user_input="Read evidence")
    manager.mark_child_running(child.run_id)
    executor = ToolExecutor(registry)
    executor.run_manager = manager
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.tool_executor = executor
    view = ToolView(snapshot_id="snapshot_read", child_run_id=child.run_id,
                    allowed_packages=("bash",), allowed_tools=("bash.run",),
                    allowed_paths=(str(tmp_path),), full_workspace_authority=True,
                    side_effect_level=SideEffectLevel.READ, max_tool_calls=16)
    before = view.model_dump(mode="json")
    try:
        assert [p["name"] for p in loop._package_catalog(view)] == ["bash"]
        assert [t["name"] for t in loop._tool_payloads_for_package("bash", tool_view=view)] == ["bash.run"]
        context = ToolContext(session_id=child.session_id, workspace_root=str(tmp_path), tool_view=view)
        read = executor.execute(invocation_id="cat", tool_name="bash.run",
                                tool_input={"command": "cat evidence.txt", "mode": "sync"}, context=context)
        assert read.status == "completed", read.error
        assert read.output["read_only"] is True and read.output["stdout"] == "direct evidence\n"
        write = executor.execute(invocation_id="write", tool_name="bash.run",
            tool_input={"command": "printf changed > changed.txt", "mode": "sync"},
            context=context.model_copy(update={"safety_review_approved": True, "safety_review_id": "approved"}))
        assert write.status == "rejected" and "ToolView" in write.error
        assert not (tmp_path / "changed.txt").exists()
        assert source.read_text(encoding="utf-8") == "direct evidence\n"
        assert view.model_dump(mode="json") == before
        narrowed = context.model_copy(update={"tool_view": view.model_copy(update={"full_workspace_authority": False})})
        denied = executor.execute(invocation_id="narrow", tool_name="bash.run",
                                  tool_input={"command": "cat evidence.txt"}, context=narrowed)
        assert denied.status == "rejected" and "scope" in denied.error
    finally:
        sessions.close()
