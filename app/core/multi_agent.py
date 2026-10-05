"""Domain-neutral contracts for policy-constrained multi-Agent orchestration.

This module intentionally contains data contracts and deterministic validation only.
Scheduling, persistence, context derivation, and Agent execution are introduced in
later phases so that the existing single-Agent runtime remains unchanged.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MULTI_AGENT_SCHEMA_VERSION = 1
GENERAL_AGENT_ID = "general_agent"


def utc_now() -> datetime:
    return datetime.now(UTC)


class ValueObject(BaseModel):
    """Immutable nested protocol value without an independent durable identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MultiAgentModel(ValueObject):
    """Immutable, versioned envelope for a durable protocol object."""

    schema_version: int = Field(default=MULTI_AGENT_SCHEMA_VERSION, ge=1)
    object_version: int = Field(default=1, ge=1)
    correlation_id: str = Field(min_length=1, max_length=200)


class PlanStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    QUEUED = "queued"
    RUNNING = "running"
    REPLANNING = "replanning"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ChildRunStatus(str, Enum):
    WAITING_CONFIRMATION = "waiting_confirmation"
    BLOCKED = "blocked"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskResultStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class SideEffectLevel(str, Enum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"


class ForkCallerKind(str, Enum):
    ROOT_PLANNER = "root_planner"
    COORDINATOR = "coordinator"
    LEAF = "leaf"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"


class AggregationStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CONFLICTING = "conflicting"
    BLOCKED = "blocked"
    FAILED = "failed"


class PlanPatchOperation(str, Enum):
    RETRY_STEP = "retry_step"
    REDUCED_SCOPE = "reduced_scope"
    ALTERNATIVE_STEP = "alternative_step"
    SKIP_AND_DEGRADE = "skip_and_degrade"
    ASK_USER = "ask_user"
    ABORT = "abort"


class MultiAgentEventType(str, Enum):
    PLAN_CREATED = "plan_created"
    FORK_SUBTASKS_REQUESTED = "fork_subtasks_requested"
    FORK_SUBTASKS_VALIDATED = "fork_subtasks_validated"
    FORK_SUBTASKS_REJECTED = "fork_subtasks_rejected"
    SUBTASK_CREATED = "subtask_created"
    SUBTASK_QUEUED = "subtask_queued"
    SUBTASK_STARTED = "subtask_started"
    SUBTASK_COMPLETED = "subtask_completed"
    SUBTASK_FAILED = "subtask_failed"
    SUBTASK_CANCELLED = "subtask_cancelled"
    REPLAN_REQUIRED = "replan_required"


class DependencyEdge(ValueObject):
    """A prerequisite-to-dependent edge in a Plan DAG."""

    source_step_id: str = Field(min_length=1)
    target_step_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def source_and_target_must_differ(self) -> DependencyEdge:
        if self.source_step_id == self.target_step_id:
            raise ValueError("A dependency edge cannot reference the same step twice.")
        return self


class RuntimeBudget(ValueObject):
    max_tokens: int | None = Field(default=None, ge=1)
    max_llm_calls: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_wall_time_seconds: int | None = Field(default=None, ge=1)


class ScopeGrant(ValueObject):
    """Requested or effective capability scope; later policy code intersects it."""

    workspace_paths: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    account_ids: tuple[str, ...] = ()
    allowed_packages: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    side_effect_level: SideEffectLevel = SideEffectLevel.NONE


class ForkPolicy(ValueObject):
    """Server-owned limits applied to every structured fork request."""

    max_depth: int = Field(ge=0)
    max_children: int = Field(ge=0)
    max_fork_size: int = Field(ge=1)
    allowed_scope: ScopeGrant
    allowed_agent_ids: tuple[str, ...] = (GENERAL_AGENT_ID,)
    allowed_inference_profile_ids: tuple[str, ...] = ()
    # Additional fork generations available to one Coordinator. Coordinators
    # themselves may only produce leaf PlanSteps, so this cannot recurse.
    coordinator_max_depth: int = Field(default=1, ge=0)
    coordinator_max_children: int = Field(default=2, ge=0)
    coordinator_max_fork_size: int = Field(default=2, ge=0)
    root_budget: RuntimeBudget = Field(default_factory=RuntimeBudget)
    coordinator_budget: RuntimeBudget = Field(
        default_factory=lambda: RuntimeBudget(
            max_tokens=32_768,
            max_llm_calls=12,
            max_tool_calls=16,
            max_wall_time_seconds=300,
        )
    )
    leaf_budget: RuntimeBudget = Field(
        default_factory=lambda: RuntimeBudget(
            max_tokens=16_384,
            max_llm_calls=8,
            max_tool_calls=10,
            max_wall_time_seconds=180,
        )
    )

    @model_validator(mode="after")
    def role_budgets_are_bounded(self) -> ForkPolicy:
        if not _budget_strictly_below(self.coordinator_budget, self.root_budget):
            raise ValueError("Coordinator budget must be strictly below root budget.")
        if not _budget_strictly_below(self.leaf_budget, self.coordinator_budget):
            raise ValueError("Leaf budget must be strictly below coordinator budget.")
        return self

    def budget_for(self, kind: ForkCallerKind) -> RuntimeBudget:
        """Return the server-owned budget ceiling for a derived Agent kind."""
        if kind == ForkCallerKind.ROOT_PLANNER:
            return self.root_budget
        parent = (
            self.coordinator_budget
            if kind == ForkCallerKind.LEAF
            else self.root_budget
        )
        child = self.leaf_budget if kind == ForkCallerKind.LEAF else self.coordinator_budget
        return _minimum_budget(parent, child)


class ForkValidationContext(ValueObject):
    """Runtime-supplied caller and access bounds, never planner-controlled input."""

    parent_effective_scope: ScopeGrant
    session_scope: ScopeGrant
    workspace_scope: ScopeGrant
    parent_step_status: PlanStepStatus
    caller_kind: ForkCallerKind
    created_by_run_id: str = Field(min_length=1, max_length=200)
    current_depth: int = Field(ge=0)
    coordinator_depth: int = Field(default=0, ge=0)
    existing_child_count: int = Field(ge=0)
    known_step_ids: tuple[str, ...] = Field(min_length=1)
    current_objective_fingerprint: str | None = None
    ancestor_objective_fingerprints: tuple[str, ...] = ()


class PlanStep(MultiAgentModel):
    step_id: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    role: str | None = Field(default=None, max_length=100)
    agent_id: str = Field(default=GENERAL_AGENT_ID, min_length=1, max_length=100)
    agent_version: str | None = Field(default=None, max_length=100)
    inference_profile_id: str | None = Field(default=None, max_length=100)
    # Server-resolved immutable inference selection, populated on first schedule.
    inference_client_name: str | None = Field(default=None, max_length=100)
    inference_model: str | None = Field(default=None, max_length=200)
    inference_reasoning_effort: Literal["low", "medium", "high"] | None = None
    inference_selection_source: Literal["request", "server_profile", "default"] = "default"
    # Server-derived hierarchy role; deliberately absent from ForkSubtaskSpec.
    agent_kind: ForkCallerKind | None = None
    depends_on: tuple[str, ...] = ()
    parallel_group: str | None = Field(default=None, max_length=100)
    allowed_packages: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    effective_scope: ScopeGrant | None = None
    input_refs: tuple[str, ...] = ()
    output_contract: str = Field(min_length=1)
    verification_criteria: tuple[str, ...] = ()
    budget: RuntimeBudget | None = None
    side_effect_level: SideEffectLevel = SideEffectLevel.NONE
    status: PlanStepStatus = PlanStepStatus.PENDING
    fork_operation_id: str | None = Field(default=None, max_length=200)
    fork_parent_step_id: str | None = Field(default=None, max_length=200)
    fork_depth: int | None = Field(default=None, ge=1)
    created_by_run_id: str | None = Field(default=None, max_length=200)
    degraded_dependency_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def dependencies_must_be_unique(self) -> PlanStep:
        if self.step_id in self.depends_on:
            raise ValueError("A plan step cannot depend on itself.")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("A plan step cannot contain duplicate dependencies.")
        provenance = (
            self.fork_operation_id,
            self.fork_parent_step_id,
            self.fork_depth,
            self.created_by_run_id,
        )
        if any(value is not None for value in provenance) and any(value is None for value in provenance):
            raise ValueError("Forked plan steps require complete fork provenance.")
        return self

    def transition_to(self, status: PlanStepStatus) -> PlanStep:
        """Return the next immutable state; dependency checks belong to ``Plan``."""

        allowed = {
            PlanStepStatus.PENDING: {
                PlanStepStatus.BLOCKED,
                PlanStepStatus.READY,
                PlanStepStatus.SKIPPED,
                PlanStepStatus.CANCELLED,
            },
            PlanStepStatus.BLOCKED: {
                PlanStepStatus.PENDING,
                PlanStepStatus.READY,
                PlanStepStatus.SKIPPED,
                PlanStepStatus.CANCELLED,
            },
            PlanStepStatus.READY: {
                PlanStepStatus.RUNNING,
                PlanStepStatus.BLOCKED,
                PlanStepStatus.SKIPPED,
                PlanStepStatus.CANCELLED,
            },
            PlanStepStatus.RUNNING: {
                PlanStepStatus.WAITING,
                PlanStepStatus.COMPLETED,
                PlanStepStatus.FAILED,
                PlanStepStatus.BLOCKED,
                PlanStepStatus.CANCELLED,
            },
            PlanStepStatus.WAITING: {
                PlanStepStatus.RUNNING,
                PlanStepStatus.FAILED,
                PlanStepStatus.BLOCKED,
                PlanStepStatus.CANCELLED,
            },
            PlanStepStatus.COMPLETED: set(),
            PlanStepStatus.FAILED: set(),
            PlanStepStatus.SKIPPED: set(),
            PlanStepStatus.CANCELLED: set(),
        }
        if status not in allowed[self.status]:
            raise ValueError(
                f"Invalid plan step status transition: {self.status.value} -> {status.value}"
            )
        return self.model_copy(update={"status": status})


class PlanPatch(ValueObject):
    """Planner-proposed replan change; authority remains in PlanPatchContext."""

    required_fields_by_operation: ClassVar[dict[PlanPatchOperation, tuple[str, ...]]] = {
        PlanPatchOperation.RETRY_STEP: ("target_step_id",),
        PlanPatchOperation.REDUCED_SCOPE: ("target_step_id", "reduced_scope"),
        PlanPatchOperation.ALTERNATIVE_STEP: ("target_step_id", "alternative_step"),
        PlanPatchOperation.SKIP_AND_DEGRADE: ("target_step_id", "degradation_note"),
        PlanPatchOperation.ASK_USER: ("user_question",),
        PlanPatchOperation.ABORT: (),
    }

    patch_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)
    operation: PlanPatchOperation
    reason: str = Field(min_length=1)
    target_step_id: str | None = Field(default=None, max_length=200)
    reduced_scope: ScopeGrant | None = None
    alternative_step: PlanStep | None = None
    degradation_note: str | None = Field(
        default=None,
        description=(
            "Explicit unmet original requirements when skipping or changing an "
            "alternative step's literal output contract or verification criteria; "
            "an execution replacement is not proof of original contract coverage."
        ),
    )
    user_question: str | None = None
    budget: RuntimeBudget | None = None

    @model_validator(mode="after")
    def operation_fields_are_consistent(self) -> PlanPatch:
        required = self.required_fields_by_operation[self.operation]
        for field_name in required:
            if getattr(self, field_name) is None:
                raise ValueError(f"{self.operation.value} patch requires {field_name}.")
        allowed = set(required) | {"budget"}
        if self.operation == PlanPatchOperation.ALTERNATIVE_STEP:
            allowed.add("degradation_note")
            if self.degradation_note is not None and not self.degradation_note.strip():
                raise ValueError("Alternative degradation_note must be nonempty.")
        supplied = {
            name for name in ("target_step_id", "reduced_scope", "alternative_step", "degradation_note", "user_question")
            if getattr(self, name) is not None
        }
        if supplied - allowed:
            raise ValueError(f"{self.operation.value} patch contains incompatible fields.")
        if self.budget is not None and self.operation not in {
            PlanPatchOperation.RETRY_STEP,
            PlanPatchOperation.ALTERNATIVE_STEP,
        }:
            raise ValueError("budget is only valid for retry_step or alternative_step patches.")
        if (
            self.alternative_step is not None
            and self.alternative_step.agent_kind is not None
        ):
            raise ValueError("agent_kind is server-owned and cannot be set in a PlanPatch.")
        if self.alternative_step is not None and self.alternative_step.agent_version is not None:
            raise ValueError("agent_version is server-owned and cannot be set in a PlanPatch.")
        if self.alternative_step is not None and any((
            self.alternative_step.inference_client_name is not None,
            self.alternative_step.inference_model is not None,
            self.alternative_step.inference_reasoning_effort is not None,
        )):
            raise ValueError("Resolved inference selection is server-owned and cannot be set in a PlanPatch.")
        return self


