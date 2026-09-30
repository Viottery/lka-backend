from __future__ import annotations

from app.core.multi_agent import (
    EvidenceRef,
    FailureDetail,
    ScopeGrant,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
)
from app.core.multi_agent_fast_path import (
    FastPathCapabilityBinding,
    FastPathDisposition,
    FastPathEventType,
    FastPathPolicy,
    FastPathRequest,
    FastPathStepKind,
    FastPathTemplateKind,
    assess_fast_path,
    assess_fast_path_completion,
    project_fast_path_metrics,
    standard_fast_path_templates,
)
from app.core.tools import ToolInvocation, ToolPackageSpec, ToolRegistry, ToolResult, ToolSpec


class _NoExecuteTool:
    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec

    def invoke(self, *, invocation: ToolInvocation, context) -> ToolResult:
        raise AssertionError("Fast-path strategy must never execute tools.")


def _registry(*, writable: bool = False) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="records", description="registered test tools"))
    registry.register_package(
        ToolPackageSpec(name="workspace", description="registered test tools")
    )
    registry.register_tool(
        _NoExecuteTool(
            ToolSpec(
                name="records.find",
                package="records",
                type="local_tool",
                description="find records",
                read_only=not writable,
                scope_uses_sources=True,
                scope_filtering_required=True,
            )
        )
    )
    registry.register_tool(
        _NoExecuteTool(
            ToolSpec(
                name="workspace.inspect",
                package="workspace",
                type="local_tool",
                description="inspect target",
                read_only=True,
                scope_path_fields=("path",),
                scope_uses_workspace=True,
            )
        )
    )
    registry.register_tool(
        _NoExecuteTool(
            ToolSpec(
                name="workspace.verify",
                package="workspace",
                type="local_tool",
                description="verify target",
                read_only=True,
                scope_path_fields=("path",),
                scope_uses_workspace=True,
            )
        )
    )
    return registry


def _templates():
    return standard_fast_path_templates(
        retrieval_capability_id="evidence",
        inspection_capability_id="inspection",
        verification_capability_id="verification",
    )


def _policy() -> FastPathPolicy:
    templates = _templates()
    return FastPathPolicy(
        correlation_id="fast_path_policy",
        enabled=True,
        allowed_templates=tuple(template.template_id for template in templates),
        templates=templates,
        capabilities=(
            FastPathCapabilityBinding(
                correlation_id="binding:evidence",
                capability_id="evidence",
                purpose=FastPathStepKind.RETRIEVAL,
                tool_names=("records.find",),
            ),
            FastPathCapabilityBinding(
                correlation_id="binding:inspection",
                capability_id="inspection",
                purpose=FastPathStepKind.INSPECTION,
                tool_names=("workspace.inspect",),
            ),
            FastPathCapabilityBinding(
                correlation_id="binding:verification",
                capability_id="verification",
                purpose=FastPathStepKind.VERIFICATION,
                tool_names=("workspace.verify",),
            ),
        ),
    )


def _request(template: FastPathTemplateKind, **updates) -> FastPathRequest:
    values = {
        "correlation_id": "fast_path_request",
        "run_id": "run_1",
        "session_id": "session_1",
        "user_input": "Find and verify the relevant record.",
        "objective": "Find and verify the relevant record.",
        "requested_template": template,
        "authorized_scope": ScopeGrant(
            workspace_paths=("/workspace",),
            source_ids=("source_1",),
            allowed_packages=("records", "workspace"),
            allowed_tools=("records.find", "workspace.inspect", "workspace.verify"),
            side_effect_level=SideEffectLevel.EXTERNAL,
        ),
        "evidence_refs": ("context:evidence:1",),
        "context_refs": ("context:window:1",),
        "context_payload": {"cached_tool_observations": [{"tool_name": "records.find"}]},
        "context_sufficient": True,
        "react_agent_available": True,
        "context_answer_available": True,
    }
    values.update(updates)
    return FastPathRequest(**values)


def _result(plan_id: str, step_id: str, status: TaskResultStatus, **updates) -> TaskResult:
    values = {
        "correlation_id": "fast_path_request",
        "result_id": f"result:{step_id}",
        "child_run_id": f"child:{step_id}",
        "plan_id": plan_id,
        "step_id": step_id,
        "snapshot_id": f"snapshot:{step_id}",
        "status": status,
        "summary": "step outcome",
    }
    if status in {TaskResultStatus.FAILED, TaskResultStatus.TIMED_OUT, TaskResultStatus.BLOCKED}:
        values["failure"] = FailureDetail(category="test", code="failed", message="step failed")
    values.update(updates)
    return TaskResult(**values)


