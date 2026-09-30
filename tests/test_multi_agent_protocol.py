from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.multi_agent import (
    ChildRun,
    ChildRunStatus,
    DependencyEdge,
    FailureDetail,
    ForkCallerKind,
    ForkPolicy,
    ForkPolicyViolation,
    ForkSubtasksOperation,
    ForkSubtaskSpec,
    ForkValidationContext,
    MultiAgentEventPayload,
    MultiAgentEventType,
    MultiAgentRunEvent,
    Plan,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    ScopeGrant,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
    VerificationResult,
    VerificationStatus,
    objective_fingerprint,
    validate_fork_subtasks,
)


def _step(step_id: str, *depends_on: str) -> PlanStep:
    return PlanStep(
        correlation_id="trace_protocol",
        step_id=step_id,
        objective=f"Complete {step_id}",
        depends_on=depends_on,
        output_contract="structured_result",
    )


def _plan(*steps: PlanStep) -> Plan:
    return Plan(
        correlation_id="trace_protocol",
        plan_id="plan_protocol",
        parent_run_id="run_parent",
        session_id="session_protocol",
        objective="Validate a generic task graph",
        steps=steps,
    )


def test_plan_accepts_acyclic_dependencies_and_transitions_to_validated() -> None:
    plan = _plan(
        _step("research"),
        _step("report", "research"),
    )

    validated = plan.transition_to(PlanStatus.VALIDATED)

    assert validated.status == PlanStatus.VALIDATED
    assert validated.validated_at is not None
    assert plan.status == PlanStatus.DRAFT
    assert plan.dependency_edges == (DependencyEdge(source_step_id="research", target_step_id="report"),)