class PlanPatchRecord(ValueObject):
    """Append-only, self-contained before/after record for deterministic replay."""

    revision: int = Field(ge=1)
    patch: PlanPatch
    before_json: str = Field(min_length=2)
    after_json: str = Field(min_length=2)
    before_hash: str = Field(min_length=64, max_length=64)
    after_hash: str = Field(min_length=64, max_length=64)
    recorded_at: datetime = Field(default_factory=utc_now)


class PlanPatchContext(ValueObject):
    """Server-owned limits used when applying a patch."""

    fork_policy: ForkPolicy
    parent_effective_scope: ScopeGrant
    session_scope: ScopeGrant
    workspace_scope: ScopeGrant
    remaining_budget: RuntimeBudget | None = None
    max_retries_per_step: int = Field(default=1, ge=0)


class ForkSubtaskSpec(ValueObject):
    """Planner-requested subtask fields; it deliberately has no policy/budget knobs."""

    step_id: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    role: str | None = Field(default=None, max_length=100)
    agent_id: str = Field(default=GENERAL_AGENT_ID, min_length=1, max_length=100)
    inference_profile_id: str | None = Field(default=None, max_length=100)
    depends_on: tuple[str, ...] = ()
    parallel_group: str | None = Field(default=None, max_length=100)
    requested_scope: ScopeGrant = Field(default_factory=ScopeGrant)
    input_refs: tuple[str, ...] = ()
    output_contract: str = Field(min_length=1)
    verification_criteria: tuple[str, ...] = ()

    @model_validator(mode="after")
    def dependencies_must_be_unique(self) -> ForkSubtaskSpec:
        if self.step_id in self.depends_on:
            raise ValueError("A forked subtask cannot depend on itself.")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("A forked subtask cannot contain duplicate dependencies.")
        return self


