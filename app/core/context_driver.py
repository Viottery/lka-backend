"""Deterministic context derivation for isolated Child Agent execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.local_config import AgentInferenceProfile
from app.core.multi_agent import (
    ContextSnapshot,
    EvidenceRef,
    ForkCallerKind,
    MemoryReference,
    PlanStep,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
)


class ContextDerivationStatus(str, Enum):
    READY = "ready"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    SCOPE_DENIED = "scope_denied"
    BUDGET_EXCEEDED = "budget_exceeded"


class ContextViewMode(str, Enum):
    REFERENCE = "reference"
    WORKING = "working"
    FULL = "full"


class EvidenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: EvidenceRef
    summary: str = Field(min_length=1)
    excerpt: str = ""
    full_content: str | None = None


class DependencyResultView(BaseModel):
    """Bounded child-visible summary of a completed prerequisite result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str
    step_id: str
    summary: str
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    verification_summary: str | None = None
    missing_requirements: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class AgentView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str
    output_contract: str
    degraded_dependency_notes: tuple[str, ...] = ()
    verification_criteria: tuple[str, ...] = ()
    dependency_result_refs: tuple[str, ...] = ()
    dependency_results: tuple[DependencyResultView, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    evidence_summaries: tuple[str, ...] = ()
    evidence_content: tuple[str, ...] = ()
    memory_refs: tuple[MemoryReference, ...] = ()
    mode: ContextViewMode
    compression_warnings: tuple[str, ...] = ()


class ToolView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    child_run_id: str | None = None
    max_tool_calls: int | None = Field(default=None, ge=1)
    allowed_packages: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    allowed_source_ids: tuple[str, ...] = ()
    allowed_account_ids: tuple[str, ...] = ()
    memory_refs: tuple[MemoryReference, ...] = ()
    full_workspace_authority: bool = False
    full_data_authority: bool = False
    side_effect_level: SideEffectLevel
    expires_at: datetime | None = None

    def allows_tool(self, *, tool_name: str, package: str | None, read_only: bool | None) -> bool:
        return self.denial_reason(tool_name=tool_name, package=package, read_only=read_only) is None

    def denial_reason(self, *, tool_name: str, package: str | None, read_only: bool | None) -> str | None:
        """Explain only this view's first refusal, never grant execution or retry."""
        if self.expires_at is not None and self.expires_at <= datetime.now(UTC):
            return "context_expired"
        if self.allowed_tools:
            if tool_name not in self.allowed_tools:
                return "tool_not_granted"
            if self.allowed_packages and package not in self.allowed_packages:
                return "package_not_granted"
        elif not package or package not in self.allowed_packages:
            return "package_not_granted"
        if self.side_effect_level in {SideEffectLevel.NONE, SideEffectLevel.READ} and read_only is not True:
            return "invocation_not_read_only"
        return None


class PlannerView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    step_id: str
    agent_kind: ForkCallerKind | None = None
    can_fork: bool = False
    dependency_result_refs: tuple[str, ...] = ()
    can_replan: bool = False


class AuditView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    child_run_id: str
    session_id: str
    policy_version: str
    workspace_version: str
    permission_version: str
    evidence_refs: tuple[str, ...] = ()
    untrusted_evidence_refs: tuple[str, ...] = ()
    compression_warnings: tuple[str, ...] = ()


class ContextViews(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: AgentView
    tool: ToolView
    planner: PlannerView
    audit: AuditView


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1, max_length=200)
    parent_snapshot_id: str | None = Field(default=None, max_length=200)
    child_run_id: str = Field(min_length=1, max_length=200)
    parent_run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=1, max_length=200)
    plan_step: PlanStep
    inference_profile: AgentInferenceProfile | None = None
    request_llm_client_name: str | None = None
    request_llm_model: str | None = None
    dependency_results: tuple[TaskResult, ...] = ()
    parent_effective_scope: ScopeGrant
    session_scope: ScopeGrant
    workspace_scope: ScopeGrant
    policy_scope: ScopeGrant
    budget: RuntimeBudget
    policy_version: str = Field(min_length=1)
    workspace_version: str = Field(min_length=1)
    permission_version: str = Field(min_length=1)
    expires_in_seconds: int | None = Field(default=None, ge=1)
    view_mode: ContextViewMode = ContextViewMode.WORKING
    evidence_candidates: tuple[EvidenceCandidate, ...] = ()
    evidence_budget_tokens: int = Field(default=512, ge=0)
    memory_candidates: tuple[MemoryReference, ...] = ()
    memory_budget_tokens: int = Field(default=2048, ge=0)


class ContextDerivationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ContextDerivationStatus
    snapshot: ContextSnapshot | None = None
    reason: str | None = None
    dependency_result_refs: tuple[str, ...] = ()
    views: ContextViews | None = None


def _intersect_scope(*scopes: ScopeGrant) -> ScopeGrant:
    def intersect(
        values: tuple[str, ...], allowed: tuple[str, ...], *, directory: bool = False
    ) -> tuple[str, ...]:
        if directory:
            # Workspace grants are directories: use the narrower descendant
            # when a selected session path sits under a configured root.
            return _intersect_workspace_paths(values, allowed)
        allowed_set = set(allowed)
        return tuple(value for value in values if value in allowed_set)

    requested, *limits = scopes
    fields = ("workspace_paths", "source_ids", "account_ids", "allowed_packages", "allowed_tools")
    values = {}
    for field in fields:
        result = getattr(requested, field)
        if not result and limits and field in {"workspace_paths", "source_ids", "account_ids"}:
            result = getattr(limits[0], field)
        for limit in limits:
            result = intersect(
                result,
                getattr(limit, field),
                directory=field == "workspace_paths",
            )
        values[field] = result
    level = min(
        scopes,
        key=lambda scope: list(SideEffectLevel).index(scope.side_effect_level),
    ).side_effect_level
    return ScopeGrant(side_effect_level=level, **values)


def _intersect_workspace_paths(
    requested: tuple[str, ...], permitted: tuple[str, ...]
) -> tuple[str, ...]:
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


def _minimum_budget(plan_budget: RuntimeBudget | None, request_budget: RuntimeBudget) -> RuntimeBudget:
    if plan_budget is None:
        return request_budget

    def minimum(left: int | None, right: int | None) -> int | None:
        if left is None:
            return right
        if right is None:
            return left
        return min(left, right)

    return RuntimeBudget(
        max_tokens=minimum(plan_budget.max_tokens, request_budget.max_tokens),
        max_llm_calls=minimum(plan_budget.max_llm_calls, request_budget.max_llm_calls),
        max_tool_calls=minimum(plan_budget.max_tool_calls, request_budget.max_tool_calls),
        max_wall_time_seconds=minimum(
            plan_budget.max_wall_time_seconds, request_budget.max_wall_time_seconds
        ),
    )


def _scope_requested(scope: ScopeGrant) -> bool:
    return bool(
        scope.workspace_paths
        or scope.source_ids
        or scope.account_ids
        or scope.allowed_packages
        or scope.allowed_tools
        or scope.side_effect_level != SideEffectLevel.NONE
    )


def _scope_fully_denied(requested: ScopeGrant, effective: ScopeGrant) -> bool:
    for field in ("workspace_paths", "source_ids", "account_ids", "allowed_packages", "allowed_tools"):
        if getattr(requested, field) and not getattr(effective, field):
            return True
    return (
        requested.side_effect_level != SideEffectLevel.NONE
        and effective.side_effect_level == SideEffectLevel.NONE
    )


