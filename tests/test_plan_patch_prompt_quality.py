from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_turn import (
    AgentTurnLoop,
    AgentTurnResult,
    _plan_patch_function_schema,
    _turn_run_id,
    _turn_run_manager,
)
from app.core.multi_agent import (
    GENERAL_AGENT_ID,
    ContextSnapshot,
    FailureDetail,
    ForkPolicy,
    Plan,
    PlanPatch,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RuntimeBudget,
    ScopeGrant,
    TaskResult,
    TaskResultStatus,
)
from app.core.multi_agent_scheduler import MultiAgentScheduler, SchedulerContext
from app.core.runtime import LocalKnowledgeAgentRuntime


@pytest.fixture
def planner():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="parent", user_input="Compare independent findings.")
    manager.mark_running(run.run_id)
    plan = Plan(
        correlation_id=run.trace_id,
        plan_id="persisted-plan",
        parent_run_id=run.run_id,
        session_id=run.session_id,
        objective=run.user_input,
        status=PlanStatus.REPLANNING,
        steps=tuple(
            PlanStep(
                correlation_id=run.trace_id,
                step_id=step_id,
                objective="Inspect an independent source.",
                output_contract="Return cited findings and limitations.",
                status=status,
            )
            for step_id, status in (
                ("part-a", PlanStepStatus.FAILED),
                ("part-b", PlanStepStatus.BLOCKED),
                ("part-c", PlanStepStatus.FAILED),
                ("already-done", PlanStepStatus.COMPLETED),
            )
        ),
    )
    manager._update_run(
        run.run_id,
        status=AgentRunStatus.RUNNING,
        metadata_patch={
            "multi_agent_plan": plan.model_dump(mode="json"),
            "multi_agent_replan_required": True,
        },
    )
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = manager
    loop.fork_policy = ForkPolicy(
        max_depth=1, max_children=5, max_fork_size=3, allowed_scope=ScopeGrant()
    )
    loop.decision_format_max_attempts = 2
    loop.llm_generation_token_budget = 512
    loop.llm_client = SimpleNamespace(supports_function_calling=False)
    loop._observations_within_prompt_budget = lambda observations: observations
    loop._route_context = lambda route: route
    loop._context_window_for_llm = lambda window: window
    loop._agent_catalog_for_prompt = list
    prompts = []
    responses = []

    def complete(**kwargs):
        prompts.append(kwargs)
        if responses:
            return responses.pop(0)
        return SimpleNamespace(content='{"operation":{"type":"final_answer","reason":"done"}}')

    loop._complete_text_with_retry = complete
    token = _turn_run_id.set(run.run_id)
    try:
        yield loop, manager, run.run_id, prompts, responses
    finally:
        _turn_run_id.reset(token)


def _decide(loop, observations=None):
    return loop._decide_next_action(
        user_input="Compare independent findings.",
        route={},
        context_window={},
        package_catalog=[],
        expanded_package_names=[],
        expanded_tools=[],
        observations=observations or [],
        llm_events=[],
    )


def _patch(**updates):
    return {
        "patch_id": "degrade-a",
        "plan_id": "persisted-plan",
        "expected_revision": 0,
        "operation": "skip_and_degrade",
        "reason": "Use evidence already collected by the parent.",
        "target_step_id": "part-a",
        "degradation_note": "Independent child verification did not complete.",
        **updates,
    }


