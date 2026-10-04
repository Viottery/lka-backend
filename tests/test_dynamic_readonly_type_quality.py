"""Only real booleans from dynamic classifiers may authorize read-only work."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    effective_tool_read_only,
)


class TruthBomb:
    def __bool__(self):
        raise AssertionError("classifier result must not be truth-coerced")


class EffectTool:
    spec = ToolSpec(name="sample.effect", package="sample", type="local_tool",
        description="Synthetic effect counter", read_only=False,
        supports_read_only_invocations=True)

    def __init__(self, classification):
        self.classification, self.effects = classification, 0

    def is_read_only_invocation(self, inputs):
        if isinstance(self.classification, Exception):
            raise self.classification
        return self.classification

    def invoke(self, *, invocation, context):
        self.effects += 1
        return ToolResult(invocation_id=invocation.invocation_id,
            tool_name=self.spec.name, status="completed", output={"effects": self.effects})


def executor(tool):
    registry = ToolRegistry()
    registry.register_tool(tool)
    return ToolExecutor(registry)


def execute(executor, *, view=None, approved=False):
    return executor.execute(invocation_id="type-probe", tool_name="sample.effect",
        tool_input={}, context=ToolContext(session_id="isolated", tool_view=view,
            safety_review_approved=approved))


def read_view(**updates):
    return ToolView(snapshot_id="type-view", allowed_tools=("sample.effect",),
        allowed_packages=("sample",), side_effect_level=SideEffectLevel.READ).model_copy(update=updates)


@pytest.mark.parametrize("classification", [
    "false", "true", 1, 0, 1.0, 0.0, {}, {"read_only": True}, [], [True], None,
    TruthBomb(), ValueError("classifier error"), RuntimeError("classifier error"),
], ids=["string-false", "string-true", "one", "zero", "float-one", "float-zero",
    "empty-dict", "dict", "empty-list", "list", "none", "truth-bomb", "value-error", "runtime-error"])
def test_non_boolean_or_exception_is_unknown_read_rejected_and_parent_needs_approval(classification):
    tool = EffectTool(classification)
    ex = executor(tool)
    assert effective_tool_read_only(tool, {}) is None
    for level in (SideEffectLevel.NONE, SideEffectLevel.READ):
        frozen = read_view(side_effect_level=level)
        before = frozen.model_dump(mode="json")
        rejected = execute(ex, view=frozen, approved=True)
        assert rejected.status == "rejected" and rejected.execution_started is False
        assert rejected.output["authorization_denial"]["current_read_only"] is None
        assert frozen.model_dump(mode="json") == before
        assert tool.effects == 0
    parent = execute(ex)
    assert parent.status == "rejected" and parent.output["safety_review_required"] is True
    assert parent.output["read_only"] is None and tool.effects == 0
    approved = execute(ex, approved=True)
    assert approved.status == "completed" and tool.effects == 1


@pytest.mark.parametrize("classification", [True, False])
def test_actual_bool_is_preserved_without_changing_authority(classification):
    tool = EffectTool(classification)
    ex = executor(tool)
    assert effective_tool_read_only(tool, {}) is classification
    result = execute(ex, view=read_view())
    assert result.status == ("completed" if classification else "rejected")
    assert tool.effects == int(classification)


@pytest.mark.parametrize("updates", [
    {"allowed_tools": ("other",)}, {"allowed_packages": ("other",)},
    {"expires_at": datetime.now(UTC) - timedelta(days=1)},
    {"allowed_source_ids": ("allowed-source",), "child_run_id": "child"},
])
def test_true_classifier_does_not_override_identity_expiry_or_source_grants(updates):
    tool = EffectTool(True)
    # A source-scoped tool must still declare/enforce argument filtering.
    tool.spec = tool.spec.model_copy(update={"scope_uses_sources": True})
    result = execute(executor(tool), view=read_view(**updates), approved=True)
    assert result.status == "rejected" and result.execution_started is False
    assert tool.effects == 0


def test_missing_dynamic_classifier_preserves_registered_readonly_tool():
    tool = EffectTool(None)
    tool.is_read_only_invocation = None
    tool.spec = tool.spec.model_copy(update={"read_only": True})
    assert effective_tool_read_only(tool, {}) is True
    result = execute(executor(tool), view=read_view())
    assert result.status == "completed" and tool.effects == 1


def test_invalid_dynamic_classifier_does_not_fall_back_to_static_true():
    tool = EffectTool("true")
    tool.spec = tool.spec.model_copy(update={"read_only": True})
    assert effective_tool_read_only(tool, {}) is None
    assert execute(executor(tool), view=read_view()).status == "rejected"
    assert tool.effects == 0


def test_classifier_lookup_exception_is_unknown_not_static_fallback():
    class BrokenDescriptor(EffectTool):
        @property
        def is_read_only_invocation(self):
            raise RuntimeError("classifier lookup error")

    tool = BrokenDescriptor(None)
    tool.spec = tool.spec.model_copy(update={"read_only": True})
    assert effective_tool_read_only(tool, {}) is None
    assert execute(executor(tool), view=read_view()).status == "rejected"
    assert tool.effects == 0


def test_cancellation_is_not_converted_to_unknown_approved_execution():
    tool = EffectTool(True)

    def cancelled(inputs):
        raise asyncio.CancelledError

    tool.is_read_only_invocation = cancelled
    with pytest.raises(asyncio.CancelledError):
        execute(executor(tool), approved=True)
    assert tool.effects == 0
