from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.multi_agent import (
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


def test_verify_aggregate_uses_step_specific_contract_decisions() -> None:
    plan = _plan(_step("a"))
    aggregate = aggregate_task_results(plan, [_result("a", TaskResultStatus.COMPLETED)])
    result = verify_aggregate(plan, aggregate, output_contract_results={"a": True})
    assert result.status == VerificationStatus.PASSED