def _zero_tool_child(manager, run_id, plan, step_id, status):
    """A terminal fixture with an actual empty tool lifecycle, not inferred safety."""
    child = manager.create_child_run(
        parent_run_id=run_id,
        plan_id=plan.plan_id,
        step_id=step_id,
        attempt=1,
        user_input="Inspect independent evidence.",
    )
    snapshot = ContextSnapshot(
        correlation_id=plan.correlation_id,
        snapshot_id=f"snapshot-{step_id}",
        parent_run_id=run_id,
        child_run_id=child.run_id,
        session_id=child.session_id,
        plan_id=plan.plan_id,
        step_id=step_id,
        objective=child.user_input,
        output_contract="Return findings and limitations.",
        effective_scope=ScopeGrant(),
        budget=RuntimeBudget(max_tokens=32768),
        policy_version="v1",
        workspace_version="v1",
        permission_version="v1",
    )
    manager._update_run(
        child.run_id,
        status=child.status,
        metadata_patch={
            "context_snapshot": snapshot.model_dump(mode="json"),
            "agent_id": GENERAL_AGENT_ID,
            "executor_kind": "react",
        },
    )
    manager.mark_child_running(child.run_id)
    if status == TaskResultStatus.FAILED:
        manager.fail_child_run(
            child.run_id, error_type="test", error="Attempt failed before tool dispatch."
        )
    else:
        manager.append_event(
            child.run_id, "run_completed", "No tools executed.", payload={"tool_event_count": 0}
        )
        manager.append_event(
            child.run_id,
            "child_tool_audit",
            "Empty completed tool lifecycle.",
            payload={
                "audit": {
                    "protocol_version": "child_tool_audit_v1",
                    "complete": True,
                    "invocations": [],
                }
            },
        )
        manager.complete_child_run(child.run_id)
    return child


def _json_response(patch):
    return SimpleNamespace(content=json.dumps({"operation": {"type": "plan_patch", **patch}}))


def test_replan_prompt_exposes_current_identity_and_flat_contract(planner):
    loop, _, _, prompts, _ = planner
    _decide(loop)
    contract = json.loads(prompts[0]["user_prompt"])["plan_patch_contract"]
    assert contract["plan_id"] == "persisted-plan"
    assert contract["expected_revision"] == 0
    assert contract["eligible_failed_step_ids"] == ["part-a", "part-b", "part-c"]
    required = contract["required_fields_by_operation"]
    assert required == {
        "retry_step": ["target_step_id"],
        "reduced_scope": ["target_step_id", "reduced_scope"],
        "alternative_step": ["target_step_id", "alternative_step"],
        "skip_and_degrade": ["target_step_id", "degradation_note"],
        "ask_user": ["user_question"],
        "abort": [],
    }
    example = contract["required_shape_example"]["operation"]
    assert example["type"] == "plan_patch"
    assert isinstance(example["operation"], str)
    assert isinstance(example["target_step_id"], str)
    PlanPatch.model_validate({key: value for key, value in example.items() if key != "type"})
    assert "one target_step_id" in prompts[0]["system_prompt"]


@pytest.mark.parametrize("native", [False, True])
def test_missing_field_gets_one_specific_repair_without_losing_intent(planner, native):
    loop, manager, run_id, prompts, responses = planner
    invalid = _patch()
    invalid.pop("degradation_note")
    if native:
        loop.llm_client = SimpleNamespace(
            supports_function_calling=True, supports_required_tool_choice=True
        )
        responses.extend(
            SimpleNamespace(
                content="", tool_calls=[SimpleNamespace(name="agent_plan_patch", arguments=patch)]
            )
            for patch in (invalid, _patch())
        )
    else:
        responses.extend([_json_response(invalid), _json_response(_patch())])
    decision = _decide(loop)
    assert decision["action"] == "plan_patch"
    assert len(prompts) == 2
    feedback = json.loads(prompts[1]["user_prompt"])["plan_patch_repair"]
    assert "degradation_note" in feedback["validation_errors"]
    assert feedback["patch_id"] == "degrade-a"
    assert feedback["target_step_id"] == "part-a"
    assert manager.get_run(run_id).metadata["multi_agent_plan"]["patch_revision"] == 0
    assert manager.list_events(run_id)[-1].type == "multi_agent_plan_patch_rejected"