class ForkSubtasksOperation(MultiAgentModel):
    """The only structured payload a planner may use to request a fork."""

    operation: Literal["fork_subtasks"] = "fork_subtasks"
    operation_id: str = Field(min_length=1, max_length=200)
    parent_step_id: str = Field(min_length=1, max_length=200)
    subtasks: tuple[ForkSubtaskSpec, ...] = Field(min_length=1)


class ForkScopeAdjustment(ValueObject):
    step_id: str = Field(min_length=1, max_length=200)
    requested_scope: ScopeGrant
    validated_scope: ScopeGrant


class ValidatedForkSubtasks(MultiAgentModel):
    """Auditable output from deterministic fork validation; creates no Child Runs."""

    operation_id: str = Field(min_length=1, max_length=200)
    requested_operation: ForkSubtasksOperation
    validated_steps: tuple[PlanStep, ...] = Field(min_length=1)
    scope_adjustments: tuple[ForkScopeAdjustment, ...] = ()
    validated_at: datetime = Field(default_factory=utc_now)


class ForkPolicyViolation(ValueError):
    """The planner requested a fork outside the server-owned policy boundary."""


_SIDE_EFFECT_ORDER = {
    SideEffectLevel.NONE: 0,
    SideEffectLevel.READ: 1,
    SideEffectLevel.WRITE: 2,
    SideEffectLevel.EXTERNAL: 3,
}


