from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from app.core.agent_runs import AgentRunEvent, AgentRunRecord, AgentRunStatus
from app.core.multi_agent import (
    GENERAL_AGENT_ID,
    AggregationStatus,
    EvidenceRef,
    FailureDetail,
    Plan,
    PlanStep,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
    VerificationStatus,
)
from app.core.multi_agent_aggregation import (
    ConfirmationState,
    VerificationPolicy,
    aggregate_task_results,
    derive_child_execution_evidence,
    verify_aggregate,
    verify_task_result,
)


def _step(step_id: str, *, side_effect: SideEffectLevel = SideEffectLevel.NONE) -> PlanStep:
    return PlanStep(
        correlation_id="trace_aggregate", step_id=step_id, objective=step_id,
        output_contract="a structured report", side_effect_level=side_effect,
        fork_operation_id="fork", fork_parent_step_id="root", fork_depth=1,
        created_by_run_id="parent",
    )


def _plan(*steps: PlanStep) -> Plan:
    return Plan(correlation_id="trace_aggregate", plan_id="plan", parent_run_id="parent",
                session_id="session", objective="objective", steps=steps)


def _result(step_id: str, status: TaskResultStatus, **updates) -> TaskResult:
    values = {
        "correlation_id": "trace_aggregate",
        "result_id": f"result:{step_id}",
        "child_run_id": f"child:{step_id}",
        "plan_id": "plan",
        "step_id": step_id,
        "snapshot_id": f"snapshot:{step_id}",
        "status": status,
        "summary": "outcome",
    }
    values.update(updates)
    return TaskResult(**values)


def test_aggregation_is_complete_only_when_every_expected_step_completed() -> None:
    plan = _plan(_step("a"), _step("b"))
    agg = aggregate_task_results(plan, [_result("a", TaskResultStatus.COMPLETED)])
    assert agg.status == AggregationStatus.PARTIAL
    assert agg.missing_step_ids == ("b",)


def test_aggregation_never_masks_failed_or_blocked_child_as_success() -> None:
    plan = _plan(_step("a"), _step("b"))
    failed = _result("a", TaskResultStatus.FAILED, failure=FailureDetail(category="tool", code="failed", message="failed"))
    blocked = _result("b", TaskResultStatus.BLOCKED, failure=FailureDetail(category="safety", code="waiting", message="waiting"))
    agg = aggregate_task_results(plan, [failed, blocked])
    assert agg.status == AggregationStatus.FAILED
    assert agg.failed_step_ids == ("a",)
    assert agg.blocked_step_ids == ("b",)


def test_latest_retry_attempt_is_authoritative_without_erasing_prior_failure() -> None:
    plan = _plan(_step("a"))
    first = _result("a", TaskResultStatus.FAILED, failure=FailureDetail(category="tool", code="failed", message="failed"),
                    attempt=1,
                    completed_at=datetime(2026, 1, 1, tzinfo=UTC))
    retry = _result("a", TaskResultStatus.COMPLETED, result_id="result:a-retry", child_run_id="child:a-retry",
                    attempt=2,
                    completed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=1))
    agg = aggregate_task_results(plan, [first, retry])
    assert agg.status == AggregationStatus.COMPLETE
    assert agg.completed_step_ids == ("a",)
    assert "earlier attempt" in agg.warnings[0]
    assert len(agg.attempt_history) == 2
    assert tuple(item.result_id for item in agg.task_results) == ("result:a-retry",)


def test_attempt_number_wins_over_clock_and_same_attempt_conflicts() -> None:
    plan = _plan(_step("a"))
    first = _result("a", TaskResultStatus.FAILED, attempt=1, result_id="r1",
                    failure=FailureDetail(category="tool", code="failed", message="failed"),
                    completed_at=datetime(2026, 1, 2, tzinfo=UTC))
    retry = _result("a", TaskResultStatus.COMPLETED, attempt=2, result_id="r2",
                    completed_at=datetime(2026, 1, 1, tzinfo=UTC))
    chosen = aggregate_task_results(plan, [first, retry])
    assert chosen.status == AggregationStatus.COMPLETE
    assert chosen.task_results[0].attempt == 2

    conflict = retry.model_copy(update={"result_id": "r2-conflict", "summary": "different"})
    conflicted = aggregate_task_results(plan, [first, retry, conflict])
    assert conflicted.status == AggregationStatus.CONFLICTING


