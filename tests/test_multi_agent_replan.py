from __future__ import annotations

import pytest

from app.core.multi_agent import (
    ForkCallerKind,
    ForkPolicy,
    Plan,
    PlanPatch,
    PlanPatchContext,
    PlanPatchOperation,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
)
from app.core.multi_agent_replan import (
    PlanPatchRejected,
    apply_plan_patch,
    replay_plan_patch_history,
)


def _step(step_id: str, *deps: str, status: PlanStepStatus = PlanStepStatus.PENDING) -> PlanStep:
    return PlanStep(correlation_id="trace_patch", step_id=step_id, objective=step_id,
                    depends_on=deps, output_contract="report", allowed_packages=("research",),
                    allowed_tools=("research.search",), side_effect_level=SideEffectLevel.READ,
                    status=status)


def _plan(*steps: PlanStep) -> Plan:
    return Plan(correlation_id="trace_patch", plan_id="plan", parent_run_id="parent",
                session_id="session", objective="objective", steps=steps, status=PlanStatus.RUNNING)


def _context() -> PlanPatchContext:
    scope = ScopeGrant(workspace_paths=("/workspace",), source_ids=("s1",),
                       account_ids=("a1",), allowed_packages=("research", "files"),
                       allowed_tools=("research.search", "files.read"),
                       side_effect_level=SideEffectLevel.WRITE)
    return PlanPatchContext(
        fork_policy=ForkPolicy(max_depth=2, max_children=5, max_fork_size=3, allowed_scope=scope),
        parent_effective_scope=scope, session_scope=scope, workspace_scope=scope,
        remaining_budget=RuntimeBudget(max_tokens=100, max_llm_calls=5), max_retries_per_step=2,
    )


def _patch(operation: PlanPatchOperation, *, revision: int = 0, **kwargs) -> PlanPatch:
    return PlanPatch(patch_id=f"p{revision+1}", plan_id="plan",
                     expected_revision=revision, operation=operation, reason="replan", **kwargs)


def test_retry_patch_is_versioned_and_history_replays() -> None:
    plan = _plan(_step("a", status=PlanStepStatus.FAILED))
    result = apply_plan_patch(plan, _patch(PlanPatchOperation.RETRY_STEP, target_step_id="a"), _context())
    assert result.plan.steps[0].status == PlanStepStatus.PENDING
    assert result.plan.patch_revision == 1
    replay = replay_plan_patch_history(result.plan.patch_history, _context())
    assert replay[0].steps == result.plan.steps


def test_retry_patch_unblocks_only_eligible_downstream_chain() -> None:
    plan = _plan(
        _step("a", status=PlanStepStatus.FAILED),
        _step("b", "a", status=PlanStepStatus.BLOCKED),
        _step("c", "b", status=PlanStepStatus.BLOCKED),
        _step("unrelated", status=PlanStepStatus.BLOCKED),
    )
    result = apply_plan_patch(
        plan, _patch(PlanPatchOperation.RETRY_STEP, target_step_id="a"), _context()
    )
    assert [item.status for item in result.plan.steps] == [
        PlanStepStatus.PENDING, PlanStepStatus.PENDING,
        PlanStepStatus.PENDING, PlanStepStatus.BLOCKED,
    ]


def test_patch_rejects_stale_revision_policy_expansion_and_excess_budget() -> None:
    plan = _plan(_step("a", status=PlanStepStatus.FAILED))
    with pytest.raises(PlanPatchRejected, match="stale"):
        apply_plan_patch(plan, _patch(PlanPatchOperation.RETRY_STEP, revision=1, target_step_id="a"), _context())
    expansion = _patch(PlanPatchOperation.REDUCED_SCOPE, target_step_id="a",
                       reduced_scope=ScopeGrant(allowed_packages=("unlisted",), side_effect_level=SideEffectLevel.READ))
    with pytest.raises(PlanPatchRejected, match="subset"):
        apply_plan_patch(plan, expansion, _context())
    too_much = _patch(PlanPatchOperation.RETRY_STEP, target_step_id="a",
                      budget=RuntimeBudget(max_tokens=101))
    with pytest.raises(PlanPatchRejected, match="budget"):
        apply_plan_patch(plan, too_much, _context())


def test_reduced_scope_persists_nested_workspace_and_other_scope_dimensions() -> None:
    plan = _plan(_step("a", status=PlanStepStatus.FAILED))
    narrowed = ScopeGrant(workspace_paths=("/workspace/sub",), source_ids=("s1",),
                          account_ids=("a1",), allowed_packages=("research",),
                          allowed_tools=("research.search",), side_effect_level=SideEffectLevel.READ)
    result = apply_plan_patch(plan, _patch(PlanPatchOperation.REDUCED_SCOPE,
                                           target_step_id="a", reduced_scope=narrowed), _context())
    assert result.plan.steps[0].effective_scope == narrowed