def objective_fingerprint(objective: str) -> str:
    """Return a stable fingerprint for exact objective text after normalization.

    Unicode compatibility forms and case are normalized; whitespace and
    punctuation separate tokens so punctuation-only edits do not evade loop
    checks, while symbol characters remain meaningful (for example C++ vs C#).
    """
    normalized = unicodedata.normalize("NFKC", objective).casefold()
    tokens: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            tokens.append("".join(current))
            current.clear()

    for char in normalized:
        category = unicodedata.category(char)
        if category[0] in {"L", "N"}:
            current.append(char)
        elif category[0] == "S":
            flush()
            tokens.append(char)
        else:
            flush()
    flush()
    canonical = " ".join(tokens)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _intersect_scope(*scopes: ScopeGrant) -> ScopeGrant:
    if not scopes:
        raise ValueError("At least one scope is required.")

    def intersection(values: tuple[str, ...], permitted: tuple[str, ...]) -> tuple[str, ...]:
        permitted_set = set(permitted)
        return tuple(value for value in values if value in permitted_set)

    requested, *limits = scopes

    def all_intersection(values: tuple[str, ...], attribute: str) -> tuple[str, ...]:
        result = values
        for limit in limits:
            if attribute == "workspace_paths":
                result = _intersect_workspace_paths(result, getattr(limit, attribute))
                continue
            result = intersection(result, getattr(limit, attribute))
        return result

    effective_level = min(
        (scope.side_effect_level for scope in scopes),
        key=lambda level: _SIDE_EFFECT_ORDER[level],
    )
    return ScopeGrant(
        workspace_paths=all_intersection(requested.workspace_paths, "workspace_paths"),
        source_ids=all_intersection(requested.source_ids, "source_ids"),
        account_ids=all_intersection(requested.account_ids, "account_ids"),
        allowed_packages=all_intersection(requested.allowed_packages, "allowed_packages"),
        allowed_tools=all_intersection(requested.allowed_tools, "allowed_tools"),
        side_effect_level=effective_level,
    )


def _intersect_workspace_paths(
    requested: tuple[str, ...], permitted: tuple[str, ...]
) -> tuple[str, ...]:
    """Intersect directory grants by containment and return the narrower path."""

    if not requested or not permitted:
        return ()
    result: list[str] = []
    for requested_value in requested:
        requested_path = Path(requested_value).resolve(strict=False)
        for permitted_value in permitted:
            permitted_path = Path(permitted_value).resolve(strict=False)
            try:
                requested_path.relative_to(permitted_path)
                narrower = requested_path
            except ValueError:
                try:
                    permitted_path.relative_to(requested_path)
                    narrower = permitted_path
                except ValueError:
                    continue
            value = str(narrower)
            if value not in result:
                result.append(value)
    return tuple(result)


def _minimum_budget(left: RuntimeBudget, right: RuntimeBudget) -> RuntimeBudget:
    def minimum(a: int | None, b: int | None) -> int | None:
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)

    return RuntimeBudget(
        max_tokens=minimum(left.max_tokens, right.max_tokens),
        max_llm_calls=minimum(left.max_llm_calls, right.max_llm_calls),
        max_tool_calls=minimum(left.max_tool_calls, right.max_tool_calls),
        max_wall_time_seconds=minimum(
            left.max_wall_time_seconds, right.max_wall_time_seconds
        ),
    )


def _budget_strictly_below(candidate: RuntimeBudget, parent: RuntimeBudget) -> bool:
    strictly_lower = False
    for field_name in (
        "max_tokens",
        "max_llm_calls",
        "max_tool_calls",
        "max_wall_time_seconds",
    ):
        child_value = getattr(candidate, field_name)
        parent_value = getattr(parent, field_name)
        if parent_value is None:
            strictly_lower = strictly_lower or child_value is not None
        elif child_value is None or child_value > parent_value:
            return False
        else:
            strictly_lower = strictly_lower or child_value < parent_value
    return strictly_lower