@pytest.mark.parametrize("native", [False, True])
def test_repeated_invalid_patch_stops_after_one_repair(planner, native):
    loop, manager, run_id, prompts, responses = planner
    invalid = _patch(target_step_id=["part-a", "part-b"])
    if native:
        loop.llm_client = SimpleNamespace(
            supports_function_calling=True, supports_required_tool_choice=True
        )
        responses.extend(
            SimpleNamespace(
                content="", tool_calls=[SimpleNamespace(name="agent_plan_patch", arguments=invalid)]
            )
            for _ in range(8)
        )
    else:
        responses.extend(_json_response(invalid) for _ in range(8))
    decision = _decide(loop)
    assert decision["action"] == "invalid_empty_decision"
    assert len(prompts) == 2
    assert "repair" in decision["reason"].lower()
    assert manager.get_run(run_id).metadata["multi_agent_replan_required"] is True
    assert (
        len([e for e in manager.list_events(run_id) if e.type == "multi_agent_plan_patch_rejected"])
        == 2
    )


def test_policy_rejections_are_bounded_without_mutation(planner):
    loop, manager, run_id, prompts, responses = planner
    observations = []
    for _ in range(2):
        responses.append(_json_response(_patch(plan_id="wrong-plan")))
        decision = _decide(loop, observations)
        assert decision["action"] == "plan_patch"
        outcome = loop._handle_plan_patch_decision(run_id=run_id, operation=decision["operation"])
        assert outcome["status"] == "rejected"
        observations.append({"action": "plan_patch", **outcome})
    assert _decide(loop, observations)["action"] == "invalid_empty_decision"
    assert len(prompts) == 2
    assert manager.get_run(run_id).metadata["multi_agent_plan"]["patch_revision"] == 0
    assert manager.get_run(run_id).metadata["multi_agent_replan_required"] is True


def test_all_skipped_without_validated_degradation_does_not_unlock_answer(planner):
    loop, manager, run_id, _, _ = planner
    run = manager.get_run(run_id)
    raw = run.metadata["multi_agent_plan"]
    for step in raw["steps"]:
        step["status"] = "skipped"
    manager._update_run(run_id, status=run.status, metadata_patch={"multi_agent_plan": raw})
    loop._execute_fork_plan(run_id=run_id, operation_id="patch:invented")
    assert loop._multi_agent_replan_pending() is True
    assert "multi_agent_degradation" not in manager.get_run(run_id).metadata


def test_degradation_cannot_bypass_pending_child_review(planner):
    loop, manager, run_id, _, _ = planner
    run = manager.get_run(run_id)
    plan = Plan.model_validate(run.metadata["multi_agent_plan"])
    manager._update_run(
        run_id,
        status=run.status,
        metadata_patch={
            "multi_agent_plan": plan.model_copy(update={"steps": plan.steps[:3]}).model_dump(
                mode="json"
            )
        },
    )
    child = manager.create_child_run(
        parent_run_id=run_id,
        plan_id=plan.plan_id,
        step_id="part-a",
        attempt=1,
        user_input="Inspect an independent source.",
    )
    manager.mark_child_running(child.run_id)
    manager.mark_waiting_confirmation(child.run_id, confirmation_id="review-required")
    for revision, step_id in enumerate(("part-a", "part-b", "part-c")):
        outcome = loop._handle_plan_patch_decision(
            run_id=run_id,
            operation=_patch(
                patch_id=f"skip-{step_id}",
                expected_revision=revision,
                target_step_id=step_id,
            ),
        )
        assert outcome["status"] == "applied"
    assert loop._multi_agent_replan_pending() is True
    assert manager.get_run(child.run_id).status == AgentRunStatus.WAITING_CONFIRMATION
    assert "multi_agent_degradation" not in manager.get_run(run_id).metadata


