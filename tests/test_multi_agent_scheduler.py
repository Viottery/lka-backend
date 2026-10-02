from __future__ import annotations

import asyncio

import pytest

from app.core.agent_runs import (
    AgentRunEvent,
    AgentRunRecord,
    AgentRunStatus,
    InMemoryAgentRunManager,
)
from app.core.context_driver import ContextDriver
from app.core.multi_agent import (
    FailureDetail,
    Plan,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)
from app.core.multi_agent_aggregation import ConfirmationState
from app.core.multi_agent_scheduler import MultiAgentScheduler, SchedulerContext


def _step(step_id: str, *deps: str) -> PlanStep:
    return PlanStep(
        correlation_id="trace_scheduler",
        step_id=step_id,
        objective=f"Complete {step_id}.",
        depends_on=deps,
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
        output_contract="A concise result.",
    )


def _active_parent(*steps: PlanStep):
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_scheduler", user_input="do the work")
    manager.mark_running(parent.run_id)
    coordinator = PlanStep(
        correlation_id=parent.trace_id,
        step_id="root_coordinator",
        objective="Coordinate work.",
        output_contract="Final answer.",
        status=PlanStepStatus.RUNNING,
    )
    plan = Plan(
        correlation_id=parent.trace_id,
        plan_id="plan_scheduler",
        parent_run_id=parent.run_id,
        session_id=parent.session_id,
        objective="do the work",
        steps=(coordinator, *steps),
        status=PlanStatus.RUNNING,
    )
    manager.record_multi_agent_plan(
        parent.run_id,
        event_type="fork_subtasks_validated",
        payload={"plan_id": plan.plan_id},
        plan=plan.model_dump(mode="json"),
    )
    scope = ScopeGrant(
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    context = SchedulerContext(
        parent_effective_scope=scope,
        session_scope=scope,
        workspace_scope=scope,
        policy_scope=scope,
        budget=RuntimeBudget(max_tool_calls=3),
    )
    return manager, parent, plan, context


def test_coordinator_side_effect_audit_includes_descendant_runs() -> None:
    class EventStore:
        def __init__(self, runs, events) -> None:
            self.runs = runs
            self.events = events

        def get_run(self, run_id):
            return self.runs.get(run_id)

        def list_events(self, run_id):
            return self.events.get(run_id, [])

    def event(run_id, sequence, event_type, payload):
        return AgentRunEvent(
            event_id=f"{run_id}:{sequence}", run_id=run_id, sequence=sequence,
            type=event_type, message=event_type, payload=payload, created_at="now",
        )

    coordinator = AgentRunRecord(
        run_id="coordinator", session_id="session", trace_id="trace",
        parent_run_id="parent", child_run_ids=("leaf",), status=AgentRunStatus.COMPLETED,
        user_input="coordinate", created_at="now",
        plan_id="plan-root", step_id="coordinate", attempt=1,
        metadata={"context_snapshot": {
            "child_run_id": "coordinator", "parent_run_id": "parent",
            "plan_id": "plan-root", "step_id": "coordinate", "session_id": "session",
        }, "multi_agent_plan": {"plan_id": "plan-nested"}},
    )
    leaf = AgentRunRecord(
        run_id="leaf", session_id="session", trace_id="trace",
        parent_run_id="coordinator", status=AgentRunStatus.COMPLETED,
        user_input="write", created_at="now",
        plan_id="plan-nested", step_id="write", attempt=1,
        metadata={"context_snapshot": {
            "child_run_id": "leaf", "parent_run_id": "coordinator",
            "plan_id": "plan-nested", "step_id": "write", "session_id": "session",
        }},
    )
    events = {
        "coordinator": [
            event("coordinator", 1, "run_started", {}),
            event("coordinator", 2, "run_completed", {"tool_event_count": 0}),
            event("coordinator", 3, "subtask_result", {"child_tool_audit": {
                "protocol_version": "child_tool_audit_v1", "complete": True, "invocations": [],
            }}),
        ],
        "leaf": [
            event("leaf", 1, "run_started", {}),
            event("leaf", 2, "safety_review_required", {"review": {
                "invocation_id": "inv-1", "read_only": False, "status": "pending",
            }}),
            event("leaf", 3, "safety_review_decided", {"review": {
                "invocation_id": "inv-1", "read_only": False, "status": "approved",
            }}),
            event("leaf", 4, "tool_started", {"tool_name": "matter.create"}),
            event("leaf", 5, "tool_completed", {"tool_name": "matter.create", "metadata": {
                "result": {"invocation_id": "inv-1", "status": "rejected"},
            }}),
            event("leaf", 6, "run_completed", {"tool_event_count": 1}),
            event("leaf", 7, "subtask_result", {"child_tool_audit": {
                "protocol_version": "child_tool_audit_v1", "complete": True,
                "invocations": [{"invocation_id": "inv-1", "tool_name": "matter.create",
                                 "read_only": False, "status": "rejected"}],
            }}),
        ],
    }
    scheduler = MultiAgentScheduler(run_manager=EventStore(
        {"coordinator": coordinator, "leaf": leaf}, events,
    ), child_executor=object(), child_runner=lambda **_: None)

    actual, confirmation = scheduler._child_tree_execution_evidence(
        "coordinator", expected_parent_id="parent", expected_plan_id="plan-root",
        expected_step_id="coordinate", expected_attempt=1, expected_trace_id="trace",
    )

    assert actual is True
    assert confirmation == ConfirmationState.APPROVED


def test_incomplete_child_audit_requires_replan_even_for_read_step() -> None:
    manager, parent, plan, _ = _active_parent(_step("a"))
    child = manager.create_child_run(
        parent_run_id=parent.run_id, plan_id=plan.plan_id, step_id="a", attempt=1,
        user_input="read evidence",
    )
    manager.mark_child_running(child.run_id)
    manager.complete_child_run(child.run_id)
    result = TaskResult(
        correlation_id=parent.trace_id, result_id="result:a", child_run_id=child.run_id,
        plan_id=plan.plan_id, step_id="a", snapshot_id="snapshot:a",
        status=TaskResultStatus.COMPLETED, summary="done",
        verification=VerificationResult(
            correlation_id=parent.trace_id,
            verification_id="verify:a",
            status=VerificationStatus.INCONCLUSIVE,
            summary="Output contract still needs review.",
            checks=(VerificationCheck(
                check_id="output_contract",
                status=VerificationStatus.INCONCLUSIVE,
                summary="No explicit contract decision.",
            ),),
        ),
    )
    scheduler = MultiAgentScheduler(
        run_manager=manager, child_executor=object(), child_runner=lambda **_: None
    )

    scheduled = scheduler._result(plan, [result], status="completed")

    assert scheduled.verification is not None
    assert scheduled.verification.status.value == "inconclusive"
    assert "actual_side_effects_unknown" in scheduled.verification.missing_requirements
    assert any(
        check.check_id.endswith(":output_contract")
        and check.status == VerificationStatus.INCONCLUSIVE
        for check in scheduled.verification.checks
    )
    assert scheduled.replan_required is True


class FakeChildExecutor:
    def __init__(
        self, manager: InMemoryAgentRunManager, *, results=None, delay=0,
        record_uncertain_tool_attempts=False,
    ):
        self.manager = manager
        self.scripted_results = {key: list(value) for key, value in (results or {}).items()}
        self.delay = delay
        self.record_uncertain_tool_attempts = record_uncertain_tool_attempts
        self.calls: list[tuple[str, int]] = []
        self.active = 0
        self.max_active = 0
        self.completion_order: list[str] = []

    async def execute(self, *, child_run_id, snapshot, views, **kwargs):
        child = self.manager.get_run(child_run_id)
        if child.status == AgentRunStatus.QUEUED:
            self.manager.attach_child_context(
                child_run_id,
                snapshot=snapshot.model_dump(mode="json"),
                views=views.model_dump(mode="json"),
            )
            self.manager.mark_child_running(child_run_id)
        self.calls.append((child.step_id, int(child.attempt or 1)))
        scripted = self.scripted_results.get(child.step_id, [])
        if child.status == AgentRunStatus.COMPLETED:
            return self._task_result(child, TaskResultStatus.COMPLETED, "resumed")
        if scripted and scripted[0] == "waiting":
            scripted.pop(0)
            self.manager.mark_waiting_confirmation(child_run_id, "review_child")
            return self._task_result(
                child,
                TaskResultStatus.BLOCKED,
                "waiting",
                failure=FailureDetail(
                    category="safety",
                    code="waiting_confirmation",
                    message="Waiting for approval.",
                ),
            )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if scripted and scripted[0] == "retryable_failure":
                scripted.pop(0)
                if self.record_uncertain_tool_attempts:
                    self.manager.append_event(
                        child_run_id, "tool_started", "Fake tool execution started.",
                        stage="tool_execute", payload={"tool_name": "knowledge.search"},
                    )
                self.manager.fail_child_run(child_run_id, error_type="ProviderTimeout", error="retry")
                return self._task_result(
                    child,
                    TaskResultStatus.FAILED,
                    "retry",
                    failure=FailureDetail(
                        category="provider",
                        code="timeout",
                        message="retry",
                        retryable=True,
                    ),
                )
            if scripted and scripted[0] == "non_retryable_failure":
                scripted.pop(0)
                self.manager.fail_child_run(child_run_id, error_type="PermanentFailure", error="failed")
                return self._task_result(
                    child, TaskResultStatus.FAILED, "failed",
                    failure=FailureDetail(
                        category="agent_execution", code="permanent", message="failed", retryable=False,
                    ),
                )
            self.manager.complete_child_run(child_run_id, result_snapshot={"answer": f"result:{child.step_id}"})
            self.manager.append_event(
                child_run_id, "run_completed", "Fake child completed.", stage="run",
                payload={"tool_event_count": 0},
            )
            self.manager.append_event(
                child_run_id, "subtask_result", "Fake child tool audit recorded.", stage="subtask",
                payload={"child_tool_audit": {
                    "protocol_version": "child_tool_audit_v1",
                    "complete": True,
                    "invocations": [],
                }},
            )
            self.completion_order.append(child.step_id)
            return self._task_result(child, TaskResultStatus.COMPLETED, f"result:{child.step_id}")
        finally:
            self.active -= 1

    @staticmethod
    def _task_result(child, status, summary, *, failure=None):
        snapshot_id = child.metadata.get("context_snapshot", {}).get("snapshot_id", "snapshot")
        return TaskResult(
            correlation_id=child.trace_id,
            result_id=f"result:{child.step_id}:{child.attempt}",
            child_run_id=child.run_id,
            plan_id=child.plan_id,
            step_id=child.step_id,
            snapshot_id=snapshot_id,
            status=status,
            summary=summary,
            failure=failure,
        )


def test_scheduler_runs_dag_dependencies_and_bounds_parallelism() -> None:
    manager, parent, _, context = _active_parent(
        _step("a"), _step("b"), _step("c", "a"), _step("d", "a", "b")
    )
    executor = FakeChildExecutor(manager, delay=0.01)
    scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=executor,
        context_driver=ContextDriver(),
        max_concurrency=2,
    )

    result = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))

    assert result.status == "completed"
    assert {item.step_id for item in result.task_results} == {"a", "b", "c", "d"}
    assert executor.max_active == 2
    assert executor.completion_order.index("a") < executor.completion_order.index("c")
    assert executor.completion_order.index("a") < executor.completion_order.index("d")
    saved_plan = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    assert all(step.status == PlanStepStatus.COMPLETED for step in saved_plan.steps[1:])

    replay = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))
    assert replay.status == "completed"
    assert {item.step_id for item in replay.task_results} == {"a", "b", "c", "d"}
    assert len(replay.task_results) == 4
    assert replay.aggregate is not None and replay.aggregate.status.value == "complete"
    assert sum(event.type == "multi_agent_aggregated" for event in manager.list_events(parent.run_id)) == 1