def validate_fork_subtasks(
    operation: ForkSubtasksOperation,
    *,
    policy: ForkPolicy,
    context: ForkValidationContext,
) -> ValidatedForkSubtasks:
    """Validate a planner request and derive server-authorized PlanSteps.

    This is intentionally a pure operation: it does not persist a plan, emit an
    event, or create a ChildRun. Planner scope remains auditable but cannot
    change the effective grant, which comes from server-owned ceilings.
    """

    if context.parent_step_status not in {PlanStepStatus.RUNNING, PlanStepStatus.WAITING}:
        raise ForkPolicyViolation("Fork parent step is not active.")
    if context.caller_kind == ForkCallerKind.LEAF:
        raise ForkPolicyViolation("Leaf Agents cannot fork subtasks.")
    coordinator_caller = context.caller_kind == ForkCallerKind.COORDINATOR
    if (
        coordinator_caller
        and context.coordinator_depth >= policy.coordinator_max_depth
    ):
        raise ForkPolicyViolation("Coordinator exceeds its server-owned max_depth.")
    if context.current_depth + 1 > policy.max_depth:
        raise ForkPolicyViolation("Fork exceeds max_depth.")
    effective_max_fork_size = policy.max_fork_size
    effective_max_children = policy.max_children
    if coordinator_caller:
        effective_max_fork_size = min(
            policy.coordinator_max_fork_size, policy.max_fork_size
        )
        effective_max_children = min(
            policy.coordinator_max_children, policy.max_children
        )
    if len(operation.subtasks) > effective_max_fork_size:
        raise ForkPolicyViolation("Fork exceeds max_fork_size.")
    if context.existing_child_count + len(operation.subtasks) > effective_max_children:
        raise ForkPolicyViolation("Fork exceeds max_children.")
    known = set(context.known_step_ids)
    if operation.parent_step_id not in known:
        raise ForkPolicyViolation("Fork parent_step_id does not exist.")
    requested_ids = [subtask.step_id for subtask in operation.subtasks]
    if len(set(requested_ids)) != len(requested_ids):
        raise ForkPolicyViolation("Fork contains duplicate subtask step IDs.")
    if known.intersection(requested_ids):
        raise ForkPolicyViolation("Fork subtask step ID already exists in the plan.")
    blocked_objectives = {
        fingerprint
        for fingerprint in (
            context.current_objective_fingerprint,
            *context.ancestor_objective_fingerprints,
        )
        if fingerprint
    }
    seen_objectives: set[str] = set()
    for subtask in operation.subtasks:
        if subtask.agent_id not in policy.allowed_agent_ids:
            raise ForkPolicyViolation(f"Fork requests an unavailable Agent: {subtask.agent_id}")
        if (
            subtask.inference_profile_id is not None
            and subtask.inference_profile_id not in policy.allowed_inference_profile_ids
        ):
            raise ForkPolicyViolation(
                f"Fork requests an unavailable inference profile: {subtask.inference_profile_id}"
            )
        fingerprint = objective_fingerprint(subtask.objective)
        if fingerprint in blocked_objectives:
            raise ForkPolicyViolation(
                "Fork subtask objective repeats the current or an ancestor objective."
            )
        if fingerprint in seen_objectives:
            raise ForkPolicyViolation(
                "Fork contains semantically duplicate subtask objectives."
            )
        seen_objectives.add(fingerprint)
    available_dependencies = known | set(requested_ids)
    adjacency = {step_id: set() for step_id in requested_ids}
    for subtask in operation.subtasks:
        for dependency_id in subtask.depends_on:
            if dependency_id not in available_dependencies:
                raise ForkPolicyViolation(f"Fork dependency does not exist: {dependency_id}")
            if dependency_id in adjacency:
                adjacency[dependency_id].add(subtask.step_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ForkPolicyViolation("Fork contains cyclic subtask dependencies.")
        if step_id in visited:
            return
        visiting.add(step_id)
        for target_step_id in adjacency[step_id]:
            visit(target_step_id)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in adjacency:
        visit(step_id)

    validated_steps: list[PlanStep] = []
    adjustments: list[ForkScopeAdjustment] = []
    policy_scope = policy.allowed_scope.model_copy(
        update={
            # An empty path/source/account limit means the fork policy adds no
            # constraint for that dimension. The authoritative grants still
            # come from the parent/session/workspace scopes below. Empty tool
            # and package sets retain deny-all semantics.
            "workspace_paths": (
                policy.allowed_scope.workspace_paths
                or context.workspace_scope.workspace_paths
            ),
            "source_ids": (
                policy.allowed_scope.source_ids
                or context.parent_effective_scope.source_ids
            ),
            "account_ids": (
                policy.allowed_scope.account_ids
                or context.parent_effective_scope.account_ids
            ),
        }
    )
    for subtask in operation.subtasks:
        # Planner-authored scope is descriptive and auditable; it cannot grant
        # or remove the child Agent's registered capabilities. The server
        # computes the effective grant exclusively from the parent, session,
        # workspace, and policy ceilings. Tool-level write calls still enter
        # the normal safety review gate.
        effective_scope = _intersect_scope(
            ScopeGrant(
                workspace_paths=policy_scope.workspace_paths,
                source_ids=policy_scope.source_ids,
                account_ids=policy_scope.account_ids,
                allowed_packages=policy_scope.allowed_packages,
                allowed_tools=policy_scope.allowed_tools,
                side_effect_level=policy_scope.side_effect_level,
            ),
            context.parent_effective_scope,
            context.session_scope,
            context.workspace_scope,
        )
        if effective_scope != subtask.requested_scope:
            adjustments.append(
                ForkScopeAdjustment(
                    step_id=subtask.step_id,
                    requested_scope=subtask.requested_scope,
                    validated_scope=effective_scope,
                )
            )
        validated_steps.append(
            PlanStep(
                correlation_id=operation.correlation_id,
                step_id=subtask.step_id,
                objective=subtask.objective,
                role=subtask.role,
                agent_id=subtask.agent_id,
                inference_profile_id=subtask.inference_profile_id,
                # Agent kind is derived from the trusted caller lineage, never
                # selected by the Planner. Coordinators produce leaves only.
                agent_kind=(
                    ForkCallerKind.LEAF
                    if coordinator_caller
                    else ForkCallerKind.COORDINATOR
                ),
                depends_on=subtask.depends_on,
                parallel_group=subtask.parallel_group,
                allowed_packages=effective_scope.allowed_packages,
                allowed_tools=effective_scope.allowed_tools,
                effective_scope=effective_scope,
                input_refs=subtask.input_refs,
                output_contract=subtask.output_contract,
                verification_criteria=subtask.verification_criteria,
                budget=policy.budget_for(
                    ForkCallerKind.LEAF
                    if coordinator_caller
                    else ForkCallerKind.COORDINATOR
                ),
                side_effect_level=effective_scope.side_effect_level,
                fork_operation_id=operation.operation_id,
                fork_parent_step_id=operation.parent_step_id,
                fork_depth=context.current_depth + 1,
                created_by_run_id=context.created_by_run_id,
            )
        )
    return ValidatedForkSubtasks(
        correlation_id=operation.correlation_id,
        operation_id=operation.operation_id,
        requested_operation=operation,
        validated_steps=tuple(validated_steps),
        scope_adjustments=tuple(adjustments),
    )


class Plan(MultiAgentModel):
    plan_id: str = Field(min_length=1, max_length=200)
    parent_run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    status: PlanStatus = PlanStatus.DRAFT
    requested_at: datetime = Field(default_factory=utc_now)
    validated_at: datetime | None = None
    patch_revision: int = Field(default=0, ge=0)
    patch_history: tuple[PlanPatchRecord, ...] = ()

    @model_validator(mode="after")
    def validate_dag(self) -> Plan:
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("A plan cannot contain duplicate step IDs.")
        known = set(step_ids)
        declared = {
            (dependency_id, step.step_id)
            for step in self.steps
            for dependency_id in step.depends_on
        }
        for source_step_id, target_step_id in declared:
            if source_step_id not in known:
                raise ValueError(f"Plan step dependency does not exist: {source_step_id}")
            if target_step_id not in known:
                raise ValueError(f"Plan step does not exist: {target_step_id}")
        adjacency = {step_id: set() for step_id in known}
        for source_step_id, target_step_id in declared:
            adjacency[source_step_id].add(target_step_id)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("A plan cannot contain cyclic dependencies.")
            if step_id in visited:
                return
            visiting.add(step_id)
            for target_step_id in adjacency[step_id]:
                visit(target_step_id)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in known:
            visit(step_id)
        revisions = [record.revision for record in self.patch_history]
        if revisions != list(range(1, self.patch_revision + 1)):
            raise ValueError("Plan patch history must be contiguous and append-only.")
        if any(record.patch.plan_id != self.plan_id for record in self.patch_history):
            raise ValueError("Plan patch history contains a patch for another plan.")
        return self

    @property
    def dependency_edges(self) -> tuple[DependencyEdge, ...]:
        """Derive edges from the canonical ``PlanStep.depends_on`` declaration."""

        return tuple(
            DependencyEdge(source_step_id=dependency_id, target_step_id=step.step_id)
            for step in self.steps
            for dependency_id in step.depends_on
        )

    def transition_to(self, status: PlanStatus) -> Plan:
        allowed = {
            PlanStatus.DRAFT: {PlanStatus.VALIDATED, PlanStatus.CANCELLED},
            PlanStatus.VALIDATED: {PlanStatus.QUEUED, PlanStatus.CANCELLED},
            PlanStatus.QUEUED: {PlanStatus.RUNNING, PlanStatus.CANCELLED},
            PlanStatus.RUNNING: {PlanStatus.REPLANNING, PlanStatus.WAITING_USER, PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED},
            PlanStatus.REPLANNING: {PlanStatus.QUEUED, PlanStatus.RUNNING, PlanStatus.WAITING_USER, PlanStatus.FAILED, PlanStatus.CANCELLED},
            PlanStatus.WAITING_USER: {PlanStatus.REPLANNING, PlanStatus.RUNNING, PlanStatus.FAILED, PlanStatus.CANCELLED},
            PlanStatus.COMPLETED: set(),
            PlanStatus.FAILED: set(),
            PlanStatus.CANCELLED: set(),
        }
        if status not in allowed[self.status]:
            raise ValueError(f"Invalid plan status transition: {self.status.value} -> {status.value}")
        if status == PlanStatus.COMPLETED:
            unresolved = [
                step.step_id
                for step in self.steps
                if step.status
                not in {
                    PlanStepStatus.COMPLETED,
                    PlanStepStatus.SKIPPED,
                    PlanStepStatus.CANCELLED,
                }
            ]
            if unresolved:
                raise ValueError(
                    "A completed plan cannot contain unresolved or failed steps: "
                    + ", ".join(unresolved)
                )
        return self.model_copy(
            update={"status": status, "validated_at": utc_now() if status == PlanStatus.VALIDATED else self.validated_at}
        )

    def ready_step_ids(self) -> tuple[str, ...]:
        """Return pending/blocked steps whose direct dependencies all completed."""

        statuses = {step.step_id: step.status for step in self.steps}
        return tuple(
            step.step_id
            for step in self.steps
            if step.status in {PlanStepStatus.PENDING, PlanStepStatus.BLOCKED}
            and all(statuses[dependency] == PlanStepStatus.COMPLETED for dependency in step.depends_on)
        )

    def transition_step(self, step_id: str, status: PlanStepStatus) -> Plan:
        """Transition one step without permitting a dependency-inconsistent ready state."""

        by_id = {step.step_id: step for step in self.steps}
        step = by_id.get(step_id)
        if step is None:
            raise KeyError(f"Plan step not found: {step_id}")
        requires_completed_dependencies = {
            PlanStepStatus.READY,
            PlanStepStatus.RUNNING,
            PlanStepStatus.WAITING,
            PlanStepStatus.COMPLETED,
        }
        if status in requires_completed_dependencies:
            incomplete = [
                dependency_id
                for dependency_id in step.depends_on
                if by_id[dependency_id].status != PlanStepStatus.COMPLETED
            ]
            if incomplete:
                raise ValueError(
                    f"Plan step {step_id} has incomplete dependencies: {', '.join(incomplete)}"
                )
        transitioned = step.transition_to(status)
        return self.model_copy(
            update={
                "steps": tuple(
                    transitioned if candidate.step_id == step_id else candidate
                    for candidate in self.steps
                )
            }
        )


class ChildRun(MultiAgentModel):
    child_run_id: str = Field(min_length=1, max_length=200)
    parent_run_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    attempt: int = Field(ge=1)
    status: ChildRunStatus = ChildRunStatus.QUEUED
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    waiting_since: datetime | None = None
    error_type: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def lifecycle_fields_must_match_status(self) -> ChildRun:
        terminal_times = {
            ChildRunStatus.COMPLETED: self.completed_at,
            ChildRunStatus.FAILED: self.failed_at,
            ChildRunStatus.CANCELLED: self.cancelled_at,
            ChildRunStatus.TIMED_OUT: self.failed_at,
        }
        terminal_time = terminal_times.get(self.status)
        if self.status in terminal_times and terminal_time is None:
            raise ValueError(f"{self.status.value} child runs require their lifecycle timestamp.")
        if self.status == ChildRunStatus.WAITING_CONFIRMATION and self.waiting_since is None:
            raise ValueError("Waiting child runs require waiting_since.")
        if self.status in {ChildRunStatus.FAILED, ChildRunStatus.TIMED_OUT, ChildRunStatus.BLOCKED} and not (
            self.error_type and self.error
        ):
            raise ValueError(f"{self.status.value} child runs require error details.")
        return self


class EvidenceRef(ValueObject):
    evidence_id: str = Field(min_length=1, max_length=200)
    source_ref: str = Field(min_length=1)
    source_id: str | None = None
    account_ref: str | None = Field(default=None, max_length=200)
    content_hash: str | None = None
    freshness_at: datetime | None = None
    untrusted_data: bool = False


class Artifact(MultiAgentModel):
    artifact_id: str = Field(min_length=1, max_length=200)
    producing_run_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1)
    content_ref: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class MemoryReference(ValueObject):
    """A server-resolved immutable memory version explicitly assigned to a child."""

    memory_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=1000)
    scope: Literal["global", "project"]
    project_id: str | None = None
    source_ids: tuple[str, ...] = ()
    source_count: int | None = Field(default=None, ge=0)
    content_truncated: bool = False
    updated_at: str