@pytest.mark.parametrize(
    ("steps", "match"),
    [
        ((_step("one", "missing"),), "does not exist"),
        ((_step("one", "two"), _step("two", "one")), "cyclic"),
        ((_step("one"), _step("one")), "duplicate step IDs"),
    ],
)
def test_plan_rejects_invalid_dag(steps: tuple[PlanStep, ...], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _plan(*steps)


def test_plan_derives_edges_from_the_canonical_step_dependencies() -> None:
    plan = _plan(_step("one"), _step("two", "one"))
    assert plan.dependency_edges == (DependencyEdge(source_step_id="one", target_step_id="two"),)


def test_plan_step_transitions_only_after_dependencies_complete() -> None:
    plan = _plan(_step("research"), _step("report", "research"))

    assert plan.ready_step_ids() == ("research",)
    with pytest.raises(ValueError, match="incomplete dependencies"):
        plan.transition_step("report", PlanStepStatus.READY)

    running = plan.transition_step("research", PlanStepStatus.READY).transition_step(
        "research", PlanStepStatus.RUNNING
    )
    completed = running.transition_step("research", PlanStepStatus.COMPLETED)

    assert completed.ready_step_ids() == ("report",)
    assert completed.transition_step("report", PlanStepStatus.READY).steps[1].status == PlanStepStatus.READY


def test_plan_step_rejects_invalid_status_transition() -> None:
    with pytest.raises(ValueError, match="Invalid plan step status transition"):
        _step("one").transition_to(PlanStepStatus.COMPLETED)


def test_plan_cannot_complete_with_unresolved_or_failed_steps() -> None:
    running = _plan(_step("done"), _step("pending")).model_copy(
        update={"status": PlanStatus.RUNNING}
    )
    with pytest.raises(ValueError, match="unresolved or failed"):
        running.transition_to(PlanStatus.COMPLETED)


def test_protocol_models_are_immutable_and_reject_unknown_fields() -> None:
    step = _step("one")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PlanStep(
            correlation_id="trace_protocol",
            step_id="two",
            objective="Complete two",
            output_contract="structured_result",
            unexpected="value",
        )
    with pytest.raises(ValidationError):
        step.status = PlanStepStatus.COMPLETED


def test_task_result_and_verification_reject_inconsistent_outcomes() -> None:
    base = {
        "correlation_id": "trace_protocol",
        "result_id": "result_1",
        "child_run_id": "child_1",
        "plan_id": "plan_protocol",
        "step_id": "one",
        "snapshot_id": "snapshot_1",
        "summary": "outcome",
    }
    with pytest.raises(ValidationError, match="require failure details"):
        TaskResult(status=TaskResultStatus.FAILED, **base)
    with pytest.raises(ValidationError, match="requires missing requirements"):
        TaskResult(status=TaskResultStatus.PARTIAL, **base)
    with pytest.raises(ValidationError, match="cannot contain missing"):
        VerificationResult(
            correlation_id="trace_protocol",
            verification_id="verification_1",
            status=VerificationStatus.PASSED,
            summary="passed",
            missing_requirements=("missing",),
        )
    assert TaskResult(
        status=TaskResultStatus.BLOCKED,
        failure=FailureDetail(category="policy", code="denied", message="Denied"),
        **base,
    ).failure is not None


def test_event_payload_is_immutable_and_event_type_is_constrained() -> None:
    event = MultiAgentRunEvent(
        correlation_id="trace_protocol",
        event_id="event_1",
        event_type=MultiAgentEventType.SUBTASK_CREATED,
        parent_run_id="run_parent",
        payload=MultiAgentEventPayload.from_dict({"status": "created"}),
    )
    copy = event.payload.as_dict()
    copy["status"] = "changed"
    assert event.payload.as_dict() == {"status": "created"}
    with pytest.raises(ValidationError):
        MultiAgentRunEvent(
            correlation_id="trace_protocol",
            event_id="event_2",
            event_type="unknown_event",
            parent_run_id="run_parent",
        )


def test_child_run_requires_snapshot_and_status_appropriate_lifecycle_details() -> None:
    base = {
        "correlation_id": "trace_protocol",
        "child_run_id": "child_1",
        "parent_run_id": "run_parent",
        "plan_id": "plan_protocol",
        "step_id": "one",
        "snapshot_id": "snapshot_1",
        "attempt": 1,
    }
    with pytest.raises(ValidationError, match="require waiting_since"):
        ChildRun(status=ChildRunStatus.WAITING_CONFIRMATION, **base)
    with pytest.raises(ValidationError, match="require error details"):
        ChildRun(status=ChildRunStatus.BLOCKED, **base)


def test_fork_subtasks_validates_policy_and_records_scope_adjustments() -> None:
    operation = ForkSubtasksOperation(
        correlation_id="trace_protocol",
        operation_id="fork_1",
        parent_step_id="root",
        subtasks=(
            ForkSubtaskSpec(
                step_id="research",
                objective="Gather evidence",
                output_contract="evidence_report",
                requested_scope=ScopeGrant(
                    allowed_packages=("knowledge", "forbidden"),
                    allowed_tools=("knowledge.search", "forbidden.tool"),
                    side_effect_level=SideEffectLevel.WRITE,
                ),
            ),
        ),
    )
    allowed_scope = ScopeGrant(
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    result = validate_fork_subtasks(
        operation,
        policy=ForkPolicy(
            max_depth=2,
            max_children=3,
            max_fork_size=2,
            allowed_scope=allowed_scope,
        ),
        context=_fork_context(scope=allowed_scope),
    )

    assert result.validated_steps[0].allowed_packages == ("knowledge",)
    assert result.validated_steps[0].side_effect_level == SideEffectLevel.READ
    assert len(result.scope_adjustments) == 1
    assert result.requested_operation is operation
    assert result.validated_steps[0].fork_parent_step_id == "root"
    assert result.validated_steps[0].created_by_run_id == "run_coordinator"


def test_server_derives_coordinator_then_leaf_roles_with_progressively_smaller_budgets() -> None:
    scope = ScopeGrant(side_effect_level=SideEffectLevel.READ)
    policy = ForkPolicy(
        max_depth=3,
        max_children=8,
        max_fork_size=4,
        allowed_scope=scope,
    )
    root_result = validate_fork_subtasks(
        _fork_operation(_fork_spec("coordinator")),
        policy=policy,
        context=_fork_context(scope=scope, caller_kind=ForkCallerKind.ROOT_PLANNER),
    )
    coordinator_step = root_result.validated_steps[0]
    assert coordinator_step.agent_kind == ForkCallerKind.COORDINATOR
    assert coordinator_step.budget == policy.coordinator_budget

    coordinator_result = validate_fork_subtasks(
        _fork_operation(_fork_spec("leaf_a"), _fork_spec("leaf_b")),
        policy=policy,
        context=_fork_context(
            scope=scope,
            caller_kind=ForkCallerKind.COORDINATOR,
            current_depth=1,
        ),
    )
    assert {step.agent_kind for step in coordinator_result.validated_steps} == {
        ForkCallerKind.LEAF
    }
    assert all(step.budget == policy.leaf_budget for step in coordinator_result.validated_steps)
    assert policy.leaf_budget.max_tokens < policy.coordinator_budget.max_tokens
    # The default root budget is unbounded; the coordinator receives a finite cap.
    assert policy.root_budget.max_tokens is None
    assert policy.coordinator_budget.max_tokens is not None
    with pytest.raises(ForkPolicyViolation, match="max_children"):
        validate_fork_subtasks(
            _fork_operation(
                _fork_spec("leaf_a"), _fork_spec("leaf_b")
            ),
            policy=policy,
            context=_fork_context(
                scope=scope,
                caller_kind=ForkCallerKind.COORDINATOR,
                existing_child_count=1,
            ),
        )


def test_leaf_cannot_fork_and_planner_cannot_assign_agent_kind() -> None:
    with pytest.raises(ForkPolicyViolation, match="Leaf Agents cannot fork"):
        validate_fork_subtasks(
            _fork_operation(_fork_spec("nested")),
            policy=_fork_policy(),
            context=_fork_context(caller_kind=ForkCallerKind.LEAF),
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ForkSubtaskSpec(
            step_id="self_promoted",
            objective="Self promote to coordinator",
            output_contract="result",
            agent_kind=ForkCallerKind.COORDINATOR,
        )


def test_fork_profile_request_is_server_allowlisted_and_frozen_on_plan_step() -> None:
    operation = _fork_operation(
        ForkSubtaskSpec(
            step_id="profiled",
            objective="Do work with the configured profile",
            output_contract="result",
            inference_profile_id="careful",
        )
    )
    allowed = _fork_policy().model_copy(
        update={"allowed_inference_profile_ids": ("careful",)}
    )
    result = validate_fork_subtasks(
        operation,
        policy=allowed,
        context=_fork_context(),
    )
    assert result.validated_steps[0].inference_profile_id == "careful"

    with pytest.raises(ForkPolicyViolation, match="unavailable inference profile"):
        validate_fork_subtasks(
            operation,
            policy=_fork_policy(),
            context=_fork_context(),
        )


def test_fork_subtasks_rejects_unknown_dependencies_and_policy_mutation_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ForkSubtasksOperation(
            correlation_id="trace_protocol",
            operation_id="fork_unsafe",
            parent_step_id="root",
            subtasks=(_fork_spec("research"),),
            total_budget={"max_tokens": 999999},
        )
    with pytest.raises(ForkPolicyViolation, match="dependency does not exist"):
        validate_fork_subtasks(
            _fork_operation(_fork_spec("research", "unknown")),
            policy=_fork_policy(),
            context=_fork_context(),
        )


@pytest.mark.parametrize(
    ("current_depth", "existing_child_count", "subtask_count", "match"),
    [
        (2, 0, 1, "max_depth"),
        (0, 3, 1, "max_children"),
        (0, 0, 3, "max_fork_size"),
    ],
)
def test_fork_subtasks_enforces_server_owned_limits(
    current_depth: int,
    existing_child_count: int,
    subtask_count: int,
    match: str,
) -> None:
    with pytest.raises(ForkPolicyViolation, match=match):
        validate_fork_subtasks(
            _fork_operation(*(_fork_spec(f"step_{index}") for index in range(subtask_count))),
            policy=_fork_policy(),
            context=_fork_context(
                current_depth=current_depth,
                existing_child_count=existing_child_count,
            ),
        )


def test_fork_subtasks_only_accepts_the_structured_operation_name() -> None:
    with pytest.raises(ValidationError):
        ForkSubtasksOperation(
            correlation_id="trace_protocol",
            operation="please fork tasks",
            operation_id="fork_natural_language",
            parent_step_id="root",
            subtasks=(_fork_spec("research"),),
        )


def test_fork_subtasks_planner_scope_cannot_self_authorize() -> None:
    requested = ForkSubtaskSpec(
        step_id="research",
        objective="Gather evidence",
        output_contract="evidence_report",
        requested_scope=ScopeGrant(allowed_tools=("knowledge.search",)),
    )
    result = validate_fork_subtasks(
        _fork_operation(requested),
        policy=ForkPolicy(
            max_depth=2,
            max_children=3,
            max_fork_size=2,
            allowed_scope=ScopeGrant(
                allowed_packages=("knowledge",),
                allowed_tools=("knowledge.search",),
                side_effect_level=SideEffectLevel.READ,
            ),
        ),
        context=_fork_context(scope=ScopeGrant(side_effect_level=SideEffectLevel.READ)),
    )
    assert result.validated_steps[0].allowed_packages == ()
    assert result.validated_steps[0].allowed_tools == ()


def test_fork_subtasks_inherits_authorized_catalog_when_planner_scope_is_empty() -> None:
    scope = ScopeGrant(
        allowed_packages=("knowledge", "filesystem"),
        allowed_tools=("knowledge.search", "filesystem.read_file", "filesystem.edit_file"),
        side_effect_level=SideEffectLevel.EXTERNAL,
    )
    result = validate_fork_subtasks(
        _fork_operation(_fork_spec("research")),
        policy=ForkPolicy(
            max_depth=2,
            max_children=3,
            max_fork_size=2,
            allowed_scope=scope,
        ),
        context=_fork_context(scope=scope),
    )
    assert result.validated_steps[0].allowed_packages == scope.allowed_packages
    assert result.validated_steps[0].allowed_tools == scope.allowed_tools
    assert result.validated_steps[0].side_effect_level == SideEffectLevel.EXTERNAL


def test_fork_scope_intersects_configured_root_with_nested_session_workspace(tmp_path) -> None:
    configured_root = tmp_path / "workspace"
    selected_workspace = configured_root / "project"
    scope = ScopeGrant(
        workspace_paths=(str(selected_workspace),),
        allowed_packages=("filesystem",),
        allowed_tools=("filesystem.read_file",),
        side_effect_level=SideEffectLevel.READ,
    )
    result = validate_fork_subtasks(
        _fork_operation(_fork_spec("research")),
        policy=ForkPolicy(
            max_depth=2,
            max_children=3,
            max_fork_size=2,
            allowed_scope=ScopeGrant(
                workspace_paths=(str(configured_root),),
                allowed_packages=("filesystem",),
                allowed_tools=("filesystem.read_file",),
                side_effect_level=SideEffectLevel.READ,
            ),
        ),
        context=_fork_context(scope=scope),
    )
    assert result.scope_adjustments[0].validated_scope.workspace_paths == (
        str(selected_workspace.resolve(strict=False)),
    )


def test_fork_subtasks_rejects_inactive_parent_step() -> None:
    with pytest.raises(ForkPolicyViolation, match="not active"):
        validate_fork_subtasks(
            _fork_operation(_fork_spec("research")),
            policy=_fork_policy(),
            context=_fork_context(parent_step_status=PlanStepStatus.COMPLETED),
        )


@pytest.mark.parametrize(
    "context_updates",
    [
        {"current_objective_fingerprint": objective_fingerprint("Gather evidence")},
        {"ancestor_objective_fingerprints": (objective_fingerprint("Gather evidence"),)},
    ],
)
def test_fork_subtasks_rejects_current_or_ancestor_objective_loops(context_updates) -> None:
    operation = _fork_operation(
        ForkSubtaskSpec(
            step_id="repeat",
            objective=" GATHER   evidence! ",
            output_contract="structured_result",
        )
    )
    with pytest.raises(ForkPolicyViolation, match="current or an ancestor"):
        validate_fork_subtasks(
            operation,
            policy=_fork_policy(),
            context=_fork_context(**context_updates),
        )


def test_fork_subtasks_rejects_normalized_duplicate_objectives_but_allows_distinct_work() -> None:
    duplicated = _fork_operation(
        ForkSubtaskSpec(
            step_id="first",
            objective="Gather evidence.",
            output_contract="evidence_report",
        ),
        ForkSubtaskSpec(
            step_id="second",
            objective=" GATHER   EVIDENCE! ",
            output_contract="another_contract",
        ),
    )
    with pytest.raises(ForkPolicyViolation, match="semantically duplicate"):
        validate_fork_subtasks(
            duplicated,
            policy=_fork_policy(),
            context=_fork_context(),
        )

    distinct = _fork_operation(
        ForkSubtaskSpec(
            step_id="summarize",
            objective="Summarize the evidence.",
            output_contract="summary",
        ),
        ForkSubtaskSpec(
            step_id="extract",
            objective="Extract dates and amounts from the evidence.",
            output_contract="facts",
        ),
    )
    result = validate_fork_subtasks(
        distinct,
        policy=_fork_policy(),
        context=_fork_context(),
    )
    assert len(result.validated_steps) == 2


def test_objective_fingerprint_preserves_meaningful_symbols() -> None:
    assert objective_fingerprint("Build C++ parser") != objective_fingerprint("Build C# parser")


def _fork_spec(step_id: str, *depends_on: str) -> ForkSubtaskSpec:
    return ForkSubtaskSpec(
        step_id=step_id,
        objective=f"Complete {step_id}",
        depends_on=depends_on,
        output_contract="structured_result",
    )


def _fork_operation(*subtasks: ForkSubtaskSpec) -> ForkSubtasksOperation:
    return ForkSubtasksOperation(
        correlation_id="trace_protocol",
        operation_id="fork_protocol",
        parent_step_id="root",
        subtasks=subtasks,
    )


def _fork_policy() -> ForkPolicy:
    return ForkPolicy(
        max_depth=2,
        max_children=3,
        max_fork_size=2,
        allowed_scope=ScopeGrant(side_effect_level=SideEffectLevel.READ),
    )


def _fork_context(
    *,
    scope: ScopeGrant | None = None,
    parent_step_status: PlanStepStatus = PlanStepStatus.RUNNING,
    current_depth: int = 0,
    existing_child_count: int = 0,
    current_objective_fingerprint: str | None = None,
    ancestor_objective_fingerprints: tuple[str, ...] = (),
    caller_kind: ForkCallerKind = ForkCallerKind.COORDINATOR,
    coordinator_depth: int = 0,
) -> ForkValidationContext:
    effective_scope = scope or ScopeGrant(side_effect_level=SideEffectLevel.READ)
    return ForkValidationContext(
        parent_effective_scope=effective_scope,
        session_scope=effective_scope,
        workspace_scope=effective_scope,
        parent_step_status=parent_step_status,
        caller_kind=caller_kind,
        created_by_run_id="run_coordinator",
        current_depth=current_depth,
        coordinator_depth=coordinator_depth,
        existing_child_count=existing_child_count,
        known_step_ids=("root",),
        current_objective_fingerprint=current_objective_fingerprint,
        ancestor_objective_fingerprints=ancestor_objective_fingerprints,
    )