def test_recovery_degrades_three_failed_steps_then_answers(planner):
    loop, manager, run_id, prompts, responses = planner
    run = manager.get_run(run_id)
    plan = Plan.model_validate(run.metadata["multi_agent_plan"])
    plan = plan.model_copy(
        update={
            "steps": (
                PlanStep(
                    correlation_id=run.trace_id,
                    step_id="root_coordinator",
                    objective=run.user_input,
                    output_contract="Final answer.",
                    status=PlanStepStatus.RUNNING,
                ),
                *(s.model_copy(update={"status": PlanStepStatus.FAILED}) for s in plan.steps[:3]),
            )
        }
    )
    manager._update_run(
        run_id, status=run.status, metadata_patch={"multi_agent_plan": plan.model_dump(mode="json")}
    )
    scheduler = MultiAgentScheduler(
        run_manager=manager, child_executor=SimpleNamespace(execute=None)
    )
    for step in plan.steps[1:]:
        child = _zero_tool_child(manager, run_id, plan, step.step_id, TaskResultStatus.FAILED)
        scheduler._save_result(
            run_id,
            TaskResult(
                correlation_id=plan.correlation_id,
                result_id=f"result-{step.step_id}",
                child_run_id=child.run_id,
                plan_id=plan.plan_id,
                step_id=step.step_id,
                snapshot_id=f"snapshot-{step.step_id}",
                status=TaskResultStatus.FAILED,
                summary="Attempt failed before tool dispatch.",
                failure=FailureDetail(
                    category="test", code="attempt_failed", message="Attempt failed."
                ),
            ),
        )
    scope = ScopeGrant()
    context = SchedulerContext(
        parent_effective_scope=scope,
        session_scope=scope,
        workspace_scope=scope,
        policy_scope=scope,
        budget=RuntimeBudget(),
        fork_policy=loop.fork_policy,
    )
    loop.fork_execution = lambda parent: asyncio.run(
        scheduler.execute_plan_async(parent, context=context)
    )
    observations = [{"action": "tool_call", "result": {"status": "completed"}}]
    for revision, step_id in enumerate(("part-a", "part-b", "part-c")):
        patch = _patch(
            patch_id=f"degrade-{step_id}", expected_revision=revision, target_step_id=step_id
        )
        invalid = dict(patch)
        invalid.pop("degradation_note")
        responses.extend([_json_response(invalid), _json_response(patch)])
        decision = _decide(loop, observations)
        assert decision["action"] == "plan_patch"
        outcome = loop._handle_plan_patch_decision(run_id=run_id, operation=decision["operation"])
        assert outcome["status"] == "applied"
        observations.append({"action": "plan_patch", **outcome})
        assert outcome["patch_revision"] == revision + 1
    assert loop._multi_agent_replan_pending() is False
    assert _decide(loop, observations)["action"] == "final_answer"
    loop.llm_client = object()
    responses.append(
        SimpleNamespace(content="Conclusion from parent evidence; independent checks failed.")
    )
    assert "independent checks failed" in loop._answer_with_llm(
        user_input="Compare independent findings.",
        route={},
        context_window={},
        observations=observations,
        final_decision={"action": "final_answer"},
        llm_events=[],
    )
    assert prompts[-1]["stage"] == "answer"
    plan = Plan.model_validate(manager.get_run(run_id).metadata["multi_agent_plan"])
    assert len(plan.patch_history) == 3
    assert all(s.status == PlanStepStatus.SKIPPED for s in plan.steps[1:])
    assert manager.get_run(run_id).metadata["multi_agent_degradation"]["skipped_step_ids"] == [
        "part-a",
        "part-b",
        "part-c",
    ]
    assert manager.get_run(run_id).metadata["multi_agent_verification"]["status"] != "passed"
    replay = loop._handle_plan_patch_decision(run_id=run_id, operation=decision["operation"])
    assert replay["status"] == "applied"
    assert replay["replan_required"] is False
    assert (
        len([e for e in manager.list_events(run_id) if e.type == "multi_agent_plan_degraded"]) == 1
    )
    runtime = SimpleNamespace(agent_run_manager=manager, multi_agent_scheduler=scheduler)
    loop.fork_plan_finalizer = lambda parent: LocalKnowledgeAgentRuntime.finalize_multi_agent_plan(
        runtime, parent
    )
    loop._finalize_multi_agent_plan(run_id)
    token = _turn_run_manager.set(manager)
    try:
        loop._complete_current_run(
            AgentTurnResult(
                run_id=run_id,
                session_id=run.session_id,
                trace_id=run.trace_id,
                answer="Conclusion from parent evidence; independent checks failed.",
            )
        )
    finally:
        _turn_run_manager.reset(token)
    final_run = manager.get_run(run_id)
    assert final_run.status == AgentRunStatus.COMPLETED
    assert final_run.metadata["multi_agent_plan"]["status"] == "completed"
    assert final_run.metadata["multi_agent_verification"]["status"] != "passed"