def test_duplicate_nonidentical_results_and_evidence_conflict_are_reported() -> None:
    plan = _plan(_step("a"))
    result = _result("a", TaskResultStatus.COMPLETED)
    conflicting = result.model_copy(update={"result_id": "other", "summary": "different"})
    e1 = EvidenceRef(evidence_id="e", source_ref="source/a")
    e2 = EvidenceRef(evidence_id="e", source_ref="source/b")
    agg = aggregate_task_results(plan, [result, conflicting], evidence_refs=[e1, e2])
    assert agg.status == AggregationStatus.CONFLICTING
    assert len(agg.conflicts) == 2


def test_verifier_requires_explicit_contract_evidence_and_write_confirmation() -> None:
    step = _step("write", side_effect=SideEffectLevel.WRITE)
    result = _result("write", TaskResultStatus.COMPLETED)
    inconclusive = verify_task_result(
        step, result,
        evidence_refs=[EvidenceRef(evidence_id="e", source_ref="source")],
        policy=VerificationPolicy(correlation_id="trace_aggregate", require_evidence=True),
        confirmation=ConfirmationState.PENDING, actual_side_effects=True,
    )
    assert inconclusive.status == VerificationStatus.INCONCLUSIVE
    assert "verify_output_contract" in inconclusive.recommended_actions
    assert "await_side_effect_confirmation" in inconclusive.recommended_actions
    passed = verify_task_result(
        step, result,
        evidence_refs=[EvidenceRef(evidence_id="e", source_ref="source")],
        output_contract_satisfied=True, confirmation=ConfirmationState.APPROVED,
        actual_side_effects=True,
        policy=VerificationPolicy(correlation_id="trace_aggregate", require_evidence=True),
    )
    assert passed.status == VerificationStatus.PASSED


def test_write_capability_alone_does_not_imply_confirmation() -> None:
    verification = verify_task_result(
        _step("write", side_effect=SideEffectLevel.EXTERNAL),
        _result("write", TaskResultStatus.COMPLETED), output_contract_satisfied=True,
    )
    assert verification.status == VerificationStatus.INCONCLUSIVE
    assert "actual_side_effects_unknown" in verification.missing_requirements


def _audit_run() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="child:a", session_id="session", trace_id="trace",
        status=AgentRunStatus.COMPLETED, user_input="work", created_at="now",
        metadata={"agent_id": GENERAL_AGENT_ID, "executor_kind": "react"},
    )


def _audit_event(sequence: int, event_type: str, payload: dict) -> AgentRunEvent:
    return AgentRunEvent(
        event_id=f"event:{sequence}", run_id="child:a", sequence=sequence,
        type=event_type, message=event_type, payload=payload, created_at="now",
    )


def test_child_execution_evidence_accepts_complete_read_only_trace() -> None:
    events = [
        _audit_event(1, "run_started", {}),
        _audit_event(2, "tool_started", {"tool_name": "knowledge.search"}),
        _audit_event(3, "tool_completed", {
            "tool_name": "knowledge.search",
            "metadata": {"result": {"invocation_id": "inv-1", "status": "completed"}},
        }),
        _audit_event(4, "run_completed", {"tool_event_count": 1}),
    ]

    actual, confirmation = derive_child_execution_evidence(
        _audit_run(), events,
        tool_audit={"protocol_version": "child_tool_audit_v1", "complete": True,
                    "invocations": [{"invocation_id": "inv-1", "tool_name": "knowledge.search",
                                    "read_only": True, "status": "completed"}]},
    )

    assert actual is False
    assert confirmation == ConfirmationState.NOT_REQUIRED