class ContextDriver:
    """Pure context assembler; it does not load data or execute tools."""

    async def derive(
        self,
        request: ContextRequest,
        *,
        evidence_resolver: Callable[[EvidenceRef], Awaitable[EvidenceCandidate | None]] | None = None,
    ) -> ContextDerivationResult:
        incomplete = [
            result.result_id
            for result in request.dependency_results
            if result.status != TaskResultStatus.COMPLETED
        ]
        dependency_refs = tuple(result.result_id for result in request.dependency_results)
        requested_memories = [value[7:] for value in request.plan_step.input_refs if value.startswith("memory:")]
        selected_memories: list[MemoryReference] = []
        child_budget = _minimum_budget(request.plan_step.budget, request.budget)
        memory_remaining = min(request.memory_budget_tokens, child_budget.max_tokens) if child_budget.max_tokens is not None else request.memory_budget_tokens
        for value in requested_memories:
            matches = [ref for ref in request.memory_candidates
                       if value in {ref.memory_id, f"{ref.memory_id}@{ref.version}"}]
            if len(matches) != 1:
                return ContextDerivationResult(status=ContextDerivationStatus.SCOPE_DENIED,
                                               reason="Explicit memory reference is missing, stale or unauthorized.")
            ref = matches[0]
            if ref in selected_memories:
                continue
            # Provenance and version metadata consume context too. Counting only
            # content lets repeated-source IDs bypass this child partition.
            cost = len(ref.model_dump_json().encode("utf-8"))
            if cost > memory_remaining:
                return ContextDerivationResult(status=ContextDerivationStatus.BUDGET_EXCEEDED,
                                               reason="Explicit memory references exceed the child memory budget.")
            selected_memories.append(ref)
            memory_remaining -= cost
        if incomplete:
            return ContextDerivationResult(
                status=ContextDerivationStatus.BLOCKED_DEPENDENCY,
                reason="Dependencies are not completed: " + ", ".join(incomplete),
                dependency_result_refs=dependency_refs,
            )

        requested_profile_id = request.plan_step.inference_profile_id
        resolved_profile = request.inference_profile
        if (
            (requested_profile_id is None and resolved_profile is not None)
            or (
                requested_profile_id is not None
                and (
                    resolved_profile is None
                    or resolved_profile.profile_id != requested_profile_id
                )
            )
        ):
            return ContextDerivationResult(
                status=ContextDerivationStatus.SCOPE_DENIED,
                reason="Plan step inference profile does not match server configuration.",
                dependency_result_refs=dependency_refs,
            )
        if resolved_profile is not None:
            frozen_values = (
                request.plan_step.inference_client_name,
                request.plan_step.inference_model,
                request.plan_step.inference_reasoning_effort,
            )
            has_frozen_values = any(value is not None for value in frozen_values)
            configured_values = (
                resolved_profile.client_name,
                resolved_profile.model,
                resolved_profile.reasoning_effort,
            )
            if has_frozen_values and (
                request.plan_step.inference_client_name is None
                or request.plan_step.inference_model is None
                or frozen_values != configured_values
            ):
                return ContextDerivationResult(
                    status=ContextDerivationStatus.SCOPE_DENIED,
                    reason="Configured inference profile changed after the PlanStep was frozen.",
                    dependency_result_refs=dependency_refs,
                )

        requested_scope = request.plan_step.effective_scope or ScopeGrant(
            allowed_packages=request.plan_step.allowed_packages,
            allowed_tools=request.plan_step.allowed_tools,
            side_effect_level=request.plan_step.side_effect_level,
        )
        effective_scope = _intersect_scope(
            requested_scope,
            request.parent_effective_scope,
            request.session_scope,
            request.workspace_scope,
            request.policy_scope,
        )
        # Full authority is relative to server-supplied current session/workspace
        # ceilings, never to the parent grant: descendants of a narrowed child
        # must not regain the parent's wider/global authority marker.
        trusted_ceiling = _intersect_scope(
            request.session_scope, request.workspace_scope, request.policy_scope
        )
        full_workspace_authority = (
            bool(trusted_ceiling.workspace_paths)
            and effective_scope.workspace_paths == trusted_ceiling.workspace_paths
        )
        full_data_authority = (
            effective_scope.source_ids == trusted_ceiling.source_ids
            and effective_scope.account_ids == trusted_ceiling.account_ids
        )
        if _scope_requested(requested_scope) and _scope_fully_denied(
            requested_scope, effective_scope
        ):
            return ContextDerivationResult(
                status=ContextDerivationStatus.SCOPE_DENIED,
                reason="Plan step scope is outside the effective access scope.",
                dependency_result_refs=dependency_refs,
            )

        budget = _minimum_budget(request.plan_step.budget, request.budget)
        if any(value == 0 for value in budget.model_dump().values() if value is not None):
            return ContextDerivationResult(
                status=ContextDerivationStatus.BUDGET_EXCEEDED,
                reason="Effective context budget is exhausted.",
                dependency_result_refs=dependency_refs,
            )
        eligible = [
            candidate
            for candidate in request.evidence_candidates
            if (candidate.evidence.source_id or candidate.evidence.source_ref)
            in effective_scope.source_ids
        ]
        eligible = [
            candidate
            for candidate in eligible
            if candidate.evidence.account_ref is None
            or candidate.evidence.account_ref in effective_scope.account_ids
        ]
        # Pack references first, accounting for their short summaries. The
        # estimate is intentionally conservative and independent of a tokenizer.
        warnings: list[str] = []
        remaining = request.evidence_budget_tokens
        selected: list[EvidenceCandidate] = []
        for candidate in eligible:
            summary = candidate.summary[:400]
            cost = max(1, (len(summary) + 3) // 4) + 12
            if cost > remaining:
                continue
            selected.append(candidate.model_copy(update={"summary": summary}))
            remaining -= cost

        evidence_summaries = tuple(candidate.summary for candidate in selected)
        evidence_content: tuple[str, ...] = ()
        if request.view_mode != ContextViewMode.REFERENCE:
            content: list[str] = []
            for candidate in selected:
                value = candidate.excerpt
                if request.view_mode == ContextViewMode.FULL:
                    value = candidate.full_content or candidate.excerpt
                if not value and evidence_resolver is not None:
                    try:
                        resolved = await evidence_resolver(candidate.evidence)
                    except Exception:  # noqa: BLE001 - resolver failures degrade to references.
                        resolved = None
                    if resolved is not None and resolved.evidence == candidate.evidence:
                        value = resolved.excerpt
                snippet_limit = min(1200, remaining * 4)
                if len(value) > snippet_limit:
                    value = value[:snippet_limit]
                    warnings.append(f"Evidence {candidate.evidence.evidence_id} was truncated.")
                if value:
                    content.append(value)
                    remaining -= (len(value) + 3) // 4
            evidence_content = tuple(content)
        evidence_refs = tuple(candidate.evidence.evidence_id for candidate in selected)
        untrusted_refs = tuple(
            candidate.evidence.evidence_id
            for candidate in selected
            if candidate.evidence.untrusted_data
        )
        expiries = []
        if request.expires_in_seconds is not None:
            expiries.append(datetime.now(UTC) + timedelta(seconds=request.expires_in_seconds))
        if budget.max_wall_time_seconds is not None:
            expiries.append(datetime.now(UTC) + timedelta(seconds=budget.max_wall_time_seconds))
        expires_at = min(expiries) if expiries else None
        if (
            request.request_llm_client_name is not None
            or request.request_llm_model is not None
        ):
            inference_profile_id = None
            inference_client_name = request.request_llm_client_name
            inference_model = request.request_llm_model
            inference_reasoning_effort = None
            inference_selection_source = "request"
        elif resolved_profile is not None:
            inference_profile_id = resolved_profile.profile_id
            inference_client_name = resolved_profile.client_name
            inference_model = resolved_profile.model
            inference_reasoning_effort = resolved_profile.reasoning_effort
            inference_selection_source = "server_profile"
        else:
            inference_profile_id = None
            inference_client_name = None
            inference_model = None
            inference_reasoning_effort = None
            inference_selection_source = "default"
        snapshot = ContextSnapshot(
            correlation_id=request.plan_step.correlation_id,
            snapshot_id=request.snapshot_id,
            parent_snapshot_id=request.parent_snapshot_id,
            parent_run_id=request.parent_run_id,
            child_run_id=request.child_run_id,
            session_id=request.session_id,
            plan_id=request.plan_id,
            step_id=request.plan_step.step_id,
            agent_kind=request.plan_step.agent_kind,
            agent_id=request.plan_step.agent_id,
            agent_version=request.plan_step.agent_version,
            inference_profile_id=inference_profile_id,
            inference_client_name=inference_client_name,
            inference_model=inference_model,
            inference_reasoning_effort=inference_reasoning_effort,
            inference_selection_source=inference_selection_source,
            objective=request.plan_step.objective,
            output_contract=request.plan_step.output_contract,
            input_refs=request.plan_step.input_refs,
            memory_refs=tuple(selected_memories),
            dependency_result_refs=dependency_refs,
            evidence_refs=tuple(candidate.evidence for candidate in selected),
            effective_scope=effective_scope,
            full_workspace_authority=full_workspace_authority,
            full_data_authority=full_data_authority,
            verification_criteria=request.plan_step.verification_criteria,
            budget=budget,
            policy_version=request.policy_version,
            workspace_version=request.workspace_version,
            permission_version=request.permission_version,
            expires_at=expires_at,
        )
        views = ContextViews(
            agent=AgentView(
                objective=request.plan_step.objective,
                output_contract=request.plan_step.output_contract,
                degraded_dependency_notes=request.plan_step.degraded_dependency_notes,
                verification_criteria=request.plan_step.verification_criteria,
                dependency_result_refs=dependency_refs,
                dependency_results=tuple(
                    DependencyResultView(
                        result_id=result.result_id,
                        step_id=result.step_id,
                        summary=result.summary,
                        artifact_refs=result.artifact_refs,
                        evidence_refs=tuple(
                            ref.evidence_id for ref in result.evidence_refs
                        ),
                        verification_summary=(
                            result.verification.summary if result.verification else None
                        ),
                        missing_requirements=result.missing_requirements,
                        warnings=result.warnings,
                    )
                    for result in request.dependency_results
                ),
                evidence_refs=evidence_refs,
                evidence_summaries=evidence_summaries,
                evidence_content=evidence_content,
                memory_refs=tuple(selected_memories),
                mode=request.view_mode,
                compression_warnings=tuple(warnings),
            ),
            tool=ToolView(
                snapshot_id=snapshot.snapshot_id,
                child_run_id=request.child_run_id,
                max_tool_calls=budget.max_tool_calls,
                allowed_packages=effective_scope.allowed_packages,
                allowed_tools=effective_scope.allowed_tools,
                allowed_paths=effective_scope.workspace_paths,
                allowed_source_ids=effective_scope.source_ids,
                allowed_account_ids=effective_scope.account_ids,
                memory_refs=tuple(selected_memories),
                full_workspace_authority=snapshot.full_workspace_authority,
                full_data_authority=snapshot.full_data_authority,
                side_effect_level=effective_scope.side_effect_level,
                expires_at=snapshot.expires_at,
            ),
            planner=PlannerView(
                plan_id=request.plan_id,
                step_id=request.plan_step.step_id,
                agent_kind=request.plan_step.agent_kind,
                can_fork=(
                    request.plan_step.agent_kind == ForkCallerKind.COORDINATOR
                ),
                dependency_result_refs=dependency_refs,
            ),
            audit=AuditView(
                snapshot_id=snapshot.snapshot_id,
                child_run_id=request.child_run_id,
                session_id=request.session_id,
                policy_version=request.policy_version,
                workspace_version=request.workspace_version,
                permission_version=request.permission_version,
                evidence_refs=evidence_refs,
                untrusted_evidence_refs=untrusted_refs,
                compression_warnings=tuple(warnings),
            ),
        )
        return ContextDerivationResult(
            status=ContextDerivationStatus.READY,
            snapshot=snapshot,
            dependency_result_refs=dependency_refs,
            views=views,
        )


def snapshot_is_stale(
    snapshot: ContextSnapshot,
    *,
    now: datetime | None = None,
    session_id: str | None = None,
    policy_version: str | None = None,
    workspace_version: str | None = None,
    permission_version: str | None = None,
) -> bool:
    """Return expiry state without mutating the immutable Snapshot."""

    return snapshot.stale or (
        session_id is not None and session_id != snapshot.session_id
    ) or (
        policy_version is not None and policy_version != snapshot.policy_version
    ) or (
        workspace_version is not None and workspace_version != snapshot.workspace_version
    ) or (
        permission_version is not None and permission_version != snapshot.permission_version
    ) or (
        snapshot.expires_at is not None
        and snapshot.expires_at <= (now or datetime.now(UTC))
    )


def mark_snapshot_stale(snapshot: ContextSnapshot) -> ContextSnapshot:
    """Return a new immutable stale marker when a bound version is invalidated."""

    return snapshot.model_copy(update={"stale": True})