def test_scheduler_retries_only_retryable_failure_with_bounded_attempts() -> None:
    manager, parent, _, context = _active_parent(_step("retry_me"))
    executor = FakeChildExecutor(manager, results={"retry_me": ["retryable_failure", "ok"]})
    scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=executor,
        max_retries=1,
    )

    result = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))

    assert result.status == "completed"
    assert executor.calls == [("retry_me", 1), ("retry_me", 2)]
    assert result.task_results[-1].summary == "result:retry_me"
    assert result.aggregate is not None and result.aggregate.status.value == "complete"
    assert result.verification is not None and result.verification.status.value == "inconclusive"
    children = manager.child_tree(parent.run_id)
    assert [child.attempt for child in children] == [1, 2]


def test_retry_cannot_hide_an_earlier_attempt_with_incomplete_tool_audit() -> None:
    manager, parent, _, context = _active_parent(_step("retry_me"))
    executor = FakeChildExecutor(
        manager,
        results={"retry_me": ["retryable_failure", "ok"]},
        record_uncertain_tool_attempts=True,
    )
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor, max_retries=1)

    result = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))

    assert result.status == "completed"
    assert result.verification is not None
    assert "actual_side_effects_unknown" in result.verification.missing_requirements
    assert result.replan_required is True


def test_failed_dag_can_be_patched_and_resumed_with_a_new_attempt() -> None:
    from app.core.agent_turn import AgentTurnLoop
    from app.core.multi_agent import ForkPolicy, PlanPatch, PlanPatchOperation, ScopeGrant

    def forked(step_id: str, *dependencies: str) -> PlanStep:
        return _step(step_id, *dependencies).model_copy(update={
            "fork_operation_id": "fork-main",
            "fork_parent_step_id": "root_coordinator",
            "fork_depth": 1,
            "created_by_run_id": "parent",
        })

    manager, parent, _, context = _active_parent(
        forked("a"), forked("b", "a"), forked("c", "b")
    )
    executor = FakeChildExecutor(manager, results={"a": ["non_retryable_failure", "ok"]})
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor)
    first = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))
    assert first.status == "failed"
    assert first.replan_required is True
    assert [step.status for step in first.plan.steps[1:]] == [
        PlanStepStatus.FAILED, PlanStepStatus.BLOCKED, PlanStepStatus.BLOCKED,
    ]

    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = manager
    loop.fork_policy = ForkPolicy(
        max_depth=2, max_children=5, max_fork_size=3, allowed_scope=context.policy_scope,
    )
    loop.fork_scope_resolver = lambda _run_id: (
        context.parent_effective_scope, context.session_scope, context.workspace_scope,
    )
    loop.multi_agent_max_retries = 1
    loop.fork_execution = lambda run_id: asyncio.run(
        scheduler.execute_plan_async(run_id, context=context)
    )
    rejected = loop._handle_plan_patch_decision(
        run_id=parent.run_id,
        operation=PlanPatch(
            patch_id="widen-a", plan_id=first.plan.plan_id, expected_revision=0,
            operation=PlanPatchOperation.REDUCED_SCOPE, reason="Attempt an invalid scope expansion.",
            target_step_id="a",
            reduced_scope=ScopeGrant(
                allowed_packages=("filesystem",), allowed_tools=("filesystem.read_file",),
                side_effect_level=SideEffectLevel.READ,
            ),
        ).model_dump(mode="json"),
    )
    assert rejected["status"] == "rejected"
    assert manager.get_run(parent.run_id).metadata["multi_agent_plan"]["patch_revision"] == 0
    assert len(manager.child_tree(parent.run_id)) == 1
    assert manager.list_events(parent.run_id)[-1].type == "multi_agent_plan_patch_rejected"

    patched = loop._handle_plan_patch_decision(
        run_id=parent.run_id,
        operation=PlanPatch(
            patch_id="retry-a", plan_id=first.plan.plan_id, expected_revision=0,
            operation=PlanPatchOperation.RETRY_STEP, reason="Retry failed dependency.",
            target_step_id="a",
        ).model_dump(mode="json"),
    )
    assert patched["status"] == "applied"
    assert patched["execution_status"] == "completed"
    attempts = manager.child_tree(parent.run_id)
    assert [(child.step_id, child.attempt) for child in attempts] == [
        ("a", 1), ("a", 2), ("b", 1), ("c", 1),
    ]
    results = [
        TaskResult.model_validate(event.payload["task_result"])
        for event in manager.list_events(parent.run_id)
        if event.type == "subtask_result"
    ]
    assert [(result.step_id, result.attempt) for result in results] == [
        ("a", 1), ("a", 2), ("b", 1), ("c", 1),
    ]
    saved = manager.get_run(parent.run_id)
    assert saved.metadata["multi_agent_plan"]["patch_revision"] == 1
    assert saved.metadata["multi_agent_replan_required"] is False
    assert patched["aggregate"]["status"] == "complete"
    replayed_patch = loop._handle_plan_patch_decision(
        run_id=parent.run_id,
        operation=PlanPatch(
            patch_id="retry-a", plan_id=first.plan.plan_id, expected_revision=0,
            operation=PlanPatchOperation.RETRY_STEP, reason="Retry failed dependency.",
            target_step_id="a",
        ).model_dump(mode="json"),
    )
    assert replayed_patch["status"] == "applied"
    assert replayed_patch["execution_status"] == "completed"
    assert len(manager.child_tree(parent.run_id)) == 4


