"""Authorization diagnostics explain refusals without granting a retry."""

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult, ToolSpec


def view(**updates):
    return ToolView(snapshot_id="snapshot", allowed_tools=("sample.action",),
                    allowed_packages=("sample",), side_effect_level=SideEffectLevel.READ,
                    **updates)


@pytest.mark.parametrize("updates,name,package,read_only,code", [
    ({"expires_at": datetime.now(UTC) - timedelta(days=1)}, "other", "other", False, "context_expired"),
    ({}, "other", "other", False, "tool_not_granted"),
    ({"allowed_packages": ("other",)}, "sample.action", "sample", False, "package_not_granted"),
    ({"allowed_tools": ()}, "sample.action", None, True, "package_not_granted"),
    ({"allowed_tools": (), "allowed_packages": ()}, "sample.action", "sample", True, "package_not_granted"),
    ({}, "sample.action", "sample", False, "invocation_not_read_only"),
    ({}, "sample.action", "sample", None, "invocation_not_read_only"),
    ({"side_effect_level": SideEffectLevel.NONE}, "sample.action", "sample", None, "invocation_not_read_only"),
    ({}, "sample.action", "sample", True, None),
    ({"allowed_packages": ()}, "sample.action", None, True, None),
    ({"allowed_tools": ()}, "sample.action", "sample", True, None),
    ({"side_effect_level": SideEffectLevel.WRITE}, "sample.action", "sample", False, None),
    ({"side_effect_level": SideEffectLevel.EXTERNAL}, "sample.action", "sample", None, None),
])
def test_view_denial_reason_preserves_order_and_identity_semantics(updates, name, package, read_only, code):
    frozen = view().model_copy(update=updates)
    before = frozen.model_dump(mode="json")
    assert frozen.denial_reason(tool_name=name, package=package, read_only=read_only) == code
    assert frozen.allows_tool(tool_name=name, package=package, read_only=read_only) is (code is None)
    assert frozen.model_dump(mode="json") == before


def test_view_diagnostics_match_original_authorization_truth_table():
    # Deliberately retain the original boolean contract as an independent oracle.
    for level, tools, packages, name, package, read_only, expired in product(
        list(SideEffectLevel), [(), ("sample.action",)], [(), ("sample",)],
        ["sample.action", "other"], [None, "sample", "other"], [True, False, None], [False, True],
    ):
        frozen = view().model_copy(update={
            "side_effect_level": level, "allowed_tools": tools, "allowed_packages": packages,
            "expires_at": datetime.now(UTC) + timedelta(days=-1 if expired else 1),
        })
        identity_allowed = (
            name in tools and (not packages or package in packages)
            if tools else bool(package and package in packages)
        )
        expected = not expired and identity_allowed and (
            level not in {SideEffectLevel.NONE, SideEffectLevel.READ} or read_only is True
        )
        assert frozen.allows_tool(tool_name=name, package=package, read_only=read_only) is expected
        assert (frozen.denial_reason(tool_name=name, package=package, read_only=read_only) is None) is expected


class ConditionalTool:
    spec = ToolSpec(name="sample.action", package="sample", type="local_tool",
                    description="Conditional reader", read_only=False, supports_read_only_invocations=True)

    def __init__(self):
        self.calls = 0

    def is_read_only_invocation(self, inputs):
        return inputs.get("mode") == "read"

    def invoke(self, *, invocation, context):
        self.calls += 1
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                          status="completed", output={"evidence": "actual read"})


def executor_for(tool):
    registry = ToolRegistry()
    registry.register_tool(tool)
    return ToolExecutor(registry)


def invoke(executor, frozen, inputs, **context_updates):
    return executor.execute(invocation_id="inv", tool_name="sample.action", tool_input=inputs,
                            context=ToolContext(session_id="sample", tool_view=frozen,
                                                safety_review_approved=True, **context_updates))


def assert_diagnostic(result, code, current_read_only, tool_granted, level="read"):
    assert result.status == "rejected" and result.execution_started is False
    assert result.output["authorization_denial"] == {
        "code": code, "current_read_only": current_read_only,
        "tool_granted": tool_granted, "allowed_side_effect_level": level,
    }
    assert "retry_within_existing_grant" not in result.output["authorization_denial"]


