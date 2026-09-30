"""Pure, capability-checked fast-path policy and Plan template construction.

This module does not classify natural-language requests, invoke tools, or execute
Plans. A caller may propose a configured template; this module only accepts it
when server policy, current context, authorization scope, and Tool Registry
metadata prove that the proposed route is safe. Otherwise it returns a lossless
escalation payload for the ordinary ReAct/Planner path.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from hashlib import sha256
from typing import Any

from pydantic import Field, model_validator

from app.core.multi_agent import (
    MultiAgentModel,
    Plan,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResult,
)
from app.core.multi_agent_aggregation import AggregationStatus, aggregate_task_results
from app.core.tools import ToolRegistry, ToolSpec


class FastPathTemplateKind(str, Enum):
    SINGLE_AGENT = "single_agent"
    CONTEXT_ANSWER = "context_answer"
    RESTRICTED_RETRIEVAL = "restricted_retrieval"
    INSPECT_THEN_VERIFY = "inspect_then_verify"
    RETRIEVE_THEN_ANSWER = "retrieve_then_answer"


class FastPathStepKind(str, Enum):
    REACT_AGENT = "react_agent"
    CONTEXT_ANSWER = "context_answer"
    RETRIEVAL = "retrieval"
    INSPECTION = "inspection"
    VERIFICATION = "verification"


class FastPathDisposition(str, Enum):
    MATCHED = "matched"
    ESCALATE = "escalate"
    DISABLED = "disabled"


class FastPathEventType(str, Enum):
    HIT = "hit"
    COMPLETED = "completed"
    UPGRADED = "upgraded"
    ERROR_COMPLETION = "error_completion"
    BYPASSED = "bypassed"


class FastPathStepTemplate(MultiAgentModel):
    """One generic Plan step; capability IDs are server configuration, not names inferred from text."""

    step_key: str = Field(min_length=1, max_length=100)
    kind: FastPathStepKind
    capability_id: str | None = Field(default=None, max_length=200)
    depends_on: tuple[str, ...] = ()
    objective_template: str = Field(min_length=1)
    output_contract: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_capability_binding(self) -> FastPathStepTemplate:
        uses_capability = self.kind in {
            FastPathStepKind.RETRIEVAL,
            FastPathStepKind.INSPECTION,
            FastPathStepKind.VERIFICATION,
        }
        if uses_capability != (self.capability_id is not None):
            raise ValueError(
                "Retrieval/inspection/verification steps require exactly one capability binding."
            )
        if self.step_key in self.depends_on or len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError(
                "Fast-path step dependencies must be unique and cannot be self-references."
            )
        return self


class FastPathTemplate(MultiAgentModel):
    template_id: FastPathTemplateKind
    steps: tuple[FastPathStepTemplate, ...] = Field(min_length=1, max_length=4)
    requires_sufficient_context: bool = False
    minimum_evidence_refs: int = Field(default=0, ge=0)
    minimum_result_evidence_refs: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_shape(self) -> FastPathTemplate:
        by_key = {step.step_key: step for step in self.steps}
        if len(by_key) != len(self.steps):
            raise ValueError("Fast-path step keys must be unique.")
        for step in self.steps:
            if set(step.depends_on) - set(by_key):
                raise ValueError(f"Fast-path dependency for {step.step_key!r} is not declared.")
        expected = {
            FastPathTemplateKind.SINGLE_AGENT: (FastPathStepKind.REACT_AGENT,),
            FastPathTemplateKind.CONTEXT_ANSWER: (FastPathStepKind.CONTEXT_ANSWER,),
            FastPathTemplateKind.RESTRICTED_RETRIEVAL: (FastPathStepKind.RETRIEVAL,),
            FastPathTemplateKind.INSPECT_THEN_VERIFY: (
                FastPathStepKind.INSPECTION,
                FastPathStepKind.VERIFICATION,
            ),
            FastPathTemplateKind.RETRIEVE_THEN_ANSWER: (
                FastPathStepKind.RETRIEVAL,
                FastPathStepKind.CONTEXT_ANSWER,
            ),
        }[self.template_id]
        if tuple(step.kind for step in self.steps) != expected:
            raise ValueError(f"Template {self.template_id.value!r} has an invalid step shape.")
        if (
            self.template_id == FastPathTemplateKind.CONTEXT_ANSWER
            and not self.requires_sufficient_context
        ):
            raise ValueError("Context-answer template must require sufficient context.")
        if (
            self.template_id == FastPathTemplateKind.RETRIEVE_THEN_ANSWER
            and self.steps[0].step_key not in self.steps[1].depends_on
        ):
            raise ValueError("Answer step must depend on the retrieval step.")
        if (
            self.template_id == FastPathTemplateKind.INSPECT_THEN_VERIFY
            and self.steps[0].step_key not in self.steps[1].depends_on
        ):
            raise ValueError("Verification step must depend on the inspection step.")
        return self


class FastPathCapabilityBinding(MultiAgentModel):
    """Server-owned semantic label for an explicitly registered capability."""

    capability_id: str = Field(min_length=1, max_length=200)
    purpose: FastPathStepKind
    tool_names: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def only_supported_tool_capabilities(self) -> FastPathCapabilityBinding:
        if self.purpose not in {
            FastPathStepKind.RETRIEVAL,
            FastPathStepKind.INSPECTION,
            FastPathStepKind.VERIFICATION,
        }:
            raise ValueError(
                "A registry binding must declare retrieval, inspection, or verification purpose."
            )
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("Capability tool names must be unique.")
        return self


class FastPathPolicy(MultiAgentModel):
    """Server-owned configuration. Disabled by default; templates are opt-in."""

    enabled: bool = False
    allowed_templates: tuple[FastPathTemplateKind, ...] = ()
    templates: tuple[FastPathTemplate, ...] = ()
    capabilities: tuple[FastPathCapabilityBinding, ...] = ()
    max_plan_steps: int = Field(default=4, ge=1, le=4)

    @model_validator(mode="after")
    def validate_configuration(self) -> FastPathPolicy:
        template_ids = [template.template_id for template in self.templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("Fast-path template IDs must be unique.")
        if len(set(self.allowed_templates)) != len(self.allowed_templates):
            raise ValueError("Allowed fast-path template IDs must be unique.")
        if set(template_ids) - set(self.allowed_templates):
            raise ValueError("Configured templates must also be explicitly allowed.")
        capability_ids = [item.capability_id for item in self.capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("Fast-path capability IDs must be unique.")
        return self


class FastPathRequest(MultiAgentModel):
    """Unmodified request and server-resolved context/scope facts for assessment."""

    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    user_input: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    requested_template: FastPathTemplateKind | None = None
    authorized_scope: ScopeGrant
    budget: RuntimeBudget = Field(default_factory=RuntimeBudget)
    context_refs: tuple[str, ...] = ()
    context_payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    cached_observation_refs: tuple[str, ...] = ()
    context_sufficient: bool = False
    react_agent_available: bool = False
    context_answer_available: bool = False


class FastPathEvent(MultiAgentModel):
    event_type: FastPathEventType
    run_id: str = Field(min_length=1, max_length=200)
    template_id: FastPathTemplateKind | None = None
    reason_code: str | None = None


class FastPathEscalation(MultiAgentModel):
    """Lossless handoff payload for ordinary ReAct/Planner continuation."""

    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    original_user_input: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    reason_code: str = Field(min_length=1, max_length=100)
    context_refs: tuple[str, ...] = ()
    preserved_context: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    cached_observation_refs: tuple[str, ...] = ()
    partial_plan: Plan | None = None
    task_results: tuple[TaskResult, ...] = ()


class FastPathDecision(MultiAgentModel):
    disposition: FastPathDisposition
    template: FastPathTemplate | None = None
    plan: Plan | None = None
    escalation: FastPathEscalation | None = None
    event: FastPathEvent
    reason_code: str


class FastPathCompletion(MultiAgentModel):
    completed: bool
    escalation: FastPathEscalation | None = None
    event: FastPathEvent
    reason_code: str


class FastPathMetrics(MultiAgentModel):
    hits: int = Field(ge=0)
    completed: int = Field(ge=0)
    upgrades: int = Field(ge=0)
    error_completions: int = Field(ge=0)
    bypassed: int = Field(ge=0)


def standard_fast_path_templates(
    *,
    retrieval_capability_id: str,
    inspection_capability_id: str,
    verification_capability_id: str,
) -> tuple[FastPathTemplate, ...]:
    """Return generic recipes; capability IDs always come from server configuration."""

    def step(
        key: str,
        kind: FastPathStepKind,
        objective: str,
        contract: str,
        capability: str | None = None,
        dependencies: tuple[str, ...] = (),
    ) -> FastPathStepTemplate:
        return FastPathStepTemplate(
            correlation_id=f"fast-path-template:{key}",
            step_key=key,
            kind=kind,
            capability_id=capability,
            depends_on=dependencies,
            objective_template=objective,
            output_contract=contract,
        )

    return (
        FastPathTemplate(
            correlation_id="fast-path-template:single-agent",
            template_id=FastPathTemplateKind.SINGLE_AGENT,
            steps=(
                step(
                    "agent",
                    FastPathStepKind.REACT_AGENT,
                    "Complete the task: {objective}",
                    "A concise result satisfying the task.",
                ),
            ),
        ),
        FastPathTemplate(
            correlation_id="fast-path-template:context-answer",
            template_id=FastPathTemplateKind.CONTEXT_ANSWER,
            requires_sufficient_context=True,
            steps=(
                step(
                    "answer",
                    FastPathStepKind.CONTEXT_ANSWER,
                    "Answer from sufficient authorized context: {objective}",
                    "A grounded answer that identifies any remaining uncertainty.",
                ),
            ),
        ),
        FastPathTemplate(
            correlation_id="fast-path-template:restricted-retrieval",
            template_id=FastPathTemplateKind.RESTRICTED_RETRIEVAL,
            minimum_result_evidence_refs=1,
            steps=(
                step(
                    "retrieve",
                    FastPathStepKind.RETRIEVAL,
                    "Retrieve authorized evidence relevant to: {objective}",
                    "Bounded evidence references and a concise relevance summary.",
                    retrieval_capability_id,
                ),
            ),
        ),
        FastPathTemplate(
            correlation_id="fast-path-template:inspect-verify",
            template_id=FastPathTemplateKind.INSPECT_THEN_VERIFY,
            steps=(
                step(
                    "inspect",
                    FastPathStepKind.INSPECTION,
                    "Inspect the authorized target relevant to: {objective}",
                    "Observed state and evidence references.",
                    inspection_capability_id,
                ),
                step(
                    "verify",
                    FastPathStepKind.VERIFICATION,
                    "Verify the preceding observations for: {objective}",
                    "Verification outcome, evidence, and explicit unresolved checks.",
                    verification_capability_id,
                    ("inspect",),
                ),
            ),
        ),
        FastPathTemplate(
            correlation_id="fast-path-template:retrieve-answer",
            template_id=FastPathTemplateKind.RETRIEVE_THEN_ANSWER,
            minimum_result_evidence_refs=1,
            steps=(
                step(
                    "retrieve",
                    FastPathStepKind.RETRIEVAL,
                    "Retrieve authorized evidence relevant to: {objective}",
                    "Bounded evidence references and a concise relevance summary.",
                    retrieval_capability_id,
                ),
                step(
                    "answer",
                    FastPathStepKind.CONTEXT_ANSWER,
                    "Answer using the retrieved evidence for: {objective}",
                    "A grounded answer that identifies any remaining uncertainty.",
                    dependencies=("retrieve",),
                ),
            ),
        ),
    )


def assess_fast_path(
    request: FastPathRequest,
    policy: FastPathPolicy,
    registry: ToolRegistry,
) -> FastPathDecision:
    """Validate a proposed route and construct a safe, non-executable Plan."""

    requested = request.requested_template
    if not policy.enabled:
        return _escalate(request, "fast_path_disabled", disposition=FastPathDisposition.DISABLED)
    if requested is None:
        return _escalate(request, "no_template_proposed")
    if requested not in policy.allowed_templates:
        return _escalate(request, "template_not_allowed")
    template = next((item for item in policy.templates if item.template_id == requested), None)
    if template is None:
        return _escalate(request, "template_not_configured")
    if len(template.steps) > policy.max_plan_steps:
        return _escalate(request, "template_exceeds_step_limit", template=template)
    if template.requires_sufficient_context and not request.context_sufficient:
        return _escalate(request, "context_insufficient", template=template)
    if len(request.evidence_refs) < template.minimum_evidence_refs:
        return _escalate(request, "evidence_insufficient", template=template)

    bindings = {item.capability_id: item for item in policy.capabilities}
    specs = {spec.name: spec for spec in registry.list_tools()}
    resolved: dict[str, tuple[ToolSpec, ...]] = {}
    for step in template.steps:
        if step.kind == FastPathStepKind.REACT_AGENT and not request.react_agent_available:
            return _escalate(request, "react_agent_unavailable", template=template)
        if step.kind == FastPathStepKind.CONTEXT_ANSWER and not request.context_answer_available:
            return _escalate(request, "context_answer_unavailable", template=template)
        if step.capability_id is None:
            continue
        binding = bindings.get(step.capability_id)
        if binding is None or binding.purpose != step.kind:
            return _escalate(request, "capability_binding_missing", template=template)
        selected: list[ToolSpec] = []
        for tool_name in binding.tool_names:
            spec = specs.get(tool_name)
            if spec is None:
                return _escalate(request, "capability_tool_not_registered", template=template)
            if tool_name not in request.authorized_scope.allowed_tools:
                return _escalate(request, "capability_outside_authorized_tools", template=template)
            if spec.package not in request.authorized_scope.allowed_packages:
                return _escalate(
                    request, "capability_outside_authorized_packages", template=template
                )
            if spec.read_only is not True:
                return _escalate(request, "fast_path_capability_not_read_only", template=template)
            if request.authorized_scope.side_effect_level == SideEffectLevel.NONE:
                return _escalate(
                    request,
                    "read_capability_outside_authorized_side_effect_scope",
                    template=template,
                )
            if not _scope_enforcement_proven(spec, request.authorized_scope):
                return _escalate(request, "capability_scope_not_enforced", template=template)
            selected.append(spec)
        resolved[step.capability_id] = tuple(selected)

    try:
        plan = _build_plan(request=request, template=template, registry=registry, resolved=resolved)
    except (ValueError, KeyError):
        return _escalate(request, "template_plan_invalid", template=template)
    event = FastPathEvent(
        correlation_id=request.correlation_id,
        event_type=FastPathEventType.HIT,
        run_id=request.run_id,
        template_id=requested,
    )
    return FastPathDecision(
        correlation_id=request.correlation_id,
        disposition=FastPathDisposition.MATCHED,
        template=template,
        plan=plan,
        event=event,
        reason_code="template_validated",
    )


def assess_fast_path_completion(
    *,
    request: FastPathRequest,
    template: FastPathTemplate,
    plan: Plan,
    task_results: Iterable[TaskResult],
    evidence_refs: Iterable[str],
    answer_text: str | None = None,
    verification_passed: bool | None = None,
) -> FastPathCompletion:
    """Complete only when all tool-backed steps succeeded and evidence is sufficient."""

    results = tuple(task_results)
    expected = {
        step.step_id
        for step, spec in zip(plan.steps, template.steps, strict=True)
        if spec.capability_id is not None
    }
    aggregate = aggregate_task_results(plan, results, expected_step_ids=expected)
    result_refs = tuple(
        evidence.evidence_id
        for result in aggregate.task_results
        for evidence in result.evidence_refs
    )
    evidence = tuple(dict.fromkeys((*evidence_refs, *result_refs)))
    failed = aggregate.status == AggregationStatus.FAILED
    conflicted = aggregate.status == AggregationStatus.CONFLICTING
    incomplete = aggregate.status != AggregationStatus.COMPLETE
    if any(
        step.kind in {FastPathStepKind.CONTEXT_ANSWER, FastPathStepKind.REACT_AGENT}
        for step in template.steps
    ):
        incomplete = incomplete or not (answer_text and answer_text.strip())
    requires_verification = any(
        step.kind == FastPathStepKind.VERIFICATION for step in template.steps
    )
    verification_failed = requires_verification and verification_passed is False
    verification_unknown = requires_verification and verification_passed is not True
    enough_evidence = len(evidence) >= template.minimum_result_evidence_refs
    reason = "completed"
    event_type = FastPathEventType.COMPLETED
    completed = True
    if failed:
        completed, reason, event_type = (
            False,
            "fast_path_step_failed",
            FastPathEventType.ERROR_COMPLETION,
        )
    elif conflicted:
        completed, reason, event_type = (
            False,
            "fast_path_task_result_conflict",
            FastPathEventType.UPGRADED,
        )
    elif verification_failed:
        completed, reason, event_type = (
            False,
            "fast_path_verification_failed",
            FastPathEventType.UPGRADED,
        )
    elif verification_unknown:
        completed, reason, event_type = (
            False,
            "fast_path_verification_inconclusive",
            FastPathEventType.UPGRADED,
        )
    elif incomplete:
        completed, reason, event_type = (
            False,
            "fast_path_steps_incomplete",
            FastPathEventType.UPGRADED,
        )
    elif not enough_evidence:
        completed, reason, event_type = (
            False,
            "result_evidence_insufficient",
            FastPathEventType.UPGRADED,
        )
    escalation = (
        None
        if completed
        else FastPathEscalation(
            correlation_id=request.correlation_id,
            run_id=request.run_id,
            session_id=request.session_id,
            original_user_input=request.user_input,
            objective=request.objective,
            reason_code=reason,
            context_refs=request.context_refs,
            preserved_context=request.context_payload,
            evidence_refs=tuple(dict.fromkeys((*request.evidence_refs, *evidence))),
            cached_observation_refs=request.cached_observation_refs,
            partial_plan=plan,
            task_results=results,
        )
    )
    event = FastPathEvent(
        correlation_id=request.correlation_id,
        event_type=event_type,
        run_id=request.run_id,
        template_id=template.template_id,
        reason_code=None if completed else reason,
    )
    return FastPathCompletion(
        correlation_id=request.correlation_id,
        completed=completed,
        escalation=escalation,
        event=event,
        reason_code=reason,
    )


def project_fast_path_metrics(events: Iterable[FastPathEvent]) -> FastPathMetrics:
    """Build lightweight counters from durable/projected fast-path events."""

    hits = completed = upgrades = errors = bypassed = 0
    for event in events:
        if event.event_type == FastPathEventType.HIT:
            hits += 1
        elif event.event_type == FastPathEventType.COMPLETED:
            completed += 1
        elif event.event_type == FastPathEventType.UPGRADED:
            upgrades += 1
        elif event.event_type == FastPathEventType.ERROR_COMPLETION:
            errors += 1
        elif event.event_type == FastPathEventType.BYPASSED:
            bypassed += 1
    return FastPathMetrics(
        correlation_id="fast-path-metrics",
        hits=hits,
        completed=completed,
        upgrades=upgrades,
        error_completions=errors,
        bypassed=bypassed,
    )


def _scope_enforcement_proven(spec: ToolSpec, scope: ScopeGrant) -> bool:
    return not (
        spec.scope_uses_sources
        and (
            not scope.source_ids or not (spec.scope_source_fields or spec.scope_filtering_required)
        )
        or spec.scope_uses_accounts
        and (
            not scope.account_ids
            or not (spec.scope_account_fields or spec.scope_filtering_required)
        )
        or spec.scope_uses_workspace
        and (
            not scope.workspace_paths
            or not (spec.scope_path_fields or spec.scope_filtering_required)
        )
    )


def _build_plan(
    *,
    request: FastPathRequest,
    template: FastPathTemplate,
    registry: ToolRegistry,
    resolved: dict[str, tuple[ToolSpec, ...]],
) -> Plan:
    plan_id = (
        "fastpath_"
        + sha256(
            f"{request.run_id}\0{template.template_id.value}\0{request.objective}".encode()
        ).hexdigest()[:20]
    )
    registered = {spec.name: spec for spec in registry.list_tools()}
    plan_steps: list[PlanStep] = []
    step_id_by_key = {
        step.step_key: f"{template.template_id.value}:{step.step_key}" for step in template.steps
    }
    for step in template.steps:
        if step.capability_id is not None:
            specs = resolved[step.capability_id]
            tool_names = tuple(spec.name for spec in specs)
            packages = tuple(
                dict.fromkeys(spec.package for spec in specs if spec.package is not None)
            )
            step_scope = request.authorized_scope.model_copy(
                update={
                    "allowed_packages": packages,
                    "allowed_tools": tool_names,
                    "side_effect_level": SideEffectLevel.READ,
                }
            )
            side_effect = SideEffectLevel.READ
        elif step.kind == FastPathStepKind.REACT_AGENT:
            tool_names = tuple(
                name for name in request.authorized_scope.allowed_tools if name in registered
            )
            packages = tuple(
                dict.fromkeys(
                    registered[name].package
                    for name in tool_names
                    if registered[name].package in request.authorized_scope.allowed_packages
                )
            )
            step_scope = request.authorized_scope.model_copy(
                update={"allowed_packages": packages, "allowed_tools": tool_names}
            )
            side_effect = step_scope.side_effect_level
        else:
            tool_names = ()
            packages = ()
            step_scope = request.authorized_scope.model_copy(
                update={
                    "allowed_packages": (),
                    "allowed_tools": (),
                    "side_effect_level": SideEffectLevel.NONE,
                }
            )
            side_effect = SideEffectLevel.NONE
        plan_steps.append(
            PlanStep(
                correlation_id=request.correlation_id,
                step_id=step_id_by_key[step.step_key],
                objective=step.objective_template.replace("{objective}", request.objective),
                role=step.kind.value,
                depends_on=tuple(step_id_by_key[key] for key in step.depends_on),
                allowed_packages=packages,
                allowed_tools=tool_names,
                effective_scope=step_scope,
                output_contract=step.output_contract,
                budget=request.budget,
                side_effect_level=side_effect,
                status=PlanStepStatus.PENDING,
            )
        )
    return Plan(
        correlation_id=request.correlation_id,
        plan_id=plan_id,
        parent_run_id=request.run_id,
        session_id=request.session_id,
        objective=request.objective,
        steps=tuple(plan_steps),
        status=PlanStatus.VALIDATED,
    )


def _escalate(
    request: FastPathRequest,
    reason_code: str,
    *,
    template: FastPathTemplate | None = None,
    disposition: FastPathDisposition = FastPathDisposition.ESCALATE,
) -> FastPathDecision:
    event = FastPathEvent(
        correlation_id=request.correlation_id,
        event_type=(
            FastPathEventType.BYPASSED
            if disposition == FastPathDisposition.DISABLED
            else FastPathEventType.UPGRADED
        ),
        run_id=request.run_id,
        template_id=template.template_id if template else request.requested_template,
        reason_code=reason_code,
    )
    escalation = FastPathEscalation(
        correlation_id=request.correlation_id,
        run_id=request.run_id,
        session_id=request.session_id,
        original_user_input=request.user_input,
        objective=request.objective,
        reason_code=reason_code,
        context_refs=request.context_refs,
        preserved_context=request.context_payload,
        evidence_refs=request.evidence_refs,
        cached_observation_refs=request.cached_observation_refs,
    )
    return FastPathDecision(
        correlation_id=request.correlation_id,
        disposition=disposition,
        template=template,
        escalation=escalation,
        event=event,
        reason_code=reason_code,
    )