class ContextSnapshot(MultiAgentModel):
    snapshot_id: str = Field(min_length=1, max_length=200)
    parent_snapshot_id: str | None = Field(default=None, max_length=200)
    parent_run_id: str = Field(min_length=1, max_length=200)
    child_run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    agent_kind: ForkCallerKind | None = None
    agent_id: str = Field(default=GENERAL_AGENT_ID, min_length=1, max_length=100)
    agent_version: str | None = Field(default=None, max_length=100)
    inference_profile_id: str | None = Field(default=None, max_length=100)
    inference_client_name: str | None = Field(default=None, max_length=100)
    inference_model: str | None = Field(default=None, max_length=200)
    inference_reasoning_effort: Literal["low", "medium", "high"] | None = None
    inference_selection_source: Literal["request", "server_profile", "default"] = "default"
    objective: str = Field(min_length=1)
    output_contract: str = Field(min_length=1)
    input_refs: tuple[str, ...] = ()
    memory_refs: tuple[MemoryReference, ...] = ()
    dependency_result_refs: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    effective_scope: ScopeGrant
    full_workspace_authority: bool = False
    full_data_authority: bool = False
    verification_criteria: tuple[str, ...] = ()
    budget: RuntimeBudget
    policy_version: str = Field(min_length=1)
    workspace_version: str = Field(min_length=1)
    permission_version: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    stale: bool = False