def test_valid_registered_capabilities_construct_generic_dag_without_executing_tools() -> None:
    request = _request(FastPathTemplateKind.INSPECT_THEN_VERIFY)
    decision = assess_fast_path(request, _policy(), _registry())
    assert decision.disposition == FastPathDisposition.MATCHED
    assert decision.plan is not None
    assert [step.role for step in decision.plan.steps] == ["inspection", "verification"]
    assert decision.plan.steps[1].depends_on == (decision.plan.steps[0].step_id,)
    assert decision.plan.steps[0].allowed_tools == ("workspace.inspect",)
    assert all(step.effective_scope is not None for step in decision.plan.steps)


def test_templates_cover_single_agent_context_retrieval_and_retrieve_answer() -> None:
    registry = _registry()
    policy = _policy()
    for kind in (
        FastPathTemplateKind.SINGLE_AGENT,
        FastPathTemplateKind.CONTEXT_ANSWER,
        FastPathTemplateKind.RESTRICTED_RETRIEVAL,
        FastPathTemplateKind.RETRIEVE_THEN_ANSWER,
    ):
        decision = assess_fast_path(_request(kind), policy, registry)
        assert decision.disposition == FastPathDisposition.MATCHED, kind
        assert decision.plan is not None
    retrieve_answer = assess_fast_path(
        _request(FastPathTemplateKind.RETRIEVE_THEN_ANSWER), policy, registry
    ).plan
    assert retrieve_answer is not None
    assert retrieve_answer.steps[1].depends_on == (retrieve_answer.steps[0].step_id,)


def test_context_insufficiency_upgrades_losslessly_to_regular_agent_path() -> None:
    request = _request(
        FastPathTemplateKind.CONTEXT_ANSWER,
        context_sufficient=False,
        context_payload={"recent_messages": [{"role": "user", "content": "verbatim"}]},
    )
    decision = assess_fast_path(request, _policy(), _registry())
    assert decision.disposition == FastPathDisposition.ESCALATE
    assert decision.reason_code == "context_insufficient"
    assert decision.escalation is not None
    assert decision.escalation.original_user_input == request.user_input
    assert decision.escalation.preserved_context == request.context_payload
    assert decision.escalation.context_refs == request.context_refs


def test_missing_or_unregistered_capability_escalates_without_guessing() -> None:
    request = _request(FastPathTemplateKind.RESTRICTED_RETRIEVAL)
    policy = _policy()
    missing = policy.model_copy(update={"capabilities": ()})
    decision = assess_fast_path(request, missing, _registry())
    assert decision.disposition == FastPathDisposition.ESCALATE
    assert decision.reason_code == "capability_binding_missing"
    absent = assess_fast_path(request, policy, ToolRegistry())
    assert absent.disposition == FastPathDisposition.ESCALATE
    assert absent.reason_code == "capability_tool_not_registered"


def test_fast_path_rejects_write_or_unscoped_capability() -> None:
    request = _request(FastPathTemplateKind.RESTRICTED_RETRIEVAL)
    writable = assess_fast_path(request, _policy(), _registry(writable=True))
    assert writable.disposition == FastPathDisposition.ESCALATE
    assert writable.reason_code == "fast_path_capability_not_read_only"
    no_sources = request.model_copy(
        update={
            "authorized_scope": request.authorized_scope.model_copy(update={"source_ids": ()}),
        }
    )
    denied = assess_fast_path(no_sources, _policy(), _registry())
    assert denied.disposition == FastPathDisposition.ESCALATE
    assert denied.reason_code == "capability_scope_not_enforced"


def test_incomplete_fast_path_preserves_plan_results_and_emits_upgrade_event() -> None:
    request = _request(FastPathTemplateKind.RESTRICTED_RETRIEVAL)
    template = next(t for t in _templates() if t.template_id == request.requested_template)
    decision = assess_fast_path(request, _policy(), _registry())
    assert decision.plan is not None
    step_id = decision.plan.steps[0].step_id
    completed = assess_fast_path_completion(
        request=request,
        template=template,
        plan=decision.plan,
        task_results=(_result(decision.plan.plan_id, step_id, TaskResultStatus.COMPLETED),),
        evidence_refs=(),
    )
    assert not completed.completed
    assert completed.reason_code == "result_evidence_insufficient"
    assert completed.escalation is not None
    assert completed.escalation.partial_plan == decision.plan
    assert completed.escalation.task_results[0].step_id == step_id
    assert completed.event.event_type == FastPathEventType.UPGRADED


