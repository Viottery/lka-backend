"""Pure validation, application, and replay for append-only PlanPatch history."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.multi_agent import (
    ForkCallerKind,
    Plan,
    PlanPatch,
    PlanPatchContext,
    PlanPatchOperation,
    PlanPatchRecord,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    _intersect_scope,
    _intersect_workspace_paths,
)


class PlanPatchRejected(ValueError):
    """A proposed replan violates current plan state, policy, or budget."""


@dataclass(frozen=True)
class PlanPatchApplication:
    plan: Plan
    record: PlanPatchRecord
    requires_user_input: bool = False
    user_question: str | None = None
    aborted: bool = False


def _dump(plan: Plan) -> str:
    return plan.model_dump_json(exclude={"patch_revision", "patch_history"}, exclude_none=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _step(plan: Plan, step_id: str) -> PlanStep:
    for step in plan.steps:
        if step.step_id == step_id:
            return step
    raise PlanPatchRejected(f"Unknown target step: {step_id}")


def _budget_within(candidate: RuntimeBudget, *ceilings: RuntimeBudget | None) -> bool:
    for field in ("max_tokens", "max_llm_calls", "max_tool_calls", "max_wall_time_seconds"):
        value = getattr(candidate, field)
        limits = [getattr(ceiling, field) for ceiling in ceilings if ceiling is not None]
        limits = [limit for limit in limits if limit is not None]
        if limits and (value is None or value > min(limits)):
            return False
    return True


def _minimum_budget(*budgets: RuntimeBudget | None) -> RuntimeBudget:
    """Clamp a default retry/replacement budget to every finite ceiling."""
    def minimum(field: str) -> int | None:
        values = [
            getattr(budget, field)
            for budget in budgets
            if budget is not None and getattr(budget, field) is not None
        ]
        return min(values) if values else None

    return RuntimeBudget(
        max_tokens=minimum("max_tokens"),
        max_llm_calls=minimum("max_llm_calls"),
        max_tool_calls=minimum("max_tool_calls"),
        max_wall_time_seconds=minimum("max_wall_time_seconds"),
    )


def _scope_within(candidate: ScopeGrant, ceiling: ScopeGrant) -> bool:
    # Empty candidate sets never act as an implicit grant. Side-effect is ordinal.
    return (
        _intersect_workspace_paths(candidate.workspace_paths, ceiling.workspace_paths) == candidate.workspace_paths
        and set(candidate.source_ids).issubset(ceiling.source_ids)
        and set(candidate.account_ids).issubset(ceiling.account_ids)
        and set(candidate.allowed_packages).issubset(ceiling.allowed_packages)
        and set(candidate.allowed_tools).issubset(ceiling.allowed_tools)
        and _effect_rank(candidate.side_effect_level) <= _effect_rank(ceiling.side_effect_level)
    )


def _effect_rank(level: SideEffectLevel) -> int:
    return {SideEffectLevel.NONE: 0, SideEffectLevel.READ: 1, SideEffectLevel.WRITE: 2, SideEffectLevel.EXTERNAL: 3}[level]


def _effective_ceiling(context: PlanPatchContext) -> ScopeGrant:
    return _intersect_scope(
        context.fork_policy.allowed_scope,
        context.parent_effective_scope,
        context.session_scope,
        context.workspace_scope,
    )


def _validated_update(plan: Plan, **updates) -> Plan:
    """Rebuild through Pydantic so DAG/history validators cannot be bypassed."""

    values = plan.model_dump(mode="python")
    values.update(updates)
    return Plan.model_validate(values)


def _mutate(plan: Plan, patch: PlanPatch, context: PlanPatchContext) -> Plan:
    if patch.plan_id != plan.plan_id:
        raise PlanPatchRejected("Patch belongs to another plan.")
    if patch.expected_revision != plan.patch_revision:
        raise PlanPatchRejected("Patch revision is stale.")
    if plan.status not in {PlanStatus.RUNNING, PlanStatus.REPLANNING, PlanStatus.WAITING_USER}:
        raise PlanPatchRejected("Plan is not in a re-plannable state.")
    if any(record.patch.patch_id == patch.patch_id for record in plan.patch_history):
        raise PlanPatchRejected("Patch ID has already been applied.")
    steps = list(plan.steps)
    index = {item.step_id: i for i, item in enumerate(steps)}

    if patch.operation == PlanPatchOperation.ASK_USER:
        return _validated_update(plan, status=PlanStatus.WAITING_USER)
    if patch.operation == PlanPatchOperation.ABORT:
        for i, item in enumerate(steps):
            if item.status in {PlanStepStatus.PENDING, PlanStepStatus.BLOCKED, PlanStepStatus.READY, PlanStepStatus.RUNNING, PlanStepStatus.WAITING}:
                steps[i] = item.model_copy(update={"status": PlanStepStatus.CANCELLED})
        return _validated_update(plan, steps=tuple(steps), status=PlanStatus.CANCELLED)

    assert patch.target_step_id is not None
    target = _step(plan, patch.target_step_id)
    if patch.operation == PlanPatchOperation.RETRY_STEP:
        if target.status not in {PlanStepStatus.FAILED, PlanStepStatus.BLOCKED}:
            raise PlanPatchRejected("Only failed or blocked steps may be retried.")
        retries = sum(1 for record in plan.patch_history if record.patch.operation == PlanPatchOperation.RETRY_STEP and record.patch.target_step_id == target.step_id)
        if retries >= context.max_retries_per_step:
            raise PlanPatchRejected("Per-step retry limit exceeded.")
        role_ceiling = context.fork_policy.budget_for(
            target.agent_kind or ForkCallerKind.LEAF
        )
        budget = patch.budget or _minimum_budget(
            target.budget, role_ceiling, context.remaining_budget
        )
        if not _budget_within(
            budget, target.budget, role_ceiling, context.remaining_budget
        ):
            raise PlanPatchRejected("Retry budget exceeds the step or remaining plan budget.")
        steps[index[target.step_id]] = target.model_copy(update={"status": PlanStepStatus.PENDING, "budget": budget})
        # A failed dependency may have blocked an entire downstream chain. Once
        # the dependency is explicitly retried, only those blocked descendants
        # become schedulable again; unrelated blocked work remains blocked.
        eligible = {target.step_id}
        changed = True
        while changed:
            changed = False
            for i, dependent in enumerate(steps):
                if dependent.status == PlanStepStatus.BLOCKED and any(
                    dependency in eligible for dependency in dependent.depends_on
                ):
                    steps[i] = dependent.model_copy(update={"status": PlanStepStatus.PENDING})
                    eligible.add(dependent.step_id)
                    changed = True
        return _validated_update(plan, steps=tuple(steps), status=PlanStatus.REPLANNING)
    if patch.operation == PlanPatchOperation.REDUCED_SCOPE:
        if target.status not in {PlanStepStatus.PENDING, PlanStepStatus.BLOCKED, PlanStepStatus.FAILED}:
            raise PlanPatchRejected("Scope can only be reduced on a non-running step.")
        assert patch.reduced_scope is not None
        ceiling = _effective_ceiling(context)
        current_scope = target.effective_scope or ScopeGrant(
            workspace_paths=ceiling.workspace_paths,
            source_ids=ceiling.source_ids,
            account_ids=ceiling.account_ids,
            allowed_packages=target.allowed_packages,
            allowed_tools=target.allowed_tools,
            side_effect_level=target.side_effect_level,
        )
        if not _scope_within(patch.reduced_scope, current_scope) or not _scope_within(patch.reduced_scope, ceiling):
            raise PlanPatchRejected("Reduced scope is not a subset of current and server-authorized scope.")
        steps[index[target.step_id]] = target.model_copy(update={
            "status": PlanStepStatus.PENDING,
            "allowed_packages": patch.reduced_scope.allowed_packages,
            "allowed_tools": patch.reduced_scope.allowed_tools,
            "side_effect_level": patch.reduced_scope.side_effect_level,
            "effective_scope": patch.reduced_scope,
        })
        return _validated_update(plan, steps=tuple(steps), status=PlanStatus.REPLANNING)
    if patch.operation == PlanPatchOperation.ALTERNATIVE_STEP:
        if target.status not in {PlanStepStatus.FAILED, PlanStepStatus.BLOCKED}:
            raise PlanPatchRejected("Alternative steps can replace only failed or blocked steps.")
        alternative = patch.alternative_step
        assert alternative is not None
        if (
            alternative.output_contract != target.output_contract
            or alternative.verification_criteria != target.verification_criteria
        ) and not (patch.degradation_note and patch.degradation_note.strip()):
            raise PlanPatchRejected(
                "Alternative changes the original contract or verification criteria; "
                "explicit degradation_note is required for uncovered requirements."
            )
        if alternative.agent_id not in context.fork_policy.allowed_agent_ids:
            raise PlanPatchRejected(f"Alternative requests an unavailable Agent: {alternative.agent_id}")
        if (
            alternative.inference_profile_id is not None
            and alternative.inference_profile_id
            not in context.fork_policy.allowed_inference_profile_ids
        ):
            raise PlanPatchRejected(
                "Alternative requests an unavailable inference profile."
            )
        if alternative.step_id in index:
            raise PlanPatchRejected("Alternative step ID already exists.")
        forked_children = sum(1 for item in steps if item.fork_operation_id is not None)
        if forked_children + 1 > context.fork_policy.max_children:
            raise PlanPatchRejected("Alternative step exceeds max_children.")
        ceiling = _effective_ceiling(context)
        current_scope = target.effective_scope or ScopeGrant(
            workspace_paths=ceiling.workspace_paths,
            source_ids=ceiling.source_ids,
            account_ids=ceiling.account_ids,
            allowed_packages=target.allowed_packages,
            allowed_tools=target.allowed_tools,
            side_effect_level=target.side_effect_level,
        )
        alternative_scope = ScopeGrant(
            workspace_paths=current_scope.workspace_paths,
            source_ids=current_scope.source_ids,
            account_ids=current_scope.account_ids,
            allowed_packages=alternative.allowed_packages,
            allowed_tools=alternative.allowed_tools,
            side_effect_level=alternative.side_effect_level,
        )
        if not _scope_within(alternative_scope, current_scope) or not _scope_within(alternative_scope, ceiling):
            raise PlanPatchRejected("Alternative step scope exceeds the replaced step or server policy.")
        alternative_depth = alternative.fork_depth or target.fork_depth
        if alternative_depth is not None and alternative_depth > context.fork_policy.max_depth:
            raise PlanPatchRejected("Alternative step exceeds max_depth.")
        role_ceiling = context.fork_policy.budget_for(
            target.agent_kind or ForkCallerKind.LEAF
        )
        alternative_budget = patch.budget or _minimum_budget(
            alternative.budget,
            target.budget,
            role_ceiling,
            context.remaining_budget,
        )
        if not _budget_within(
            alternative_budget,
            target.budget,
            role_ceiling,
            context.remaining_budget,
        ):
            raise PlanPatchRejected(
                "Alternative step budget exceeds the step role or remaining plan budget."
            )
        for dependency in alternative.depends_on:
            if dependency not in index:
                raise PlanPatchRejected(f"Alternative dependency does not exist: {dependency}")
        alternative = alternative.model_copy(update={
            "effective_scope": alternative_scope,
            "budget": alternative_budget,
            "fork_operation_id": target.fork_operation_id,
            "fork_parent_step_id": target.fork_parent_step_id,
            "fork_depth": target.fork_depth,
            "created_by_run_id": target.created_by_run_id,
            "agent_kind": target.agent_kind,
        })
        steps[index[target.step_id]] = target.model_copy(update={"status": PlanStepStatus.SKIPPED})
        for i, dependent in enumerate(steps):
            if target.step_id in dependent.depends_on:
                steps[i] = dependent.model_copy(update={
                    "depends_on": tuple(
                        alternative.step_id if dep == target.step_id else dep
                        for dep in dependent.depends_on
                    ),
                    "degraded_dependency_notes": (
                        (*dependent.degraded_dependency_notes,
                         f"{target.step_id}: {patch.degradation_note}")
                        if patch.degradation_note else dependent.degraded_dependency_notes
                    ),
                })
        steps.append(alternative)
        return _validated_update(plan, steps=tuple(steps), status=PlanStatus.REPLANNING)
    if patch.operation == PlanPatchOperation.SKIP_AND_DEGRADE:
        if target.status not in {PlanStepStatus.FAILED, PlanStepStatus.BLOCKED}:
            raise PlanPatchRejected("Only failed or blocked steps can be skipped with degradation.")
        assert patch.degradation_note is not None
        steps[index[target.step_id]] = target.model_copy(update={"status": PlanStepStatus.SKIPPED})
        for i, dependent in enumerate(steps):
            if target.step_id in dependent.depends_on:
                steps[i] = dependent.model_copy(update={
                    "depends_on": tuple(dep for dep in dependent.depends_on if dep != target.step_id),
                    "degraded_dependency_notes": (*dependent.degraded_dependency_notes, f"{target.step_id}: {patch.degradation_note}"),
                })
        # Revalidation of the immutable Plan re-checks the entire DAG.
        return _validated_update(plan, steps=tuple(steps), status=PlanStatus.REPLANNING)
    raise PlanPatchRejected(f"Unsupported patch operation: {patch.operation}")


def apply_plan_patch(plan: Plan, patch: PlanPatch, context: PlanPatchContext) -> PlanPatchApplication:
    """Validate and atomically apply one patch, appending a replayable record."""

    before = _dump(plan)
    mutated = _mutate(plan, patch, context)
    after = _dump(mutated)
    record = PlanPatchRecord(
        revision=plan.patch_revision + 1,
        patch=patch,
        before_json=before,
        after_json=after,
        before_hash=_hash(before),
        after_hash=_hash(after),
    )
    result = _validated_update(
        mutated,
        patch_revision=plan.patch_revision + 1,
        patch_history=(*plan.patch_history, record),
    )
    return PlanPatchApplication(
        plan=result,
        record=record,
        requires_user_input=patch.operation == PlanPatchOperation.ASK_USER,
        user_question=patch.user_question,
        aborted=patch.operation == PlanPatchOperation.ABORT,
    )


def replay_plan_patch_history(records: tuple[PlanPatchRecord, ...], context: PlanPatchContext) -> tuple[Plan, ...]:
    """Validate each immutable before/after transition and return replay states."""

    states: list[Plan] = []
    for expected_revision, record in enumerate(records, start=1):
        if record.revision != expected_revision:
            raise PlanPatchRejected("Patch history revisions are not contiguous.")
        if _hash(record.before_json) != record.before_hash or _hash(record.after_json) != record.after_hash:
            raise PlanPatchRejected("Patch history snapshot hash mismatch.")
        before = Plan.model_validate_json(record.before_json).model_copy(update={
            "patch_revision": expected_revision - 1,
            "patch_history": records[: expected_revision - 1],
        })
        replayed = _mutate(before, record.patch, context)
        if _dump(replayed) != record.after_json:
            raise PlanPatchRejected("Patch replay does not match recorded after-state.")
        states.append(replayed)
    return tuple(states)