def test_waiting_confirmation_is_persisted_and_scheduler_resumes_after_child() -> None:
    manager, parent, _, context = _active_parent(_step("approval"))
    executor = FakeChildExecutor(manager, results={"approval": ["waiting"]})
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor)

    waiting = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))

    assert waiting.status == "waiting_confirmation"
    assert len(waiting.waiting_child_run_ids) == 1
    child_id = waiting.waiting_child_run_ids[0]
    child = manager.get_run(child_id)
    assert child.status == AgentRunStatus.WAITING_CONFIRMATION
    saved_plan = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    assert saved_plan.steps[1].status == PlanStepStatus.WAITING

    manager.resume_running(child_id)
    manager.complete_child_run(child_id, result_snapshot={"answer": "approved result"})
    manager.mark_waiting_confirmation(parent.run_id, "review_child")
    resumed = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))

    assert resumed.status == "completed"
    saved_plan = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    assert saved_plan.steps[1].status == PlanStepStatus.COMPLETED
    assert manager.get_run(parent.run_id).status == AgentRunStatus.WAITING_CONFIRMATION


def test_failed_dependency_blocks_downstream_steps() -> None:
    manager, parent, _, context = _active_parent(_step("first"), _step("second", "first"))
    executor = FakeChildExecutor(manager, results={"first": ["retryable_failure", "retryable_failure"]})
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor, max_retries=1)

    result = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))

    assert result.status == "failed"
    assert result.failed_step_ids == ("first", "second")