def test_native_schema_and_validator_share_operation_requirements():
    schema = _plan_patch_function_schema()
    for variant in schema["oneOf"]:
        operation = variant["properties"]["operation"]["enum"][0]
        assert variant["required"] == list(PlanPatch.required_fields_by_operation[operation])
    assert "agent_id" in schema["properties"]["alternative_step"]["properties"]
    assert "agent_kind" not in schema["properties"]["alternative_step"]["properties"]


def _mixed_terminal_recovery(planner, *, operation, has_output):
    loop, manager, run_id, _, _ = planner
    run = manager.get_run(run_id)
    plan = Plan.model_validate(run.metadata["multi_agent_plan"])
    initial = (
        (plan.steps[0],)
        if operation == "alternative_step"
        else (
            plan.steps[0],
            plan.steps[-1].model_copy(update={"status": PlanStepStatus.PENDING}),
        )
    )
    manager._update_run(
        run_id,
        status=run.status,
        metadata_patch={
            "multi_agent_plan": plan.model_copy(update={"steps": initial}).model_dump(mode="json"),
        },
    )
    patch = _patch(operation=operation)
    completed_id = "already-done"
    if operation == "alternative_step":
        patch.pop("degradation_note")
        completed_id = "replacement"
        patch["alternative_step"] = {
            "correlation_id": plan.correlation_id,
            "step_id": completed_id,
            "objective": "Return an independent finding using available evidence.",
            "output_contract": "Return a conclusion with limitations.",
        }
    assert loop._handle_plan_patch_decision(run_id=run_id, operation=patch)["status"] == "applied"
    plan = Plan.model_validate(manager.get_run(run_id).metadata["multi_agent_plan"])
    plan = plan.model_copy(
        update={
            "steps": tuple(
                s.model_copy(update={"status": PlanStepStatus.COMPLETED})
                if s.step_id == completed_id
                else s
                for s in plan.steps
            )
        }
    )
    manager._update_run(
        run_id,
        status=run.status,
        metadata_patch={
            "multi_agent_plan": plan.model_dump(mode="json"),
        },
    )
    scheduler = MultiAgentScheduler(
        run_manager=manager, child_executor=SimpleNamespace(execute=None)
    )
    results = []
    for step_id, status in (
        ("part-a", TaskResultStatus.FAILED),
        (completed_id, TaskResultStatus.COMPLETED),
    ):
        child = _zero_tool_child(manager, run_id, plan, step_id, status)
        result = TaskResult(
            correlation_id=plan.correlation_id,
            result_id=f"result-{step_id}",
            child_run_id=child.run_id,
            plan_id=plan.plan_id,
            step_id=step_id,
            snapshot_id=f"snapshot-{step_id}",
            status=status,
            summary="Child's concrete conclusion."
            if has_output
            else "Child Agent completed without a textual answer.",
            missing_requirements=() if has_output else ("child_answer_missing",),
            failure=FailureDetail(category="test", code="attempt_failed", message="Attempt failed.")
            if status == TaskResultStatus.FAILED
            else None,
        )
        scheduler._save_result(run_id, result)
        results.append(result)
    schedule = scheduler._result(plan, results[-1:], status="completed", history=results)
    assert schedule.replan_required is True
    assert "actual_side_effects_unknown" not in schedule.verification.missing_requirements
    loop.fork_execution = lambda _: pytest.fail("Terminal children must not be scheduled again.")
    return scheduler, patch, completed_id


