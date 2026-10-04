"""Configured fast-path scope derives from registered tools, not ToolSpec wrappers."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_turn import AgentTurnLoop
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolExecutor, ToolRegistry, ToolSpec


class StaticTool:
    def __init__(self, *, read_only=True):
        self.spec = ToolSpec(
            name="sample.inspect", package="sample", type="local_tool",
            description="Synthetic scope probe", read_only=read_only,
        )

    def invoke(self, **kwargs):
        raise AssertionError("Fast-path assessment must not execute tools.")


class DynamicTool(StaticTool):
    def __init__(self, classification):
        super().__init__()
        self.classification = classification
        self.inputs = []

    def is_read_only_invocation(self, inputs):
        self.inputs.append(inputs)
        if isinstance(self.classification, Exception):
            raise self.classification
        return self.classification


def setup_loop(tmp_path, tool, *, scoped=True, **view_updates):
    registry = ToolRegistry()
    registry.register_tool(tool)
    manager = InMemoryAgentRunManager()
    view = ToolView(
        snapshot_id="frozen-fast-path", allowed_tools=(tool.spec.name,),
        allowed_packages=(tool.spec.package,), allowed_paths=("/frozen/workspace",),
        allowed_source_ids=("source-grant",), allowed_account_ids=("account-grant",),
        side_effect_level=SideEffectLevel.READ,
    ).model_copy(update=view_updates)
    run = manager.create_run(
        session_id="fast-path-quality", user_input="Inspect the granted evidence.",
        metadata={"context_views": {"tool": view.model_dump(mode="json")}} if scoped else {},
    )
    loop = AgentTurnLoop(
        session_service=None, tool_executor=ToolExecutor(registry), llm_client=None,
        log_dir=tmp_path, run_manager=manager, fast_path_single_agent_enabled=True,
    )
    return loop, manager, run


def request(loop, run):
    return loop._configured_fast_path_request(
        run_id=run.run_id, session_id=run.session_id, user_input=run.user_input,
    )


def assess(loop, run):
    loop._assess_configured_fast_path(
        run_id=run.run_id, session_id=run.session_id, user_input=run.user_input,
    )


@pytest.mark.parametrize("dynamic", [False, True])
def test_readonly_registered_tool_builds_exact_frozen_scope_and_fast_path_hit(tmp_path, dynamic):
    tool = DynamicTool(True) if dynamic else StaticTool()
    loop, manager, run = setup_loop(tmp_path, tool)
    before = manager.get_run(run.run_id).metadata.copy()

    result = request(loop, run)

    assert result is not None
    assert result.authorized_scope.allowed_tools == (tool.spec.name,)
    assert result.authorized_scope.allowed_packages == (tool.spec.package,)
    assert result.authorized_scope.workspace_paths == ("/frozen/workspace",)
    assert result.authorized_scope.source_ids == ("source-grant",)
    assert result.authorized_scope.account_ids == ("account-grant",)
    assert result.authorized_scope.side_effect_level == SideEffectLevel.READ
    assess(loop, run)
    assert [event.type for event in manager.list_events(run.run_id)] == ["fast_path_hit"]
    assert manager.get_run(run.run_id).metadata == before
    if dynamic:
        assert tool.inputs and all(inputs == {} for inputs in tool.inputs)


@pytest.mark.parametrize("classification", [False, None, "true", 1, RuntimeError("unknown")])
def test_dynamic_false_or_unknown_does_not_inherit_static_readonly_or_expand_read_grant(
    tmp_path, classification,
):
    tool = DynamicTool(classification)
    loop, manager, run = setup_loop(tmp_path, tool)
    before = manager.get_run(run.run_id).metadata.copy()

    assert request(loop, run) is None
    assess(loop, run)

    events = manager.list_events(run.run_id)
    assert [event.type for event in events] == ["fast_path_upgraded"]
    assert events[0].payload["reason_code"] == "fast_path_agent_unavailable"
    assert manager.get_run(run.run_id).metadata == before


@pytest.mark.parametrize("updates", [
    {"allowed_tools": ("foreign.inspect",)},
    {"allowed_packages": ("foreign",)},
    {"expires_at": datetime.now(UTC) - timedelta(days=1)},
])
def test_readonly_does_not_override_tool_package_or_expiry_denial(tmp_path, updates):
    loop, manager, run = setup_loop(tmp_path, StaticTool(), **updates)
    before = manager.get_run(run.run_id).metadata.copy()

    assert request(loop, run) is None
    assert manager.get_run(run.run_id).metadata == before


def test_unregistered_listing_cannot_supply_fast_path_authority(tmp_path, monkeypatch):
    tool = StaticTool()
    loop, manager, run = setup_loop(tmp_path, tool)
    absent = tool.spec.model_copy(update={"name": "sample.absent"})
    view = loop._tool_view_for_run(run.run_id).model_copy(update={"allowed_tools": (absent.name,)})
    manager._update_run(
        run.run_id, status=run.status,
        metadata_patch={"context_views": {"tool": view.model_dump(mode="json")}},
    )
    monkeypatch.setattr(loop.tool_executor.registry, "list_tools", lambda: [absent])

    assert request(loop, run) is None
    assess(loop, run)
    assert [event.type for event in manager.list_events(run.run_id)] == ["fast_path_upgraded"]


def test_static_writable_tool_remains_hidden_at_read(tmp_path):
    loop, _, run = setup_loop(tmp_path, StaticTool(read_only=False))
    assert request(loop, run) is None


@pytest.mark.parametrize("tool", [StaticTool(), DynamicTool(False), DynamicTool(None)])
def test_no_tool_view_preserves_legacy_external_scope_without_dynamic_probe(tmp_path, tool):
    loop, manager, run = setup_loop(tmp_path, tool, scoped=False)

    result = request(loop, run)

    assert result is not None
    assert result.authorized_scope.allowed_tools == (tool.spec.name,)
    assert result.authorized_scope.side_effect_level == SideEffectLevel.EXTERNAL
    assert result.authorized_scope.workspace_paths == ()
    assert result.authorized_scope.source_ids == ()
    assert result.authorized_scope.account_ids == ()
    if isinstance(tool, DynamicTool):
        assert tool.inputs == []
    assess(loop, run)
    assert [event.type for event in manager.list_events(run.run_id)] == ["fast_path_hit"]