def test_read_rejection_explains_invocation_then_explicit_legal_read_succeeds():
    tool = ConditionalTool()
    executor = executor_for(tool)
    frozen = view()
    before = frozen.model_dump(mode="json")
    rejected = invoke(executor, frozen, {"mode": "write"})
    assert_diagnostic(rejected, "invocation_not_read_only", False, True)
    assert "not proven read-only" in rejected.error
    assert "subsequent invocations" in rejected.error
    assert tool.calls == 0  # No automatic rewrite or execution.
    read = invoke(executor, frozen, {"mode": "read"})
    assert read.status == "completed" and read.output["evidence"] == "actual read"
    assert tool.calls == 1
    assert frozen.model_dump(mode="json") == before


def test_none_read_only_is_not_proven_read_only_and_never_executed():
    tool = ConditionalTool()
    tool.spec = tool.spec.model_copy(update={"read_only": None})
    tool.is_read_only_invocation = None
    rejected = invoke(executor_for(tool), view(), {})
    assert_diagnostic(rejected, "invocation_not_read_only", None, True)
    assert "not proven read-only" in rejected.error
    assert tool.calls == 0


@pytest.mark.parametrize("updates,code", [
    ({"allowed_tools": ("other",)}, "tool_not_granted"),
    ({"allowed_packages": ("other",)}, "package_not_granted"),
])
def test_ungranted_tool_or_package_is_not_an_invocation_read_only_failure(updates, code):
    tool = ConditionalTool()
    frozen = view().model_copy(update=updates)
    before = frozen.model_dump(mode="json")
    rejected = invoke(executor_for(tool), frozen, {"mode": "read"})
    assert_diagnostic(rejected, code, True, False)
    assert tool.calls == 0 and frozen.model_dump(mode="json") == before


def test_expiry_precedes_identity_and_read_only_failure_without_retry_promise():
    tool = ConditionalTool()
    frozen = view(expires_at=datetime.now(UTC) - timedelta(days=1)).model_copy(
        update={"allowed_tools": ("other",), "allowed_packages": ("other",)}
    )
    before = frozen.model_dump(mode="json")
    rejected = invoke(executor_for(tool), frozen, {"mode": "write"})
    assert_diagnostic(rejected, "context_expired", False, False)
    assert tool.calls == 0 and frozen.model_dump(mode="json") == before


def test_legal_read_still_cannot_bypass_user_source_constraint():
    tool = ConditionalTool()
    tool.spec = tool.spec.model_copy(update={"unrestricted_execution": True})
    executor = executor_for(tool)
    executor.registry.register_effect_constraint("source_fence", blocked_domains=(), block_unrestricted=True)
    executor.constraint_store.add([("session", "sample")], {"source_fence"})
    frozen = view()
    before = frozen.model_dump(mode="json")
    rejected = invoke(executor, frozen, {"mode": "read"})
    assert rejected.status == "rejected" and rejected.execution_started is False
    assert rejected.output["human_review_required"] is True
    assert "authorization_denial" not in rejected.output
    assert "retry_within_existing_grant" not in rejected.output
    assert tool.calls == 0 and frozen.model_dump(mode="json") == before


def test_legal_read_still_cannot_bypass_cancelled_run():
    tool = ConditionalTool()
    executor = executor_for(tool)
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="sample", user_input="Read evidence")
    manager.mark_running(run.run_id)
    manager.request_cancel(run.run_id, reason="stop")
    executor.run_manager = manager
    frozen = view()
    before = frozen.model_dump(mode="json")
    rejected = invoke(executor, frozen, {"mode": "read"}, run_id=run.run_id)
    assert rejected.status == "rejected" and rejected.execution_started is False
    assert "cancelled or terminal" in rejected.error
    assert "authorization_denial" not in rejected.output
    assert "retry_within_existing_grant" not in rejected.output
    assert tool.calls == 0 and frozen.model_dump(mode="json") == before