def test_scheduler_continues_ready_branches_after_failure_with_bounded_batch() -> None:
    manager, parent, _, context = _active_parent(
        _step("fails"), _step("blocked", "fails"), _step("independent")
    )
    executor = FakeChildExecutor(manager, results={"fails": ["non_retryable_failure"]})
    scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=executor,
        max_concurrency=1,
    )

    result = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))

    assert result.status == "failed"
    assert executor.completion_order == ["independent"]
    assert [step.status for step in result.plan.steps[1:]] == [
        PlanStepStatus.FAILED,
        PlanStepStatus.BLOCKED,
        PlanStepStatus.COMPLETED,
    ]
    assert {item.step_id for item in result.task_results} == {"fails", "independent"}
    assert result.aggregate is not None
    assert result.aggregate.status.value == "failed"


def test_resume_retry_of_timed_out_child_creates_new_attempt() -> None:
    manager, parent, _, context = _active_parent(_step("recover_timeout"))
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=FakeChildExecutor(manager))
    plan = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    step = plan.steps[1]

    running_plan = plan.transition_step(step.step_id, PlanStepStatus.READY).transition_step(
        step.step_id, PlanStepStatus.RUNNING
    )
    attempt, timed_out_child, snapshot, views = asyncio.run(
        scheduler._prepare_attempt(
            parent.run_id, manager.get_run(parent.run_id), running_plan,
            next(item for item in running_plan.steps if item.step_id == step.step_id),
            context, {},
        )
    )
    assert attempt == 1
    manager.attach_child_context(
        timed_out_child.run_id,
        snapshot=snapshot.model_dump(mode="json"),
        views=views.model_dump(mode="json"),
    )
    manager.mark_child_running(timed_out_child.run_id)
    manager.timeout_child_run(
        timed_out_child.run_id, error="interrupted before scheduler recovery"
    )
    manager.record_multi_agent_plan(
        parent.run_id,
        event_type="test_recovery_state",
        payload={"plan_id": plan.plan_id},
        plan=running_plan.model_dump(mode="json"),
    )

    class RetryTimedOutExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def execute(self, *, child_run_id, snapshot, views, **kwargs):
            child = manager.get_run(child_run_id)
            self.calls.append((child.run_id, int(child.attempt or 1)))
            if child.status == AgentRunStatus.TIMED_OUT:
                return FakeChildExecutor._task_result(
                    child,
                    TaskResultStatus.TIMED_OUT,
                    "timeout",
                    failure=FailureDetail(
                        category="provider", code="timeout", message="retry", retryable=True,
                    ),
                )
            manager.attach_child_context(
                child_run_id,
                snapshot=snapshot.model_dump(mode="json"),
                views=views.model_dump(mode="json"),
            )
            manager.mark_child_running(child_run_id)
            manager.complete_child_run(child_run_id, result_snapshot={"answer": "recovered"})
            return FakeChildExecutor._task_result(
                manager.get_run(child_run_id), TaskResultStatus.COMPLETED, "recovered"
            )

    retry_executor = RetryTimedOutExecutor()
    retry_scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=retry_executor,
        max_retries=1,
    )
    result = asyncio.run(retry_scheduler.execute_plan_async(parent.run_id, context=context))

    assert result.status == "completed"
    assert [child.attempt for child in manager.child_tree(parent.run_id)] == [1, 2]
    assert retry_executor.calls == [(timed_out_child.run_id, 1), (manager.child_tree(parent.run_id)[1].run_id, 2)]


