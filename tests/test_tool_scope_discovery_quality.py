"""Discovery hides only tools whose immutable grants make every input invalid."""

import pytest

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


class Reader:
    def __init__(self, **metadata):
        self.spec = ToolSpec(
            name="records.read", package="records", type="local_tool",
            description="Read granted records", read_only=True, **metadata,
        )
        self.invocations = 0

    def invoke(self, *, invocation, context):
        self.invocations += 1
        return ToolResult(invocation_id=invocation.invocation_id,
                          tool_name=self.spec.name, status="completed")


def make_loop(tool):
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="records", description="Read records"))
    registry.register_tool(tool)
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.tool_executor = ToolExecutor(registry)
    return loop


def child_view(**grants):
    return ToolView(snapshot_id="scope_snapshot", child_run_id="scope_child",
                    allowed_packages=("records",), allowed_tools=("records.read",),
                    side_effect_level=SideEffectLevel.READ, **grants)


@pytest.mark.parametrize(("metadata", "grants"), [
    ({"scope_uses_sources": True, "scope_filtering_required": True}, {}),
    ({"scope_source_fields": ("sources",)}, {}),
    ({"scope_uses_accounts": True, "scope_filtering_required": True}, {}),
    ({"scope_account_fields": ("accounts",)}, {}),
    ({"scope_uses_workspace": True, "scope_filtering_required": True}, {}),
    ({"scope_path_fields": ("path",)}, {}),
    ({"scope_uses_sources": True}, {"allowed_source_ids": ("s1",)}),
    ({"scope_uses_accounts": True}, {"allowed_account_ids": ("a1",)}),
    ({"scope_uses_workspace": True}, {"allowed_paths": ("/permitted",)}),
])
def test_impossible_scope_is_hidden_in_catalog_expansion_and_existence(metadata, grants):
    tool = Reader(**metadata)
    loop = make_loop(tool)
    view = child_view(**grants)
    before = view.model_dump(mode="json")
    context = ToolContext(session_id="scope_session", tool_view=view)
    # Execution is already known to reject: discovery must agree without a call.
    assert loop.tool_executor._check_scope(spec=tool.spec, tool_input={},
        context=context, tool_view=view) is not None
    assert loop._package_catalog(view) == []
    assert loop._tool_payloads_for_package("records", tool_view=view) == []
    assert not loop._package_exists("records", tool_view=view)
    assert view.model_dump(mode="json") == before and tool.invocations == 0


@pytest.mark.parametrize(("metadata", "grants"), [
    ({}, {}),
    ({"scope_uses_sources": True, "scope_filtering_required": True},
     {"allowed_source_ids": ("s1",)}),
    ({"scope_source_fields": ("sources",)}, {"allowed_source_ids": ("s1",)}),
    ({"scope_uses_accounts": True, "scope_filtering_required": True},
     {"allowed_account_ids": ("a1",)}),
    ({"scope_account_fields": ("accounts",)}, {"allowed_account_ids": ("a1",)}),
    ({"scope_uses_workspace": True, "scope_filtering_required": True},
     {"allowed_paths": ("/permitted",)}),
    ({"scope_path_fields": ("path",)}, {"allowed_paths": ("/permitted",)}),
    ({"scope_uses_sources": True}, {"full_data_authority": True}),
    ({"scope_uses_accounts": True}, {"full_data_authority": True}),
    ({"scope_uses_workspace": True}, {"full_workspace_authority": True}),
])
def test_usable_or_argument_dependent_tools_remain_visible(metadata, grants):
    loop = make_loop(Reader(**metadata))
    view = child_view(**grants)
    assert loop._package_catalog(view)[0]["tool_names"] == ["records.read"]
    assert loop._tool_payloads_for_package("records", tool_view=view)[0]["name"] == "records.read"
    assert loop._package_exists("records", tool_view=view)


def test_discovery_does_not_grant_outside_scope_or_remove_argument_checks():
    tool = Reader(scope_source_fields=("sources",))
    loop = make_loop(tool)
    view = child_view(allowed_source_ids=("s1",))
    assert loop._package_exists("records", tool_view=view)
    context = ToolContext(session_id="scope_session", tool_view=view)
    for index, inputs in enumerate(({}, {"sources": ["outside"]})):
        result = loop.tool_executor.execute(invocation_id=f"deny-{index}",
            tool_name="records.read", tool_input=inputs, context=context)
        assert result.status == "rejected" and result.execution_started is False
    result = loop.tool_executor.execute(invocation_id="allowed", tool_name="records.read",
                                       tool_input={"sources": ["s1"]}, context=context)
    assert result.status == "completed" and tool.invocations == 1


def test_mixed_package_keeps_only_available_tool_names():
    unavailable = Reader(scope_uses_sources=True)
    loop = make_loop(unavailable)
    available = Reader()
    available.spec = available.spec.model_copy(update={"name": "records.status"})
    loop.tool_executor.registry.register_tool(available)
    view = child_view().model_copy(update={"allowed_tools": ("records.read", "records.status")})
    assert loop._package_catalog(view)[0]["tool_names"] == ["records.status"]
    assert [item["name"] for item in loop._tool_payloads_for_package("records", tool_view=view)] == [
        "records.status"]


def test_non_child_and_unscoped_discovery_are_unchanged():
    loop = make_loop(Reader(scope_uses_sources=True, scope_uses_accounts=True,
                            scope_uses_workspace=True))
    assert loop._package_exists("records")
    view = child_view().model_copy(update={"child_run_id": None})
    assert loop._package_exists("records", tool_view=view)


def test_conditional_reader_remains_visible_but_actual_write_is_denied(tmp_path):
    tool = Reader(scope_path_fields=("path",))
    tool.spec = tool.spec.model_copy(update={"read_only": False,
                                            "supports_read_only_invocations": True})
    tool.is_read_only_invocation = lambda inputs: inputs.get("mode") == "read"
    loop = make_loop(tool)
    view = child_view(allowed_paths=(str(tmp_path),))
    assert loop._package_exists("records", tool_view=view)
    context = ToolContext(session_id="scope_session", tool_view=view,
                          safety_review_approved=True, safety_review_id="reviewed")
    result = loop.tool_executor.execute(invocation_id="write", tool_name="records.read",
        tool_input={"path": str(tmp_path / "result.txt"), "mode": "write"}, context=context)
    assert result.status == "rejected" and tool.invocations == 0
