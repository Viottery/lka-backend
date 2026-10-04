"""Only executor-owned pre-invocation rejection proves no tool effect."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.agent_runs import AgentRunEvent, AgentRunRecord, AgentRunStatus
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_tool_graph import AgentToolLifecycleGraph
from app.core.child_tool_audit import build_child_tool_audit
from app.core.context_driver import ToolView
from app.core.multi_agent import (
    GENERAL_AGENT_ID,
    PlanStep,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
    VerificationStatus,
)
from app.core.multi_agent_aggregation import (
    ConfirmationState,
    VerificationPolicy,
    derive_child_execution_evidence,
    verify_task_result,
)
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult, ToolSpec


class EffectTool:
    spec = ToolSpec(name="sample.effect", package="sample", type="local_tool",
                    description="May change state", read_only=False)

    def __init__(self, path, outcome="failed"):
        self.path = path
        self.outcome = outcome

    def invoke(self, *, invocation, context):
        self.path.write_text("changed", encoding="utf-8")
        if self.outcome == "exception":
            raise RuntimeError("Failure after effect")
        # A tool must not be able to forge a pre-invocation denial marker.
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                          status=self.outcome, execution_started=False,
                          error="Tool is outside the immutable ContextSnapshot ToolView.")


def execute(tmp_path, level=SideEffectLevel.READ, outcome="failed"):
    tool = EffectTool(tmp_path / "changed.txt", outcome)
    registry = ToolRegistry()
    registry.register_tool(tool)
    view = ToolView(snapshot_id="snapshot", allowed_packages=("sample",),
                    allowed_tools=(tool.spec.name,), side_effect_level=level)
    result = ToolExecutor(registry).execute(invocation_id="inv", tool_name=tool.spec.name,
        tool_input={}, context=ToolContext(session_id="child", tool_view=view,
        safety_review_approved=True, safety_review_id="approved"))
    return result, registry, tool.path, view


def trace(result, registry):
    data = result.model_dump(mode="json")
    event = SimpleNamespace(tool_name=result.tool_name, input={}, result=data)
    audit = build_child_tool_audit([event], registry)
    payloads = [
        ("run_started", {}),
        ("safety_review_required", {"review": {"invocation_id": "inv", "read_only": False,
                                              "status": "pending"}}),
        ("safety_review_decided", {"review": {"invocation_id": "inv", "read_only": False,
                                             "status": "approved"}}),
        ("tool_started", {"tool_name": result.tool_name}),
        ("tool_completed", {"tool_name": result.tool_name, "metadata": {"result": data}}),
        ("run_completed", {"tool_event_count": 1}),
    ]
    events = [AgentRunEvent(event_id=f"event:{index}", run_id="child", sequence=index,
                           type=kind, message=kind, payload=payload, created_at="now")
              for index, (kind, payload) in enumerate(payloads, 1)]
    run = AgentRunRecord(run_id="child", session_id="child", trace_id="trace",
        status=AgentRunStatus.COMPLETED, user_input="work", created_at="now",
        metadata={"agent_id": GENERAL_AGENT_ID, "executor_kind": "react"})
    return run, events, audit


def test_read_scope_rejection_before_invoke_does_not_become_an_actual_effect(tmp_path):
    result, registry, path, view = execute(tmp_path)
    assert result.status == "rejected" and not path.exists()
    assert view.side_effect_level == SideEffectLevel.READ
    run, events, audit = trace(result, registry)
    actual, confirmation = derive_child_execution_evidence(run, events, tool_audit=audit)
    assert actual is False
    assert confirmation == ConfirmationState.NOT_REQUIRED
    assert result.execution_started is False
    assert audit["invocations"][0]["execution_started"] is False
    verification = verify_task_result(
        PlanStep(correlation_id="trace", step_id="step", objective="work",
                 output_contract="evidence", side_effect_level=SideEffectLevel.READ),
        TaskResult(correlation_id="trace", result_id="result", child_run_id="child",
                   plan_id="plan", step_id="step", snapshot_id="snapshot",
                   status=TaskResultStatus.COMPLETED, summary="No evidence obtained."),
        actual_side_effects=actual, confirmation=confirmation,
        policy=VerificationPolicy(correlation_id="trace", require_side_effect_audit=True),
    )
    assert "actual_side_effect_scope_mismatch" not in verification.missing_requirements
    assert verification.status == VerificationStatus.INCONCLUSIVE  # No contract upgrade.


@pytest.mark.parametrize("outcome", ["failed", "rejected", "exception"])
def test_effect_then_failure_or_rejection_remains_effectful_and_marker_cannot_be_forged(tmp_path, outcome):
    result, registry, path, _ = execute(tmp_path, SideEffectLevel.EXTERNAL, outcome)
    assert path.read_text(encoding="utf-8") == "changed"
    run, events, audit = trace(result, registry)
    assert derive_child_execution_evidence(run, events, tool_audit=audit) == (
        True, ConfirmationState.APPROVED,
    )
    assert result.execution_started is True
    assert audit["invocations"][0]["execution_started"] is True


@pytest.mark.parametrize("case", [
    "audit_only", "result_only", "disagree", "completed", "failed", "invalid_boolean",
])
def test_no_execution_proof_must_match_durable_result_and_rejected_status(tmp_path, case):
    result, registry, _, _ = execute(tmp_path)
    run, events, audit = trace(result, registry)
    audit = deepcopy(audit)
    data = events[4].payload["metadata"]["result"]
    # Seed the expected new shape so this counterexample also runs before the fix.
    data["execution_started"] = False
    audit["invocations"][0]["execution_started"] = False
    if case == "audit_only":
        data.pop("execution_started")
    elif case == "result_only":
        audit["invocations"][0].pop("execution_started")
    elif case == "disagree":
        data["execution_started"] = True
    elif case in {"completed", "failed"}:
        data["status"] = case
        audit["invocations"][0]["status"] = case
    else:
        data["execution_started"] = 0
        audit["invocations"][0]["execution_started"] = 0
    assert derive_child_execution_evidence(run, events, tool_audit=audit) == (
        None, ConfirmationState.MISSING,
    )


def test_legacy_or_output_only_marker_and_failed_run_do_not_prove_no_effect(tmp_path):
    result, registry, _, _ = execute(tmp_path)
    run, events, audit = trace(result, registry)
    data = events[4].payload["metadata"]["result"]
    data.pop("execution_started", None)
    audit["invocations"][0].pop("execution_started", None)
    data["output"] = {"execution_started": False}
    assert derive_child_execution_evidence(run, events, tool_audit=audit) == (
        True, ConfirmationState.APPROVED,
    )
    failed = run.model_copy(update={"status": AgentRunStatus.FAILED})
    assert derive_child_execution_evidence(failed, events, tool_audit=audit) == (
        None, ConfirmationState.MISSING,
    )


@pytest.mark.parametrize("level", [SideEffectLevel.READ, SideEffectLevel.EXTERNAL])
def test_execution_boundary_survives_real_lifecycle_and_sqlite_recovery(tmp_path, level):
    tool = EffectTool(tmp_path / "effect.txt", "failed")
    registry = ToolRegistry()
    registry.register_tool(tool)
    db_path = tmp_path / "audit.sqlite3"
    progress = []

    def lifecycle(store, executor):
        return AgentToolLifecycleGraph(
            tool_executor=executor,
            review_tool_call=lambda *_args: (None, None),
            append_progress=lambda kind, status, message, metadata: progress.append((kind, metadata)),
            raise_if_cancel_requested=lambda: None,
            artifact_store=store,
        )

    request = {
        "invocation_id": "inv", "run_id": "child", "tool_name": tool.spec.name,
        "tool_input": {},
        "context": ToolContext(
            session_id="child", safety_review_approved=True,
            tool_view=ToolView(snapshot_id="snapshot", allowed_tools=(tool.spec.name,),
                               allowed_packages=("sample",), side_effect_level=level),
        ),
    }
    first = lifecycle(SqliteAgentRunStore(db_path), ToolExecutor(registry)).run(**request)
    expected = level == SideEffectLevel.EXTERNAL
    assert first.execution_started is expected
    assert tool.path.exists() is expected
    assert [kind for kind, _ in progress] == ["tool_started", "tool_completed"]
    assert progress[-1][1]["result"]["execution_started"] is expected
    progress.clear()
    # A fresh store and executor recover the durable result without another invocation.
    recovered = lifecycle(SqliteAgentRunStore(db_path), ToolExecutor(ToolRegistry())).run(**request)
    assert recovered == first
    assert [kind for kind, _ in progress] == ["tool_recovered", "tool_completed"]
    assert progress[-1][1]["result"]["execution_started"] is expected