def test_parent_cancellation_propagates_to_running_child() -> None:
    manager, parent, _, context = _active_parent(_step("long_running"))
    started = asyncio.Event()

    class BlockingExecutor:
        async def execute(self, *, child_run_id, snapshot, views, **kwargs):
            manager.attach_child_context(
                child_run_id,
                snapshot=snapshot.model_dump(mode="json"),
                views=views.model_dump(mode="json"),
            )
            manager.mark_child_running(child_run_id)
            started.set()
            await asyncio.Event().wait()

    executor = BlockingExecutor()
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor)

    async def run_and_cancel():
        task = asyncio.create_task(scheduler.execute_plan_async(parent.run_id, context=context))
        await started.wait()
        child_id = manager.get_run(parent.run_id).child_run_ids[0]
        manager.cancel_run(parent.run_id, reason="cancel test")
        with pytest.raises(asyncio.CancelledError):
            await task
        return child_id

    child_id = asyncio.run(run_and_cancel())
    assert manager.get_run(parent.run_id).status == AgentRunStatus.CANCELLED
    assert manager.get_run(child_id).status == AgentRunStatus.CANCELLED


def test_manual_retry_requires_latest_failed_attempt_and_reschedules_step() -> None:
    manager, parent, _, context = _active_parent(_step("retry_manually"))
    executor = FakeChildExecutor(
        manager, results={"retry_manually": ["retryable_failure", "ok"]}
    )
    scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=executor,
        max_retries=0,
    )

    first = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))
    assert first.status == "failed"
    failed_child = manager.child_tree(parent.run_id)[0]
    assert failed_child.status == AgentRunStatus.FAILED

    retried = asyncio.run(
        scheduler.retry_child_run_async(
            parent_run_id=parent.run_id,
            child_run_id=failed_child.run_id,
            context=context,
        )
    )

    assert retried.status == "completed"
    assert executor.calls == [("retry_manually", 1), ("retry_manually", 2)]
    with pytest.raises(ValueError):
        asyncio.run(
            scheduler.retry_child_run_async(
                parent_run_id=parent.run_id,
                child_run_id=failed_child.run_id,
                context=context,
            )
        )