@pytest.mark.parametrize("operation", ["alternative_step", "skip_and_degrade"])
@pytest.mark.parametrize("has_output", [True, False])
def test_mixed_terminal_plan_finishes_without_repeating_completed_child(
    planner, operation, has_output
):
    loop, manager, run_id, prompts, responses = planner
    scheduler, patch, completed_id = _mixed_terminal_recovery(
        planner,
        operation=operation,
        has_output=has_output,
    )
    verification = manager.get_run(run_id).metadata["multi_agent_verification"]
    observations = [{"action": "plan_patch", "replan_required": True, "verification": verification}]
    decision = _decide(loop, observations)
    assert decision["action"] == "final_answer"
    assert (
        not prompts
    )  # A control call cannot re-read completed work just for inconclusive verification.
    assert loop._multi_agent_replan_pending() is False
    recovery = observations[-1]
    assert recovery["action"] == "multi_agent_recovery"
    assert recovery["completed_step_ids"] == [completed_id]
    assert recovery["skipped_step_ids"] == ["part-a"]
    assert recovery["verification"]["status"] != "passed"
    assert recovery["missing_output_step_ids"] == ([] if has_output else [completed_id])
    assert "verification is incomplete" in decision["reason"]
    if has_output:
        assert recovery["task_results"][0]["summary"] == "Child's concrete conclusion."
    loop.llm_client = object()
    responses.append(
        SimpleNamespace(content="Available conclusion; independent verification is incomplete.")
    )
    answer = loop._answer_with_llm(
        user_input="Compare independent findings.",
        route={},
        context_window={},
        observations=observations,
        final_decision=decision,
        llm_events=[],
    )
    assert "verification is incomplete" in answer
    answer_observation = json.loads(prompts[-1]["user_prompt"])["observations"][-1]
    assert answer_observation["missing_output_step_ids"] == ([] if has_output else [completed_id])
    replay = loop._handle_plan_patch_decision(run_id=run_id, operation=patch)
    assert replay["replan_required"] is False
    assert manager.get_run(run_id).metadata["multi_agent_verification"] == verification
    assert (
        len([e for e in manager.list_events(run_id) if e.type == "multi_agent_recovery_ready"]) == 1
    )
    runtime = SimpleNamespace(agent_run_manager=manager, multi_agent_scheduler=scheduler)
    LocalKnowledgeAgentRuntime.finalize_multi_agent_plan(runtime, run_id)
    assert manager.get_run(run_id).metadata["multi_agent_plan"]["status"] == "completed"
    token = _turn_run_manager.set(manager)
    try:
        run = manager.get_run(run_id)
        loop._complete_current_run(
            AgentTurnResult(
                run_id=run_id,
                session_id=run.session_id,
                trace_id=run.trace_id,
                answer=answer,
            )
        )
    finally:
        _turn_run_manager.reset(token)
    assert manager.get_run(run_id).status == AgentRunStatus.COMPLETED


@pytest.mark.parametrize(
    "status",
    [PlanStepStatus.PENDING, PlanStepStatus.FAILED, PlanStepStatus.BLOCKED, PlanStepStatus.WAITING],
)
def test_mixed_recovery_does_not_hide_unresolved_canonical_steps(planner, status):
    loop, manager, run_id, _, _ = planner
    _mixed_terminal_recovery(planner, operation="alternative_step", has_output=True)
    run = manager.get_run(run_id)
    raw = run.metadata["multi_agent_plan"]
    raw["steps"][-1]["status"] = status.value
    manager._update_run(run_id, status=run.status, metadata_patch={"multi_agent_plan": raw})
    _decide(loop)
    assert loop._multi_agent_replan_pending() is True
    assert "multi_agent_recovery" not in manager.get_run(run_id).metadata