class TaskAssignment(MultiAgentModel):
    assignment_id: str = Field(min_length=1, max_length=200)
    child_run_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(default=GENERAL_AGENT_ID, min_length=1, max_length=100)
    agent_version: str | None = Field(default=None, max_length=100)
    inference_profile_id: str | None = Field(default=None, max_length=100)
    objective: str = Field(min_length=1)
    output_contract: str = Field(min_length=1)
    verification_criteria: tuple[str, ...] = ()


class FailureDetail(ValueObject):
    category: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1)
    retryable: bool = False
    recommended_actions: tuple[str, ...] = ()


class VerificationCheck(ValueObject):
    check_id: str = Field(min_length=1, max_length=100)
    status: VerificationStatus
    summary: str = Field(min_length=1)
    required: bool = True


class VerificationResult(MultiAgentModel):
    verification_id: str = Field(min_length=1, max_length=200)
    status: VerificationStatus
    summary: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    checks: tuple[VerificationCheck, ...] = ()
    recommended_actions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def passed_verification_cannot_have_missing_requirements(self) -> VerificationResult:
        if self.status == VerificationStatus.PASSED and self.missing_requirements:
            raise ValueError("A passed verification cannot contain missing requirements.")
        if self.status == VerificationStatus.PASSED and any(
            check.required and check.status != VerificationStatus.PASSED
            for check in self.checks
        ):
            raise ValueError("A passed verification cannot contain an unsuccessful required check.")
        return self