def test_child_wall_time_budget_times_out_child_without_retrying() -> None:
    manager, parent, _, context = _active_parent(_step("timeout"))
    context = SchedulerContext(
        parent_effective_scope=context.parent_effective_scope,
        session_scope=context.session_scope,
        workspace_scope=context.workspace_scope,
        policy_scope=context.policy_scope,
        budget=RuntimeBudget(max_wall_time_seconds=1),
    )

    class NeverEndingExecutor:
        async def execute(self, *, child_run_id, snapshot, views, **kwargs):
            manager.attach_child_context(
                child_run_id,
                snapshot=snapshot.model_dump(mode="json"),
                views=views.model_dump(mode="json"),
            )
            manager.mark_child_running(child_run_id)
            await asyncio.Event().wait()

    scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=NeverEndingExecutor(),
        max_retries=3,
    )
    result = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))

    child = manager.child_tree(parent.run_id)[0]
    assert result.status == "failed"
    assert child.status == AgentRunStatus.TIMED_OUT
    assert child.error_type == "timeout"
    assert len(manager.child_tree(parent.run_id)) == 1

    class CompleteOnRetry:
        async def execute(self, *, child_run_id, snapshot, views, **kwargs):
            manager.attach_child_context(
                child_run_id,
                snapshot=snapshot.model_dump(mode="json"),
                views=views.model_dump(mode="json"),
            )
            manager.mark_child_running(child_run_id)
            manager.complete_child_run(child_run_id, result_snapshot={"answer": "retried"})
            return FakeChildExecutor._task_result(
                manager.get_run(child_run_id), TaskResultStatus.COMPLETED, "retried"
            )

    retry_scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=CompleteOnRetry(),
    )
    retried = asyncio.run(
        retry_scheduler.retry_child_run_async(
            parent_run_id=parent.run_id,
            child_run_id=child.run_id,
            context=context,
        )
    )
    assert retried.status == "completed"
    assert [item.attempt for item in manager.child_tree(parent.run_id)] == [1, 2]