def test_alternative_step_inherits_trusted_fork_provenance_and_rewires_dependents() -> None:
    failed = _step("a", status=PlanStepStatus.FAILED).model_copy(update={
        "fork_operation_id": "fork-op", "fork_parent_step_id": "root",
        "fork_depth": 1, "created_by_run_id": "parent",
    })
    plan = _plan(failed, _step("b", "a"))
    alternative = _step("replacement")
    result = apply_plan_patch(plan, _patch(PlanPatchOperation.ALTERNATIVE_STEP,
                                           target_step_id="a", alternative_step=alternative), _context())
    replacement = next(step for step in result.plan.steps if step.step_id == "replacement")
    dependent = next(step for step in result.plan.steps if step.step_id == "b")
    assert replacement.fork_operation_id == "fork-op"
    assert dependent.depends_on == ("replacement",)


def test_alternative_step_cannot_widen_replaced_step_scope() -> None:
    narrow = _step("a", status=PlanStepStatus.FAILED).model_copy(update={
        "effective_scope": ScopeGrant(
            allowed_packages=("research",), allowed_tools=("research.search",),
            side_effect_level=SideEffectLevel.READ,
        ),
    })
    plan = _plan(narrow)
    wider = _step("replacement").model_copy(update={
        "allowed_packages": ("files",), "allowed_tools": ("files.read",),
        "side_effect_level": SideEffectLevel.WRITE,
    })
    with pytest.raises(PlanPatchRejected, match="replaced step"):
        apply_plan_patch(
            plan,
            _patch(PlanPatchOperation.ALTERNATIVE_STEP, target_step_id="a", alternative_step=wider),
            _context(),
        )


def test_alternative_step_preserves_server_role_and_cannot_widen_role_budget() -> None:
    policy = ForkPolicy(
        max_depth=2,
        max_children=5,
        max_fork_size=3,
        allowed_scope=_context().fork_policy.allowed_scope,
    )
    context = _context().model_copy(
        update={
            "fork_policy": policy,
            "remaining_budget": RuntimeBudget(max_tokens=100_000, max_llm_calls=100),
        }
    )
    coordinator = _step("a", status=PlanStepStatus.FAILED).model_copy(
        update={
            "agent_kind": ForkCallerKind.COORDINATOR,
            "fork_operation_id": "fork-op",
            "fork_parent_step_id": "root",
            "fork_depth": 1,
            "created_by_run_id": "root-run",
            "budget": policy.coordinator_budget,
        }
    )
    plan = _plan(coordinator)

    oversized = _step("replacement").model_copy(
        update={"budget": RuntimeBudget(max_tokens=40_000)}
    )
    with pytest.raises(PlanPatchRejected, match="role"):
        apply_plan_patch(
            plan,
            _patch(
                PlanPatchOperation.ALTERNATIVE_STEP,
                target_step_id="a",
                alternative_step=oversized,
                budget=RuntimeBudget(max_tokens=40_000),
            ),
            context,
        )

    valid = apply_plan_patch(
        plan,
        _patch(
            PlanPatchOperation.ALTERNATIVE_STEP,
            target_step_id="a",
            alternative_step=_step("replacement"),
        ),
        context,
    )
    replacement = next(
        step for step in valid.plan.steps if step.step_id == "replacement"
    )
    assert replacement.agent_kind == ForkCallerKind.COORDINATOR
    assert replacement.budget == policy.coordinator_budget


def test_alternative_step_revalidates_dag_and_skip_degrades_dependents() -> None:
    plan = _plan(_step("a", status=PlanStepStatus.FAILED), _step("b", "a"))
    cyclic = _step("alternative", "b")
    with pytest.raises(ValueError, match="cyclic"):
        # Plan reconstruction is the authoritative full DAG check.
        apply_plan_patch(plan, _patch(PlanPatchOperation.ALTERNATIVE_STEP,
                                      target_step_id="a", alternative_step=cyclic), _context())
    patched = apply_plan_patch(plan, _patch(PlanPatchOperation.SKIP_AND_DEGRADE,
                                           target_step_id="a", degradation_note="continue without source"), _context())
    dependent = next(step for step in patched.plan.steps if step.step_id == "b")
    assert dependent.depends_on == ()
    assert dependent.degraded_dependency_notes == ("a: continue without source",)


def test_ask_user_and_abort_are_explicit_plan_outcomes() -> None:
    plan = _plan(_step("a"))
    wait = apply_plan_patch(plan, _patch(PlanPatchOperation.ASK_USER, user_question="Proceed?"), _context())
    assert wait.requires_user_input and wait.plan.status == PlanStatus.WAITING_USER
    aborted = apply_plan_patch(plan, _patch(PlanPatchOperation.ABORT), _context())
    assert aborted.aborted and aborted.plan.status == PlanStatus.CANCELLED
    assert aborted.plan.steps[0].status == PlanStepStatus.CANCELLED