def test_mixed_recovery_does_not_bypass_active_child_review(planner):
    loop, manager, run_id, _, _ = planner
    _mixed_terminal_recovery(planner, operation="alternative_step", has_output=True)
    run = manager.get_run(run_id)
    plan = Plan.model_validate(run.metadata["multi_agent_plan"])
    child = manager.create_child_run(
        parent_run_id=run_id,
        plan_id=plan.plan_id,
        step_id="part-a",
        attempt=2,
        user_input="Pending review.",
    )
    manager.mark_child_running(child.run_id)
    manager.mark_waiting_confirmation(child.run_id, confirmation_id="review-required")
    _decide(loop)
    assert loop._multi_agent_replan_pending() is True
    assert "multi_agent_recovery" not in manager.get_run(run_id).metadata


def test_mixed_recovery_does_not_unlock_unpatched_skips(planner):
    loop, manager, run_id, _, _ = planner
    _mixed_terminal_recovery(planner, operation="alternative_step", has_output=True)
    run = manager.get_run(run_id)
    raw = run.metadata["multi_agent_plan"]
    raw.update(patch_history=[], patch_revision=0)
    manager._update_run(run_id, status=run.status, metadata_patch={"multi_agent_plan": raw})
    _decide(loop)
    assert loop._multi_agent_replan_pending() is True


def test_mixed_recovery_preserves_observed_side_effect_authority_failure(planner):
    loop, manager, run_id, _, _ = planner
    _mixed_terminal_recovery(planner, operation="alternative_step", has_output=True)
    run = manager.get_run(run_id)
    verification = run.metadata["multi_agent_verification"]
    verification["missing_requirements"].append("actual_side_effect_scope_mismatch")
    manager._update_run(
        run_id, status=run.status, metadata_patch={"multi_agent_verification": verification}
    )
    _decide(loop)
    assert loop._multi_agent_replan_pending() is True


@pytest.mark.parametrize("operation", ["alternative_step", "skip_and_degrade"])
def test_mixed_recovery_preserves_unknown_execution_audit(planner, operation):
    loop, manager, run_id, _, _ = planner
    _mixed_terminal_recovery(planner, operation=operation, has_output=True)
    run = manager.get_run(run_id)
    verification = run.metadata["multi_agent_verification"]
    verification["missing_requirements"] = ["actual_side_effects_unknown"]
    manager._update_run(
        run_id, status=run.status, metadata_patch={"multi_agent_verification": verification}
    )
    _decide(loop)
    assert loop._multi_agent_replan_pending() is True
    assert "multi_agent_recovery" not in manager.get_run(run_id).metadata


def test_explicit_all_skipped_degradation_cannot_bypass_unknown_execution_audit(planner):
    loop, manager, run_id, _, _ = planner
    run = manager.get_run(run_id)
    plan = Plan.model_validate(run.metadata["multi_agent_plan"])
    manager._update_run(
        run_id,
        status=run.status,
        metadata_patch={
            "multi_agent_plan": plan.model_copy(update={"steps": plan.steps[:3]}).model_dump(
                mode="json"
            ),
            "multi_agent_verification": {
                "status": "inconclusive",
                "missing_requirements": ["actual_side_effects_unknown"],
            },
        },
    )
    for revision, step_id in enumerate(("part-a", "part-b", "part-c")):
        assert (
            loop._handle_plan_patch_decision(
                run_id=run_id,
                operation=_patch(
                    patch_id=f"skip-{step_id}",
                    expected_revision=revision,
                    target_step_id=step_id,
                ),
            )["status"]
            == "applied"
        )
    assert loop._multi_agent_replan_pending() is True
    assert "multi_agent_degradation" not in manager.get_run(run_id).metadata