def test_child_execution_evidence_requires_complete_tool_and_safety_audit() -> None:
    incomplete = [
        _audit_event(1, "run_started", {}),
        _audit_event(2, "tool_started", {"tool_name": "knowledge.search"}),
        _audit_event(3, "run_completed", {"tool_event_count": 1}),
    ]
    actual, confirmation = derive_child_execution_evidence(
        _audit_run(), incomplete,
        tool_audit={"protocol_version": "child_tool_audit_v1", "complete": True,
                    "invocations": [{"invocation_id": "inv-1", "tool_name": "knowledge.search",
                                    "read_only": True, "status": "completed"}]},
    )
    assert actual is None
    assert confirmation == ConfirmationState.MISSING

    writable = [
        _audit_event(1, "run_started", {}),
        _audit_event(2, "safety_review_required", {"review": {
            "invocation_id": "inv-2", "read_only": False, "status": "pending",
        }}),
        _audit_event(3, "safety_review_decided", {"review": {
            "invocation_id": "inv-2", "read_only": False, "status": "approved",
        }}),
        _audit_event(4, "tool_started", {"tool_name": "matter.create"}),
        _audit_event(5, "tool_completed", {
            "tool_name": "matter.create",
            "metadata": {"result": {"invocation_id": "inv-2", "status": "rejected"}},
        }),
        _audit_event(6, "run_completed", {"tool_event_count": 1}),
    ]
    actual, confirmation = derive_child_execution_evidence(
        _audit_run(), writable,
        tool_audit={"protocol_version": "child_tool_audit_v1", "complete": True,
                    "invocations": [{"invocation_id": "inv-2", "tool_name": "matter.create",
                                    "read_only": False, "status": "rejected"}]},
    )
    assert actual is True
    assert confirmation == ConfirmationState.APPROVED


def test_child_execution_evidence_keeps_legacy_run_unknown() -> None:
    events = [
        _audit_event(1, "run_started", {}),
        _audit_event(2, "run_completed", {"tool_event_count": 0}),
    ]

    actual, confirmation = derive_child_execution_evidence(_audit_run(), events)

    assert actual is None
    assert confirmation == ConfirmationState.MISSING


@pytest.mark.parametrize("case", [
    "read", "staged", "incomplete", "source_applied", "external_unknown",
    "wrong_snapshot", "wrong_source", "wrong_workspace", "wrong_result", "wrong_executor",
    "missing_audit", "duplicate_audit", "foreign_event", "unisolated", "event_gap",
    "invalid_count", "invalid_result_count",
])
def test_isolated_workspace_audit_is_complete_and_scope_bound(tmp_path, case):
    source, isolated = str(tmp_path / "source"), str(tmp_path / "isolated")
    count = 1 if case == "staged" else 0
    audit = {
        "protocol_version": "isolated_workspace_audit_v1", "complete": True,
        "snapshot_id": "snapshot:a",
        "source_workspace_sha256": sha256(source.encode()).hexdigest(),
        "isolated_workspace_sha256": sha256(isolated.encode()).hexdigest(),
        "staged_change_count": count, "source_applied": False,
        "external_effects_ruled_out": True,
    }
    run = _audit_run().model_copy(update={
        "metadata": {"agent_id": "another_workspace_executor", "executor_kind": "external_cli",
                     "context_snapshot": {"snapshot_id": "snapshot:a", "effective_scope": {
                         "workspace_paths": [source], "side_effect_level": "write",
                         "allowed_packages": [], "allowed_tools": [],
                         "source_ids": [], "account_ids": [],
                     }}},
        "result_snapshot": {"staged_workspace": isolated, "staged_change_count": count},
    })
    if case == "incomplete":
        audit["complete"] = False
    elif case == "source_applied":
        audit["source_applied"] = True
    elif case == "external_unknown":
        audit.pop("external_effects_ruled_out")
    elif case == "wrong_snapshot":
        audit["snapshot_id"] = "other"
    elif case == "wrong_workspace":
        audit["isolated_workspace_sha256"] = "other"
    elif case == "wrong_source":
        audit["source_workspace_sha256"] = "other"
    elif case == "wrong_result":
        run.result_snapshot["staged_change_count"] = 2
    elif case == "invalid_count":
        audit["staged_change_count"] = False
    elif case == "invalid_result_count":
        run.result_snapshot["staged_change_count"] = 0.0
    elif case == "wrong_executor":
        run.metadata["executor_kind"] = "react"
    elif case == "unisolated":
        run.result_snapshot["staged_workspace"] = source
        audit["isolated_workspace_sha256"] = sha256(source.encode()).hexdigest()
    values = [("run_started", {})]
    if case != "missing_audit":
        values.append(("child_tool_audit", {"audit": audit}))
    if case == "duplicate_audit":
        values.append(("child_tool_audit", {"audit": audit}))
    values.append(("subtask_completed", {}))
    events = [_audit_event(index, kind, payload) for index, (kind, payload) in enumerate(values, 1)]
    if case == "foreign_event":
        events[1] = events[1].model_copy(update={"run_id": "another-child"})
    elif case == "event_gap":
        events[-1] = events[-1].model_copy(update={"sequence": 4})

    actual, confirmation = derive_child_execution_evidence(run, events, tool_audit=audit)

    if case == "read":
        assert actual is False and confirmation == ConfirmationState.NOT_REQUIRED
    elif case == "staged":
        assert actual is True and confirmation == ConfirmationState.MISSING
    else:
        assert actual is None and confirmation == ConfirmationState.MISSING


