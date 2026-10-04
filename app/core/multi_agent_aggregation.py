"""Deterministic aggregation and verification for domain-neutral Agent results."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from enum import Enum
from hashlib import sha256
from pathlib import Path

from pydantic import Field

from app.core.agent_runs import AgentRunEvent, AgentRunRecord, AgentRunStatus
from app.core.multi_agent import (
    GENERAL_AGENT_ID,
    AggregationStatus,
    Artifact,
    EvidenceRef,
    MultiAgentModel,
    Plan,
    PlanStep,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)


class AggregationConflict(MultiAgentModel):
    conflict_id: str = Field(min_length=1)
    references: tuple[str, ...] = ()
    summary: str = Field(min_length=1)


class AggregatedTaskResults(MultiAgentModel):
    plan_id: str = Field(min_length=1)
    status: AggregationStatus
    expected_step_ids: tuple[str, ...] = ()
    completed_step_ids: tuple[str, ...] = ()
    partial_step_ids: tuple[str, ...] = ()
    blocked_step_ids: tuple[str, ...] = ()
    failed_step_ids: tuple[str, ...] = ()
    missing_step_ids: tuple[str, ...] = ()
    task_results: tuple[TaskResult, ...] = ()
    attempt_history: tuple[TaskResult, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    conflicts: tuple[AggregationConflict, ...] = ()
    warnings: tuple[str, ...] = ()


def _canonical(model: MultiAgentModel) -> str:
    return model.model_dump_json(exclude={"created_at", "completed_at"})


def aggregate_task_results(
    plan: Plan,
    task_results: Iterable[TaskResult],
    *,
    artifacts: Iterable[Artifact] = (),
    evidence_refs: Iterable[EvidenceRef] = (),
    expected_step_ids: Iterable[str] | None = None,
) -> AggregatedTaskResults:
    """Combine outputs without allowing missing, failed, or blocked work to pass."""

    expected = tuple(expected_step_ids) if expected_step_ids is not None else tuple(
        step.step_id for step in plan.steps if step.fork_operation_id is not None
    )
    if len(set(expected)) != len(expected) or set(expected) - {step.step_id for step in plan.steps}:
        raise ValueError("expected_step_ids must be unique IDs declared in the plan.")
    results = tuple(sorted(task_results, key=lambda item: (item.step_id, item.attempt, item.completed_at, item.result_id)))
    provided_artifacts = tuple(sorted(artifacts, key=lambda item: (item.artifact_id, item.producing_run_id)))
    provided_evidence = tuple(sorted(evidence_refs, key=lambda item: (item.evidence_id, item.source_ref)))
    expected_set = set(expected)
    relevant: dict[str, list[TaskResult]] = {step_id: [] for step_id in expected}
    conflicts: list[AggregationConflict] = []
    warnings: list[str] = []
    for result in results:
        if result.plan_id != plan.plan_id:
            conflicts.append(AggregationConflict(
                correlation_id=result.correlation_id,
                conflict_id=f"foreign-result:{result.result_id}",
                references=(result.result_id,),
                summary="Task result belongs to another plan.",
            ))
        elif result.step_id in expected_set:
            relevant[result.step_id].append(result)
        else:
            warnings.append(f"Ignored result for non-expected step {result.step_id}.")
    for step_id, candidates in relevant.items():
        if len(candidates) > 1:
            latest_attempt = max(candidate.attempt for candidate in candidates)
            latest = [candidate for candidate in candidates if candidate.attempt == latest_attempt]
            fingerprints = {_canonical(candidate) for candidate in latest}
            if len(fingerprints) > 1:
                conflicts.append(AggregationConflict(
                    correlation_id=plan.correlation_id,
                    conflict_id=f"duplicate-step:{step_id}",
                    references=tuple(candidate.result_id for candidate in latest),
                    summary=f"Conflicting latest results exist for step {step_id}.",
                ))
            else:
                warnings.append(f"Retained attempt {latest_attempt} for step {step_id}; {len(candidates) - len(latest)} earlier attempt(s) retained as history.")

    artifact_by_id: dict[str, Artifact] = {}
    for artifact in provided_artifacts:
        existing = artifact_by_id.get(artifact.artifact_id)
        if existing is not None and _canonical(existing) != _canonical(artifact):
            conflicts.append(AggregationConflict(
                correlation_id=plan.correlation_id,
                conflict_id=f"artifact:{artifact.artifact_id}",
                references=(existing.artifact_id, artifact.artifact_id),
                summary="Artifact ID refers to incompatible artifact records.",
            ))
        else:
            artifact_by_id[artifact.artifact_id] = artifact
    evidence_by_id: dict[str, EvidenceRef] = {}
    all_evidence = [*provided_evidence, *(ref for artifact in provided_artifacts for ref in artifact.evidence_refs), *(ref for result in results for ref in result.evidence_refs)]
    for evidence in all_evidence:
        existing = evidence_by_id.get(evidence.evidence_id)
        if existing is not None and existing != evidence:
            conflicts.append(AggregationConflict(
                correlation_id=plan.correlation_id,
                conflict_id=f"evidence:{evidence.evidence_id}",
                references=(existing.source_ref, evidence.source_ref),
                summary="Evidence ID refers to incompatible evidence records.",
            ))
        else:
            evidence_by_id[evidence.evidence_id] = evidence
    for result in results:
        for artifact_ref in result.artifact_refs:
            if artifact_ref not in artifact_by_id:
                warnings.append(f"Unresolved artifact reference {artifact_ref} from {result.result_id}.")

    completed, partial, blocked, failed = [], [], [], []
    authoritative: dict[str, TaskResult] = {}
    for step_id in expected:
        candidates = relevant[step_id]
        if not candidates:
            continue
        # Duplicate outcomes are represented as a conflict and are not accepted as success.
        latest_attempt = max(candidate.attempt for candidate in candidates)
        latest = [candidate for candidate in candidates if candidate.attempt == latest_attempt]
        if len({_canonical(candidate) for candidate in latest}) > 1:
            continue
        status = latest[0].status
        authoritative[step_id] = latest[0]
        if status == TaskResultStatus.COMPLETED:
            completed.append(step_id)
        elif status == TaskResultStatus.PARTIAL:
            partial.append(step_id)
        elif status == TaskResultStatus.BLOCKED:
            blocked.append(step_id)
        else:
            failed.append(step_id)
    missing = [step_id for step_id in expected if not relevant[step_id]]
    if conflicts:
        status = AggregationStatus.CONFLICTING
    elif failed:
        status = AggregationStatus.FAILED
    elif blocked:
        status = AggregationStatus.BLOCKED
    elif partial or missing or len(completed) != len(expected):
        status = AggregationStatus.PARTIAL
    else:
        status = AggregationStatus.COMPLETE
    return AggregatedTaskResults(
        correlation_id=plan.correlation_id,
        plan_id=plan.plan_id,
        status=status,
        expected_step_ids=expected,
        completed_step_ids=tuple(completed),
        partial_step_ids=tuple(partial),
        blocked_step_ids=tuple(blocked),
        failed_step_ids=tuple(failed),
        missing_step_ids=tuple(missing),
        task_results=tuple(authoritative[step_id] for step_id in expected if step_id in authoritative),
        attempt_history=tuple(result for step_id in expected for result in relevant[step_id]),
        artifacts=tuple(artifact_by_id.values()),
        evidence_refs=tuple(evidence_by_id.values()),
        conflicts=tuple(conflicts),
        warnings=tuple(dict.fromkeys(warnings)),
    )


class ConfirmationState(str, Enum):
    NOT_REQUIRED = "not_required"
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    MISSING = "missing"


class VerificationPolicy(MultiAgentModel):
    require_evidence: bool = False
    required_artifact_kinds: tuple[str, ...] = ()
    require_side_effect_confirmation: bool = True
    require_side_effect_audit: bool = False


def derive_child_execution_evidence(
    run: AgentRunRecord,
    events: Iterable[AgentRunEvent],
    *,
    tool_audit: dict[str, object] | None = None,
) -> tuple[bool | None, ConfirmationState]:
    """Derive side-effect evidence from a complete, durable child-run event log.

    ``False`` is returned only when the completed run's tool lifecycle events
    reconcile with its terminal summary and no tool execution could have effects.
    A server-proven pre-invocation rejection is not execution. Missing or
    inconsistent audit data stays unknown.
    """

    ordered = sorted(events, key=lambda event: event.sequence)
    if not ordered or [event.sequence for event in ordered] != list(range(1, len(ordered) + 1)):
        return None, ConfirmationState.MISSING
    if run.status == AgentRunStatus.FAILED:
        terminal_failures = [event for event in ordered if event.type == "subtask_failed"]
        attempted_tools = any(
            event.type in {
                "tool_started", "tool_completed", "safety_review_required",
                "safety_review_decided",
            }
            for event in ordered
        )
        if (
            run.metadata.get("agent_id") == GENERAL_AGENT_ID
            and run.metadata.get("executor_kind") == "react"
            and len(terminal_failures) == 1
            and not attempted_tools
            and ordered[-1] == terminal_failures[0]
        ):
            return False, ConfirmationState.NOT_REQUIRED
        return None, ConfirmationState.MISSING
    if run.status != AgentRunStatus.COMPLETED:
        return None, ConfirmationState.MISSING
    if isinstance(tool_audit, dict) and tool_audit.get("protocol_version") == "isolated_workspace_audit_v1":
        return _isolated_workspace_execution_evidence(run, ordered, tool_audit)
    if (
        not isinstance(tool_audit, dict)
        or tool_audit.get("protocol_version") != "child_tool_audit_v1"
        or tool_audit.get("complete") is not True
        or not isinstance(tool_audit.get("invocations"), list)
    ):
        return None, ConfirmationState.MISSING

    terminal = [event for event in ordered if event.type == "run_completed"]
    if len(terminal) != 1:
        return None, ConfirmationState.MISSING
    expected_count = terminal[0].payload.get("tool_event_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
        return None, ConfirmationState.MISSING

    started = [event for event in ordered if event.type == "tool_started"]
    completed = [event for event in ordered if event.type == "tool_completed"]
    if len(started) != len(completed):
        return None, ConfirmationState.MISSING
    started_values = [event.payload.get("tool_name") for event in started]
    completed_values = [event.payload.get("tool_name") for event in completed]
    if any(not isinstance(name, str) or not name for name in (*started_values, *completed_values)):
        return None, ConfirmationState.MISSING
    started_names = Counter(started_values)
    completed_names = Counter(completed_values)
    if started_names != completed_names:
        return None, ConfirmationState.MISSING

    reviews: dict[str, dict[str, object]] = {}
    for event in ordered:
        if event.type not in {"safety_review_required", "safety_review_decided"}:
            continue
        review = event.payload.get("review")
        if isinstance(review, dict) and isinstance(review.get("invocation_id"), str):
            reviews[str(review["invocation_id"])] = review

    completed_by_invocation: dict[str, dict[str, object]] = {}
    for event in completed:
        metadata = event.payload.get("metadata")
        result = metadata.get("result") if isinstance(metadata, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("invocation_id"), str):
            return None, ConfirmationState.MISSING
        completed_by_invocation[str(result["invocation_id"])] = result

    classifications: dict[str, dict[str, object]] = {}
    for item in tool_audit["invocations"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("invocation_id"), str)
            or not isinstance(item.get("tool_name"), str)
            or not isinstance(item.get("read_only"), bool)
            or not isinstance(item.get("status"), str)
            or (item.get("execution_started") is not None
                and not isinstance(item.get("execution_started"), bool))
        ):
            return None, ConfirmationState.MISSING
        invocation_id = str(item["invocation_id"])
        if invocation_id in classifications:
            return None, ConfirmationState.MISSING
        classifications[invocation_id] = item
    if (
        len(classifications) != expected_count
        or not set(completed_by_invocation).issubset(classifications)
        or not set(reviews).issubset(classifications)
    ):
        return None, ConfirmationState.MISSING
    for event in completed:
        metadata = event.payload.get("metadata")
        result = metadata.get("result") if isinstance(metadata, dict) else None
        if not isinstance(result, dict):
            return None, ConfirmationState.MISSING
        classification = classifications.get(str(result.get("invocation_id")))
        if (
            classification is None
            or classification.get("tool_name") != event.payload.get("tool_name")
            or classification.get("status") != result.get("status")
            or classification.get("execution_started") is not result.get("execution_started")
        ):
            return None, ConfirmationState.MISSING

    rejected_without_execution = sum(
        1 for invocation_id, review in reviews.items()
        if invocation_id not in completed_by_invocation
        and review.get("read_only") in {False, None}
        and review.get("status") == "rejected"
    )
    if expected_count != len(completed) + rejected_without_execution:
        return None, ConfirmationState.MISSING

    executed_non_readonly: list[str] = []
    for invocation_id, classification in classifications.items():
        review = reviews.get(invocation_id)
        executed = invocation_id in completed_by_invocation
        if classification.get("execution_started") is False:
            if not executed or classification.get("status") != "rejected":
                return None, ConfirmationState.MISSING
            continue
        if classification["read_only"] is True:
            if not executed or review is not None:
                return None, ConfirmationState.MISSING
            continue
        if review is None or review.get("read_only") is not False:
            return None, ConfirmationState.MISSING
        if not executed and review.get("status") == "rejected":
            if classification.get("status") != "rejected":
                return None, ConfirmationState.MISSING
            continue
        if review.get("status") != "approved" or not executed:
            return None, ConfirmationState.MISSING
        # Entering tool.invoke (or an unmarked legacy completion) cannot rule
        # out a partial effect even when the result is rejected or failed.
        executed_non_readonly.append(invocation_id)

    return bool(executed_non_readonly), (
        ConfirmationState.APPROVED
        if executed_non_readonly
        else ConfirmationState.NOT_REQUIRED
    )


def _isolated_workspace_execution_evidence(
    run: AgentRunRecord, events: list[AgentRunEvent], audit: dict[str, object],
) -> tuple[bool | None, ConfirmationState]:
    """Reconcile an external executor's staged manifest with its frozen scope.

    An executor without a staged workspace cannot claim a no-effect result through
    this protocol. Staged changes still need the ordinary approval/apply workflow.
    """
    unknown = (None, ConfirmationState.MISSING)
    snapshot = run.metadata.get("context_snapshot")
    scope = snapshot.get("effective_scope") if isinstance(snapshot, dict) else None
    result = run.result_snapshot
    count = audit.get("staged_change_count")
    workspaces = scope.get("workspace_paths") if isinstance(scope, dict) else None
    source = workspaces[0] if isinstance(workspaces, list) and len(workspaces) == 1 else None
    isolated = result.get("staged_workspace") if isinstance(result, dict) else None
    if (
        run.metadata.get("executor_kind") != "external_cli"
        or audit.get("complete") is not True or audit.get("source_applied") is not False
        or audit.get("external_effects_ruled_out") is not True
        or not isinstance(count, int) or isinstance(count, bool) or count < 0
        or not isinstance(source, str) or not isinstance(isolated, str)
        or audit.get("source_workspace_sha256") != sha256(source.encode()).hexdigest()
        or not isinstance(scope, dict)
        or not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"]
        or audit.get("snapshot_id") != snapshot.get("snapshot_id")
        or scope.get("side_effect_level") != "write"
        or any(scope.get(key) != [] for key in (
            "allowed_packages", "allowed_tools", "source_ids", "account_ids",
        ))
        or not isinstance(result, dict)
        or not isinstance(result.get("staged_change_count"), int)
        or result.get("staged_change_count") != count
        or isinstance(result.get("staged_change_count"), bool)
        or audit.get("isolated_workspace_sha256") != sha256(isolated.encode()).hexdigest()
    ):
        return unknown
    source_path, isolated_path = Path(source), Path(isolated)
    if (
        not source_path.is_absolute() or not isolated_path.is_absolute()
        or isolated_path.is_relative_to(source_path)
        or source_path.is_relative_to(isolated_path)
    ):
        return unknown
    terminals = [event for event in events if event.type == "subtask_completed"]
    audit_events = [event for event in events if event.type == "child_tool_audit"]
    if (
        len(terminals) != 1 or events[-1] != terminals[0]
        or len(audit_events) != 1 or audit_events[0].payload.get("audit") != audit
        or any(event.run_id != run.run_id for event in events)
        or any(
            event.type in {"tool_started", "tool_completed", "run_completed"}
            for event in events
        )
    ):
        return unknown
    return bool(count), (
        ConfirmationState.MISSING if count else ConfirmationState.NOT_REQUIRED
    )


def verify_task_result(
    step: PlanStep,
    result: TaskResult,
    *,
    artifacts: Iterable[Artifact] = (),
    evidence_refs: Iterable[EvidenceRef] = (),
    output_contract_satisfied: bool | None = None,
    confirmation: ConfirmationState = ConfirmationState.NOT_REQUIRED,
    actual_side_effects: bool | None = None,
    policy: VerificationPolicy | None = None,
) -> VerificationResult:
    """Check machine-verifiable contract signals and return explicit next actions.

    Natural-language contract compliance is intentionally not inferred here; callers
    must supply a verifier outcome or the check remains inconclusive.
    """

    policy = policy or VerificationPolicy(correlation_id=result.correlation_id)
    artifact_map = {artifact.artifact_id: artifact for artifact in artifacts}
    evidence_map = {ref.evidence_id: ref for ref in evidence_refs}
    evidence_map.update({ref.evidence_id: ref for artifact in artifact_map.values() for ref in artifact.evidence_refs})
    evidence_map.update({ref.evidence_id: ref for ref in result.evidence_refs})
    checks: list[VerificationCheck] = []
    missing: list[str] = []
    actions: list[str] = []
    if result.step_id != step.step_id:
        contract_state = VerificationStatus.FAILED
        contract_summary = "Task result does not match the requested plan step."
    elif output_contract_satisfied is True:
        contract_state, contract_summary = VerificationStatus.PASSED, "Output contract reported satisfied."
    elif output_contract_satisfied is False:
        contract_state, contract_summary = VerificationStatus.FAILED, "Output contract reported unsatisfied."
    else:
        contract_state, contract_summary = VerificationStatus.INCONCLUSIVE, "Output contract needs an explicit verifier decision."
        actions.append("verify_output_contract")
    checks.append(VerificationCheck(
        check_id="output_contract",
        status=contract_state,
        summary=contract_summary,
        required=True,
    ))
    unresolved_artifacts = [ref for ref in result.artifact_refs if ref not in artifact_map]
    if unresolved_artifacts:
        missing.extend(f"artifact:{ref}" for ref in unresolved_artifacts)
    missing_kinds = [kind for kind in policy.required_artifact_kinds if not any(a.kind == kind for a in artifact_map.values())]
    missing.extend(f"artifact_kind:{kind}" for kind in missing_kinds)
    artifact_ok = not unresolved_artifacts and not missing_kinds
    checks.append(VerificationCheck(check_id="artifacts", status=VerificationStatus.PASSED if artifact_ok else VerificationStatus.FAILED, summary="Required artifact references resolved." if artifact_ok else "Required artifacts are missing or unresolved.", required=bool(result.artifact_refs or policy.required_artifact_kinds)))
    if policy.require_evidence and not evidence_map:
        missing.append("evidence")
        evidence_ok = False
    else:
        evidence_ok = True
    checks.append(VerificationCheck(check_id="evidence", status=VerificationStatus.PASSED if evidence_ok else VerificationStatus.FAILED, summary="Evidence requirements satisfied." if evidence_ok else "Required evidence is missing.", required=policy.require_evidence))
    could_have_side_effects = step.side_effect_level in {SideEffectLevel.WRITE, SideEffectLevel.EXTERNAL}
    confirmation_required = policy.require_side_effect_confirmation and could_have_side_effects
    side_effect_required = confirmation_required and actual_side_effects is True
    if actual_side_effects is True and not could_have_side_effects:
        confirmation_state = VerificationStatus.FAILED
        confirmation_summary = "Non-read-only tool execution exceeded the step's declared side-effect scope."
        missing.append("actual_side_effect_scope_mismatch")
        actions.append("replan_or_abort")
    elif actual_side_effects is None and policy.require_side_effect_audit:
        confirmation_state = VerificationStatus.INCONCLUSIVE
        confirmation_summary = "Child tool audit is missing or incomplete; side effects cannot be ruled out."
        missing.append("actual_side_effects_unknown")
        actions.append("inspect_tool_review_records")
    elif not could_have_side_effects or actual_side_effects is False:
        confirmation_state = VerificationStatus.PASSED
        confirmation_summary = "No non-read-only side effect occurred."
    elif actual_side_effects is None:
        confirmation_state = VerificationStatus.INCONCLUSIVE
        confirmation_summary = "Actual side effects were not reported; confirmation cannot be inferred from capability alone."
        missing.append("actual_side_effects_unknown")
        actions.append("inspect_tool_review_records")
    elif not side_effect_required:
        confirmation_state = VerificationStatus.PASSED
        confirmation_summary = "Confirmation policy does not require approval for this reported side effect."
    elif confirmation == ConfirmationState.APPROVED:
        confirmation_state, confirmation_summary = VerificationStatus.PASSED, "Required side effect was confirmed."
    elif confirmation == ConfirmationState.REJECTED:
        confirmation_state, confirmation_summary = VerificationStatus.FAILED, "Required side effect was rejected."
        missing.append("side_effect_confirmation_rejected")
    else:
        confirmation_state, confirmation_summary = VerificationStatus.INCONCLUSIVE, "Required side-effect confirmation is pending or missing."
        missing.append("side_effect_confirmation")
        actions.append("await_side_effect_confirmation")
    checks.append(VerificationCheck(
        check_id="side_effect_confirmation",
        status=confirmation_state,
        summary=confirmation_summary,
        required=(
            confirmation_required
            or policy.require_side_effect_audit
            or (actual_side_effects is True and not could_have_side_effects)
        ),
    ))
    if result.status in {TaskResultStatus.FAILED, TaskResultStatus.CANCELLED, TaskResultStatus.TIMED_OUT, TaskResultStatus.BLOCKED}:
        checks.append(VerificationCheck(check_id="task_status", status=VerificationStatus.FAILED, summary=f"Task ended with {result.status.value} status."))
        actions.append("replan_or_abort")
    elif result.status == TaskResultStatus.PARTIAL:
        checks.append(VerificationCheck(check_id="task_status", status=VerificationStatus.INCONCLUSIVE, summary="Task result is partial."))
        missing.extend(result.missing_requirements or ("partial_result",))
        actions.append("resolve_partial_result")
    required_states = [check.status for check in checks if check.required]
    if VerificationStatus.FAILED in required_states:
        status = VerificationStatus.FAILED
        if not actions:
            actions.append("retry_or_replan")
    elif VerificationStatus.INCONCLUSIVE in required_states:
        status = VerificationStatus.INCONCLUSIVE
    else:
        status = VerificationStatus.PASSED
    if status != VerificationStatus.PASSED and not actions:
        actions.append("supply_missing_requirements")
    return VerificationResult(
        correlation_id=result.correlation_id,
        verification_id=f"verify:{result.result_id}",
        status=status,
        summary="; ".join(check.summary for check in checks),
        evidence_refs=tuple(evidence_map.values()),
        missing_requirements=tuple(dict.fromkeys(missing)),
        checks=tuple(checks),
        recommended_actions=tuple(dict.fromkeys(actions)),
    )


def verify_aggregate(
    plan: Plan,
    aggregate: AggregatedTaskResults,
    *,
    policy: VerificationPolicy | None = None,
    output_contract_results: dict[str, bool] | None = None,
    confirmation_by_step: dict[str, ConfirmationState] | None = None,
    actual_side_effects_by_step: dict[str, bool] | None = None,
) -> VerificationResult:
    """Verify the aggregate and its authoritative task results as one outcome."""

    if aggregate.plan_id != plan.plan_id:
        raise ValueError("Aggregate belongs to another plan.")
    by_id = {step.step_id: step for step in plan.steps}
    contract_results = output_contract_results or {}
    confirmations = confirmation_by_step or {}
    actual_effects = actual_side_effects_by_step or {}
    task_verifications = tuple(
        verify_task_result(
            by_id[result.step_id], result,
            artifacts=aggregate.artifacts,
            evidence_refs=aggregate.evidence_refs,
            output_contract_satisfied=contract_results.get(result.step_id),
            confirmation=confirmations.get(result.step_id, ConfirmationState.NOT_REQUIRED),
            actual_side_effects=actual_effects.get(result.step_id),
            policy=policy,
        )
        for result in aggregate.task_results
        if result.step_id in by_id
    )
    checks: list[VerificationCheck] = []
    missing: list[str] = []
    actions: list[str] = []
    if aggregate.status == AggregationStatus.COMPLETE:
        aggregate_state = VerificationStatus.PASSED
    elif aggregate.status in {AggregationStatus.FAILED, AggregationStatus.CONFLICTING}:
        aggregate_state = VerificationStatus.FAILED
    else:
        aggregate_state = VerificationStatus.INCONCLUSIVE
    checks.append(VerificationCheck(
        check_id="aggregate_outcome", status=aggregate_state,
        summary=f"Aggregation status is {aggregate.status.value}.",
    ))
    if aggregate.missing_step_ids:
        missing.extend(f"step:{step_id}" for step_id in aggregate.missing_step_ids)
        actions.append("complete_missing_steps")
    if aggregate.conflicts:
        missing.extend(f"conflict:{conflict.conflict_id}" for conflict in aggregate.conflicts)
        actions.append("resolve_aggregation_conflicts")
    for index, item in enumerate(task_verifications):
        for check in item.checks:
            checks.append(VerificationCheck(
                check_id=f"{item.verification_id}:{check.check_id}",
                status=check.status,
                summary=check.summary,
                required=check.required,
            ))
        missing.extend(item.missing_requirements)
        actions.extend(item.recommended_actions)
    required_states = [check.status for check in checks if check.required]
    if VerificationStatus.FAILED in required_states:
        status = VerificationStatus.FAILED
    elif VerificationStatus.INCONCLUSIVE in required_states:
        status = VerificationStatus.INCONCLUSIVE
    else:
        status = VerificationStatus.PASSED
    if status != VerificationStatus.PASSED and not actions:
        actions.append("replan_or_ask_user")
    return VerificationResult(
        correlation_id=plan.correlation_id,
        verification_id=f"verify-aggregate:{aggregate.plan_id}",
        status=status,
        summary="; ".join(check.summary for check in checks),
        evidence_refs=aggregate.evidence_refs,
        missing_requirements=tuple(dict.fromkeys(missing)),
        checks=tuple(checks),
        recommended_actions=tuple(dict.fromkeys(actions)),
    )