def test_failed_fast_path_completion_is_not_success_and_metrics_are_projected() -> None:
    request = _request(FastPathTemplateKind.RESTRICTED_RETRIEVAL)
    template = next(t for t in _templates() if t.template_id == request.requested_template)
    decision = assess_fast_path(request, _policy(), _registry())
    assert decision.plan is not None
    failed = assess_fast_path_completion(
        request=request,
        template=template,
        plan=decision.plan,
        task_results=(
            _result(decision.plan.plan_id, decision.plan.steps[0].step_id, TaskResultStatus.FAILED),
        ),
        evidence_refs=(),
    )
    assert not failed.completed
    assert failed.event.event_type == FastPathEventType.ERROR_COMPLETION
    metrics = project_fast_path_metrics((decision.event, failed.event))
    assert metrics.hits == 1
    assert metrics.error_completions == 1


def test_context_answer_completion_requires_a_real_answer() -> None:
    request = _request(FastPathTemplateKind.CONTEXT_ANSWER)
    template = next(t for t in _templates() if t.template_id == request.requested_template)
    decision = assess_fast_path(request, _policy(), _registry())
    assert decision.plan is not None
    missing = assess_fast_path_completion(
        request=request,
        template=template,
        plan=decision.plan,
        task_results=(),
        evidence_refs=(),
    )
    assert not missing.completed
    assert missing.reason_code == "fast_path_steps_incomplete"
    answered = assess_fast_path_completion(
        request=request,
        template=template,
        plan=decision.plan,
        task_results=(),
        evidence_refs=(),
        answer_text="Grounded answer.",
    )
    assert answered.completed


def test_inspect_then_verify_requires_an_explicit_verifier_outcome() -> None:
    request = _request(FastPathTemplateKind.INSPECT_THEN_VERIFY)
    template = next(t for t in _templates() if t.template_id == request.requested_template)
    decision = assess_fast_path(request, _policy(), _registry())
    assert decision.plan is not None
    results = tuple(
        _result(decision.plan.plan_id, step.step_id, TaskResultStatus.COMPLETED)
        for step in decision.plan.steps
    )
    inconclusive = assess_fast_path_completion(
        request=request,
        template=template,
        plan=decision.plan,
        task_results=results,
        evidence_refs=(),
    )
    assert not inconclusive.completed
    assert inconclusive.reason_code == "fast_path_verification_inconclusive"
    passed = assess_fast_path_completion(
        request=request,
        template=template,
        plan=decision.plan,
        task_results=results,
        evidence_refs=(),
        verification_passed=True,
    )
    assert passed.completed


def test_latest_retry_is_used_and_conflicting_same_attempt_upgrades() -> None:
    request = _request(FastPathTemplateKind.RESTRICTED_RETRIEVAL)
    template = next(t for t in _templates() if t.template_id == request.requested_template)
    decision = assess_fast_path(request, _policy(), _registry())
    assert decision.plan is not None
    step_id = decision.plan.steps[0].step_id
    first = _result(
        decision.plan.plan_id,
        step_id,
        TaskResultStatus.FAILED,
        attempt=1,
    )
    retry = _result(
        decision.plan.plan_id,
        step_id,
        TaskResultStatus.COMPLETED,
        attempt=2,
        result_id="retried",
        evidence_refs=(EvidenceRef(evidence_id="evidence:1", source_ref="source_1"),),
    )
    completed = assess_fast_path_completion(
        request=request,
        template=template,
        plan=decision.plan,
        task_results=(first, retry),
        evidence_refs=(),
    )
    assert completed.completed

    contradictory = retry.model_copy(update={"summary": "different latest claim"})
    conflict = assess_fast_path_completion(
        request=request,
        template=template,
        plan=decision.plan,
        task_results=(retry, contradictory),
        evidence_refs=(),
    )
    assert not conflict.completed
    assert conflict.reason_code == "fast_path_task_result_conflict"