def test_failed_child_with_complete_zero_tool_trace_proves_no_side_effect() -> None:
    failed_run = _audit_run().model_copy(update={"status": AgentRunStatus.FAILED})
    events = [
        _audit_event(1, "run_started", {}),
        _audit_event(2, "run_failed", {}),
        _audit_event(3, "subtask_failed", {}),
    ]

    actual, confirmation = derive_child_execution_evidence(failed_run, events)

    assert actual is False
    assert confirmation == ConfirmationState.NOT_REQUIRED

    attempted = [
        _audit_event(1, "run_started", {}),
        _audit_event(2, "tool_started", {"tool_name": "matter.create"}),
        _audit_event(3, "run_failed", {}),
        _audit_event(4, "subtask_failed", {}),
    ]
    actual, confirmation = derive_child_execution_evidence(failed_run, attempted)
    assert actual is None
    assert confirmation == ConfirmationState.MISSING


def test_audited_verification_does_not_pass_unknown_read_only_scope_or_scope_mismatch() -> None:
    result = _result("a", TaskResultStatus.COMPLETED)
    policy = VerificationPolicy(correlation_id="trace_aggregate", require_side_effect_audit=True)
    unknown = verify_task_result(
        _step("a"), result, output_contract_satisfied=True,
        actual_side_effects=None, policy=policy,
    )
    assert unknown.status == VerificationStatus.INCONCLUSIVE
    assert "actual_side_effects_unknown" in unknown.missing_requirements

    mismatch = verify_task_result(
        _step("a"), result, output_contract_satisfied=True,
        actual_side_effects=True, confirmation=ConfirmationState.APPROVED, policy=policy,
    )
    assert mismatch.status == VerificationStatus.FAILED
    assert "actual_side_effect_scope_mismatch" in mismatch.missing_requirements


def test_output_contract_failure_and_step_mismatch_are_required_verification_checks() -> None:
    step = _step("expected")
    failed_contract = verify_task_result(
        step, _result("expected", TaskResultStatus.COMPLETED),
        output_contract_satisfied=False,
    )
    assert failed_contract.status == VerificationStatus.FAILED
    assert any(
        check.check_id == "output_contract"
        and check.status == VerificationStatus.FAILED
        and check.required
        for check in failed_contract.checks
    )

    mismatched_step = verify_task_result(
        step, _result("different", TaskResultStatus.COMPLETED),
        output_contract_satisfied=True,
    )
    assert mismatched_step.status == VerificationStatus.FAILED
    assert any(
        check.check_id == "output_contract"
        and check.status == VerificationStatus.FAILED
        and check.required
        for check in mismatched_step.checks
    )


def test_verify_aggregate_uses_step_specific_contract_decisions() -> None:
    plan = _plan(_step("a"))
    aggregate = aggregate_task_results(plan, [_result("a", TaskResultStatus.COMPLETED)])
    result = verify_aggregate(plan, aggregate, output_contract_results={"a": True})
    assert result.status == VerificationStatus.PASSED