class TaskResult(MultiAgentModel):
    result_id: str = Field(min_length=1, max_length=200)
    child_run_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    status: TaskResultStatus
    attempt: int = Field(default=1, ge=1)
    summary: str = Field(min_length=1)
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    verification: VerificationResult | None = None
    failure: FailureDetail | None = None
    missing_requirements: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    completed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def status_requires_consistent_outcome_details(self) -> TaskResult:
        failed_statuses = {
            TaskResultStatus.FAILED,
            TaskResultStatus.TIMED_OUT,
            TaskResultStatus.BLOCKED,
        }
        if self.status in failed_statuses and self.failure is None:
            raise ValueError(f"{self.status.value} task results require failure details.")
        if self.status == TaskResultStatus.COMPLETED and self.failure is not None:
            raise ValueError("A completed task result cannot contain failure details.")
        if self.status == TaskResultStatus.PARTIAL and not (
            self.missing_requirements or self.warnings
        ):
            raise ValueError("A partial task result requires missing requirements or warnings.")
        return self


class MultiAgentEventPayload(ValueObject):
    """JSON-safe payload stored as canonical JSON to prevent nested mutation."""

    json_data: str = Field(default="{}", min_length=2)

    @model_validator(mode="after")
    def must_be_a_json_object(self) -> MultiAgentEventPayload:
        try:
            value = json.loads(self.json_data)
        except json.JSONDecodeError as exc:
            raise ValueError("Event payload must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise TypeError("Event payload must be a JSON object.")
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != self.json_data:
            return self.model_copy(update={"json_data": canonical})
        return self

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MultiAgentEventPayload:
        return cls(json_data=json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.json_data)


class MultiAgentRunEvent(MultiAgentModel):
    """Durable event envelope; SSE may project a redacted subset later."""

    event_id: str = Field(min_length=1, max_length=200)
    event_type: MultiAgentEventType
    parent_run_id: str = Field(min_length=1, max_length=200)
    child_run_id: str | None = Field(default=None, max_length=200)
    plan_id: str | None = Field(default=None, max_length=200)
    step_id: str | None = Field(default=None, max_length=200)
    payload: MultiAgentEventPayload = Field(default_factory=MultiAgentEventPayload)
    occurred_at: datetime = Field(default_factory=utc_now)
