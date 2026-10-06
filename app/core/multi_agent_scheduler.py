"""Durable DAG scheduler for validated multi-Agent plans."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.agent_executors import AgentExecutorRegistry
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.child_agent import ChildAgentExecutor
from app.core.context_driver import (
    ContextDriver,
    ContextRequest,
    ContextViewMode,
    EvidenceCandidate,
)
from app.core.multi_agent import (
    GENERAL_AGENT_ID,
    ForkPolicy,
    MemoryReference,
    Plan,
    PlanPatchContext,
    PlanPatchOperation,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
    VerificationResult,
    VerificationStatus,
)
from app.core.multi_agent_aggregation import (
    AggregatedTaskResults,
    ConfirmationState,
    VerificationPolicy,
    aggregate_task_results,
    derive_child_execution_evidence,
    verify_aggregate,
)
from app.core.multi_agent_replan import replay_plan_patch_history


class MultiAgentScheduleResult(BaseModel):
    """Serializable outcome of one scheduler pass."""

    model_config = ConfigDict(frozen=True)

    status: str
    plan: Plan
    task_results: tuple[TaskResult, ...] = ()
    waiting_child_run_ids: tuple[str, ...] = ()
    failed_step_ids: tuple[str, ...] = ()
    aggregate: AggregatedTaskResults | None = None
    verification: VerificationResult | None = None
    replan_required: bool = False


@dataclass(frozen=True)
class SchedulerContext:
    parent_effective_scope: ScopeGrant
    session_scope: ScopeGrant
    workspace_scope: ScopeGrant
    policy_scope: ScopeGrant
    budget: RuntimeBudget
    policy_version: str = "policy_v1"
    workspace_version: str = "workspace_v1"
    permission_version: str = "permission_v1"
    evidence_candidates: tuple[EvidenceCandidate, ...] = ()
    memory_candidates: tuple[MemoryReference, ...] = ()
    view_mode: ContextViewMode = ContextViewMode.WORKING
    fork_policy: ForkPolicy | None = None


class MultiAgentScheduler:
    """Executes ready plan steps, persisting each transition on the parent run.

    The scheduler deliberately receives access ceilings from runtime. It never
    interprets an empty scope as unrestricted; ContextDriver intersects the
    plan request against all four caller supplied scopes.
    """

    def __init__(
        self,
        *,
        run_manager: InMemoryAgentRunManager,
        child_executor: ChildAgentExecutor,
        agent_registry: AgentExecutorRegistry | None = None,
        context_driver: ContextDriver | None = None,
        child_runner: Callable[..., Awaitable[TaskResult]] | None = None,
        max_concurrency: int = 2,
        max_global_concurrency: int = 8,
        max_retries: int = 1,
        executor_guard: Callable[[Any, Any], str | None] | None = None,
    ) -> None:
        if max_concurrency < 1 or max_global_concurrency < 1 or max_retries < 0:
            raise ValueError("Scheduler concurrency must be positive and retries non-negative.")
        self.run_manager = run_manager
        self.child_executor = child_executor
        self.child_runner = child_runner or child_executor.execute
        self.agent_registry = agent_registry
        self.context_driver = context_driver or ContextDriver()
        self.max_concurrency = max_concurrency
        self.max_global_concurrency = max_global_concurrency
        self.max_retries = max_retries
        self.executor_guard = executor_guard
        self._parent_locks: dict[str, threading.Lock] = {}
        self._admission_guard = threading.Lock()
        self._global_slots = threading.BoundedSemaphore(max_global_concurrency)
        self._session_slots: dict[str, threading.BoundedSemaphore] = {}

    def _resolved_executor(self, step: PlanStep) -> tuple[Any | None, Any]:
        if self.agent_registry is not None:
            resolved = self.agent_registry.resolve(step.agent_id, step.agent_version)
            self._resolved_profile(step)
            return resolved
        if step.agent_id != GENERAL_AGENT_ID:
            raise ValueError(f"Agent is unavailable: {step.agent_id}")
        if step.inference_profile_id is not None:
            raise ValueError("Inference profiles are unavailable without an Agent registry.")
        return None, self.child_executor

    def _resolved_profile(self, step: PlanStep):
        if step.inference_profile_id is None:
            if any((
                step.inference_client_name is not None,
                step.inference_model is not None,
                step.inference_reasoning_effort is not None,
            )):
                raise ValueError("Frozen inference values require a profile ID.")
            return None
        if self.agent_registry is None:
            raise ValueError("Inference profiles are unavailable without an Agent registry.")
        profile = self.agent_registry.resolve_inference_profile(step.inference_profile_id)
        has_frozen_values = any((
            step.inference_client_name is not None,
            step.inference_model is not None,
            step.inference_reasoning_effort is not None,
        ))
        frozen_values = (
            step.inference_client_name,
            step.inference_model,
            step.inference_reasoning_effort,
        )
        configured_values = (
            profile.client_name,
            profile.model,
            profile.reasoning_effort,
        )
        if has_frozen_values and (
            step.inference_client_name is None
            or step.inference_model is None
            or frozen_values != configured_values
        ):
            raise ValueError(
                f"Inference profile changed after PlanStep was frozen: {step.inference_profile_id}"
            )
        return profile

    def _executor_for(self, step: PlanStep) -> Any:
        return self._resolved_executor(step)[1]

    async def execute_plan_async(
        self,
        parent_run_id: str,
        *,
        context: SchedulerContext,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
    ) -> MultiAgentScheduleResult:
        """Run all currently unblocked steps until waiting, failure, or completion.

        Calling this again is the resume operation: completed child runs are
        rehydrated from their durable snapshot/result and waiting children are
        left untouched until their own Agent run has resumed.
        """

        lock = self._parent_locks.setdefault(parent_run_id, threading.Lock())
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            return await self._execute_locked(
                parent_run_id,
                context=context,
                llm_client_name=llm_client_name,
                llm_model=llm_model,
            )
        finally:
            lock.release()

    def cancel_child_run(self, *, parent_run_id: str, child_run_id: str) -> Any:
        """Cancel one owned child run and its descendants, if it is still active."""
        parent = self.run_manager.get_run(parent_run_id)
        if parent is None:
            raise KeyError(f"Agent run not found: {parent_run_id}")
        if child_run_id not in parent.child_run_ids:
            raise ValueError("Child run does not belong directly to the supplied parent.")
        child = self.run_manager.get_run(child_run_id)
        if child is None or child.parent_run_id != parent_run_id:
            raise ValueError("Child run ownership does not match the supplied parent.")
        if child.status not in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING, AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}:
            return child
        return self.run_manager.cancel_run(child_run_id, reason="Child run cancelled by user.")

    async def retry_child_run_async(
        self,
        *,
        parent_run_id: str,
        child_run_id: str,
        context: SchedulerContext,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
    ) -> MultiAgentScheduleResult:
        """Manually retry one failed, directly-owned latest child attempt."""
        lock = self._parent_locks.setdefault(parent_run_id, threading.Lock())
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            parent = self.run_manager.get_run(parent_run_id)
            child = self.run_manager.get_run(child_run_id)
            if parent is None or child is None:
                raise KeyError("Parent or child Agent run not found.")
            if parent.status != AgentRunStatus.RUNNING:
                raise ValueError("Manual retry requires a running parent Agent run.")
            if child_run_id not in parent.child_run_ids or child.parent_run_id != parent_run_id:
                raise ValueError("Child run does not belong directly to the supplied parent.")
            if child.status not in {AgentRunStatus.FAILED, AgentRunStatus.TIMED_OUT}:
                raise ValueError("Only failed or timed-out child runs can be retried.")
            if any(
                active is not None
                and active.plan_id == child.plan_id
                and active.step_id == child.step_id
                and active.status in {
                    AgentRunStatus.QUEUED,
                    AgentRunStatus.RUNNING,
                    AgentRunStatus.WAITING_CONFIRMATION,
                    AgentRunStatus.WAITING_USER,
                }
                for active in (self.run_manager.get_run(run_id) for run_id in parent.child_run_ids)
            ):
                raise ValueError("This plan step already has an active child attempt.")
            raw_plan = parent.metadata.get("multi_agent_plan")
            if not isinstance(raw_plan, dict):
                raise TypeError("Parent run has no persisted validated multi-Agent plan.")
            plan = Plan.model_validate(raw_plan)
            if plan.status not in {PlanStatus.RUNNING, PlanStatus.REPLANNING}:
                raise ValueError("Manual retry requires a running multi-Agent plan.")
            target = next((step for step in plan.steps if step.step_id == child.step_id), None)
            if target is None or target.status not in {PlanStepStatus.FAILED, PlanStepStatus.BLOCKED}:
                raise ValueError("Child plan step is not in a retryable failed state.")
            attempts = [
                self.run_manager.get_run(run_id)
                for run_id in parent.child_run_ids
            ]
            latest = max(
                (item for item in attempts if item is not None and item.plan_id == child.plan_id and item.step_id == child.step_id),
                key=lambda item: item.attempt or 0,
                default=None,
            )
            if latest is None or latest.run_id != child_run_id:
                raise ValueError("Only the latest child attempt can be manually retried.")

            reset_ids = {target.step_id}
            changed = True
            while changed:
                changed = False
                for step in plan.steps:
                    if step.status != PlanStepStatus.BLOCKED or step.step_id in reset_ids:
                        continue
                    if any(dependency in reset_ids for dependency in step.depends_on) and all(
                        dependency in reset_ids
                        or next(item for item in plan.steps if item.step_id == dependency).status
                        in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}
                        for dependency in step.depends_on
                    ):
                        reset_ids.add(step.step_id)
                        changed = True
            plan = Plan.model_validate(
                {
                    **plan.model_dump(mode="json"),
                    "steps": [
                        {
                            **step.model_dump(mode="json"),
                            "status": PlanStepStatus.PENDING.value,
                        }
                        if step.step_id in reset_ids
                        else step.model_dump(mode="json")
                        for step in plan.steps
                    ],
                }
            )
            self._save_plan(
                parent_run_id,
                plan,
                "subtask_manual_retry_requested",
                {"child_run_id": child_run_id, "step_id": child.step_id, "reset_step_ids": sorted(reset_ids)},
            )
            return await self._execute_locked(
                parent_run_id,
                context=context,
                llm_client_name=llm_client_name,
                llm_model=llm_model,
                force_new_step_ids=frozenset(reset_ids),
            )
        finally:
            lock.release()

    async def _execute_locked(
        self,
        parent_run_id: str,
        *,
        context: SchedulerContext,
        llm_client_name: str | None,
        llm_model: str | None,
        force_new_step_ids: frozenset[str] = frozenset(),
    ) -> MultiAgentScheduleResult:
        parent = self.run_manager.get_run(parent_run_id)
        if parent is None:
            raise KeyError(f"Agent run not found: {parent_run_id}")
        if parent.status not in {AgentRunStatus.RUNNING, AgentRunStatus.WAITING_CONFIRMATION}:
            raise ValueError("Multi-Agent scheduling requires an active parent run.")
        raw_plan = parent.metadata.get("multi_agent_plan")
        if not isinstance(raw_plan, dict):
            raise TypeError("Parent run has no persisted validated multi-Agent plan.")
        plan = Plan.model_validate(raw_plan)
        if plan.parent_run_id != parent_run_id or plan.session_id != parent.session_id:
            raise ValueError("Persisted plan does not belong to this parent run.")
        if plan.status not in {PlanStatus.RUNNING, PlanStatus.REPLANNING}:
            raise ValueError(f"Plan is not schedulable from status {plan.status.value}.")
        if plan.patch_history:
            policy = context.fork_policy or ForkPolicy(
                max_depth=max((step.fork_depth or 1 for step in plan.steps), default=1),
                max_children=max(1, sum(step.fork_operation_id is not None for step in plan.steps) + 1),
                max_fork_size=max(1, len(plan.steps)),
                allowed_scope=context.policy_scope,
            )
            replay_plan_patch_history(
                plan.patch_history,
                PlanPatchContext(
                    fork_policy=policy,
                    parent_effective_scope=context.parent_effective_scope,
                    session_scope=context.session_scope,
                    workspace_scope=context.workspace_scope,
                    max_retries_per_step=self.max_retries,
                ),
            )
        if plan.status == PlanStatus.REPLANNING:
            plan = plan.transition_to(PlanStatus.RUNNING)
            self._save_plan(parent_run_id, plan, "multi_agent_plan_resumed", {})

        result_by_step = self._restore_results(parent_run_id)
        produced: list[TaskResult] = []
        while True:
            parent = self.run_manager.get_run(parent_run_id)
            if parent is None:
                raise KeyError(f"Agent run not found: {parent_run_id}")
            if parent.status == AgentRunStatus.CANCELLED or self.run_manager.is_cancel_requested(parent_run_id):
                self._cancel_children(parent_run_id)
                plan = self._cancel_unfinished_steps(parent_run_id, plan)
                self._save_plan(parent_run_id, plan, "multi_agent_cancelled", {})
                return self._result(plan, produced, status="cancelled")
            if parent.status not in {AgentRunStatus.RUNNING, AgentRunStatus.WAITING_CONFIRMATION}:
                raise ValueError("Parent run stopped while multi-Agent plan was active.")

            waiting = self._waiting_children(parent_run_id, plan)
            if waiting:
                waiting_for_user = [
                    (step, child) for step, child in waiting
                    if child.status == AgentRunStatus.WAITING_USER
                ]
                if waiting_for_user:
                    for step, child in waiting_for_user:
                        result_by_step[step.step_id] = self._result_from_saved_child(child, step)
                        self._save_result(parent_run_id, result_by_step[step.step_id])
                        plan = self._set_step_status(plan, step.step_id, PlanStepStatus.WAITING)
                        pending = child.metadata.get("pending_user_question")
                        self._append_step_event(
                            parent_run_id,
                            "subtask_waiting_user",
                            step,
                            child.run_id,
                            int(child.attempt or 1),
                            {"question_id": pending.get("question_id") if isinstance(pending, dict) else None},
                        )
                    self._save_plan(parent_run_id, plan, "multi_agent_waiting_user", {})
                    self.run_manager.mark_waiting_for_child_user(
                        parent_run_id,
                        child_run_ids=tuple(child.run_id for _, child in waiting_for_user),
                    )
                    return self._result(
                        plan,
                        produced + [result_by_step[step.step_id] for step, _ in waiting_for_user],
                        status="waiting_user",
                        waiting_ids=tuple(child.run_id for _, child in waiting_for_user),
                    )
                for step, child in waiting:
                    task_result = self._result_from_saved_child(child, step)
                    result_by_step[step.step_id] = task_result
                    self._save_result(parent_run_id, task_result)
                    plan = self._set_step_status(plan, step.step_id, PlanStepStatus.WAITING)
                    self._append_step_event(
                        parent_run_id, "subtask_waiting_confirmation", step,
                        child.run_id, int(child.attempt or 1),
                        {"confirmation_id": child.metadata.get("confirmation_id")},
                    )
                self._save_plan(parent_run_id, plan, "multi_agent_waiting_confirmation", {})
                return self._result(
                    plan, produced + [result_by_step[step.step_id] for step, _ in waiting],
                    status="waiting_confirmation",
                    waiting_ids=tuple(child.run_id for _, child in waiting),
                )

            plan, resumed_results = await self._reconcile_waiting_steps(
                parent_run_id, plan
            )
            if resumed_results:
                result_by_step.update({result.step_id: result for result in resumed_results})
                produced.extend(resumed_results)
                for result in resumed_results:
                    self._save_result(parent_run_id, result)
                self._save_plan(parent_run_id, plan, "multi_agent_schedule_progress", {})

            plan = self._block_failed_dependencies(plan)

            ready_ids = plan.ready_step_ids()
            if not ready_ids:
                unresolved = [
                    step for step in plan.steps
                    if step.step_id != "root_coordinator"
                    and step.status not in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}
                ]
                if unresolved:
                    failed = tuple(
                        step.step_id for step in unresolved
                        if step.status in {PlanStepStatus.FAILED, PlanStepStatus.BLOCKED, PlanStepStatus.CANCELLED}
                    )
                    status = "failed" if failed else "running"
                    self._save_plan(parent_run_id, plan, "multi_agent_schedule_progress", {"status": status})
                    return self._result(
                        plan,
                        self._authoritative_results(plan, result_by_step),
                        status=status,
                        failed_ids=failed,
                    )
                cancelled = any(step.status == PlanStepStatus.CANCELLED for step in plan.steps)
                status = "cancelled" if cancelled else "completed"
                self._save_plan(parent_run_id, plan, "multi_agent_schedule_progress", {"status": status})
                return self._result(
                    plan, self._authoritative_results(plan, result_by_step), status=status,
                    history=self._restore_attempts(parent_run_id),
                )

            batch = [next(step for step in plan.steps if step.step_id == step_id) for step_id in ready_ids]
            batch = batch[: self.max_concurrency]
            prepared: list[tuple[PlanStep, Any, Any]] = []
            for step in batch:
                plan = self._set_step_status(plan, step.step_id, PlanStepStatus.READY)
                try:
                    definition, _ = self._resolved_executor(step)
                    if step.inference_profile_id is not None:
                        allowed_profiles = (
                            context.fork_policy.allowed_inference_profile_ids
                            if context.fork_policy is not None
                            else ()
                        )
                        if step.inference_profile_id not in allowed_profiles:
                            raise ValueError(
                                f"Inference profile is outside the current policy: {step.inference_profile_id}"
                            )
                        profile = self._resolved_profile(step)
                        if profile is not None and step.inference_client_name is None:
                            step = step.model_copy(update={
                                "inference_client_name": profile.client_name,
                                "inference_model": profile.model,
                                "inference_reasoning_effort": profile.reasoning_effort,
                            })
                            plan = plan.model_copy(update={"steps": tuple(
                                step if item.step_id == step.step_id else item
                                for item in plan.steps
                            )})
                            self._save_plan(
                                parent_run_id,
                                plan,
                                "subtask_inference_profile_frozen",
                                {
                                    "step_id": step.step_id,
                                    "profile_id": profile.profile_id,
                                    "selection_source": "server_profile",
                                },
                            )
                except ValueError as exc:
                    plan = self._set_step_status(plan, step.step_id, PlanStepStatus.BLOCKED)
                    self._save_plan(parent_run_id, plan, "subtask_agent_rejected", {
                        "step_id": step.step_id,
                        "agent_id": step.agent_id,
                        "reason": str(exc),
                    })
                    continue
                if definition is not None and step.agent_version is None:
                    step = next(item for item in plan.steps if item.step_id == step.step_id)
                    step = step.model_copy(update={"agent_version": definition.version})
                    plan = plan.model_copy(update={"steps": tuple(
                        step if item.step_id == step.step_id else item for item in plan.steps
                    )})
                plan = self._set_step_status(plan, step.step_id, PlanStepStatus.RUNNING)
                attempt, child, snapshot, views = await self._prepare_attempt(
                    parent_run_id,
                    parent,
                    plan,
                    step,
                    context,
                    result_by_step,
                    force_new_attempt=step.step_id in force_new_step_ids,
                    request_llm_client_name=llm_client_name,
                    request_llm_model=llm_model,
                )
                self._save_plan(parent_run_id, plan, "subtask_started", {
                    "step_id": step.step_id,
                    "child_run_id": child.run_id,
                    "attempt": attempt,
                })
                prepared.append((step, child, (attempt, snapshot, views)))

            if not prepared:
                continue

            async def run_one(
                step: PlanStep,
                child: Any,
                prepared_values: tuple[Any, Any, Any],
                _session_id: str = parent.session_id,
            ):
                attempt, snapshot, views = prepared_values
                if snapshot is None or views is None:
                    return attempt, self._blocked_result(child, step, "Context snapshot could not be derived.")
                with self._admission_guard:
                    session_slot = self._session_slots.setdefault(
                        _session_id, threading.BoundedSemaphore(self.max_concurrency)
                    )
                acquired_global = False
                acquired_session = False
                try:
                    while not self._global_slots.acquire(blocking=False):
                        self._assert_parent_active(parent_run_id)
                        await asyncio.sleep(0.05)
                    acquired_global = True
                    while not session_slot.acquire(blocking=False):
                        self._assert_parent_active(parent_run_id)
                        await asyncio.sleep(0.05)
                    acquired_session = True
                    definition, _ = self._resolved_executor(step)
                    denial = self.executor_guard(child, definition) if self.executor_guard else None
                    if denial:
                        self.run_manager.fail_child_run(child.run_id, error_type="source_constraint", error=denial)
                        return attempt, self._blocked_result(child, step, denial, code="source_constraint")
                    executor = self._executor_for(step.model_copy(update={"agent_version": snapshot.agent_version}))
                    if child.status == AgentRunStatus.RUNNING:
                        resume = getattr(executor, "resume", None)
                        if callable(resume):
                            await resume(child.run_id)
                        else:
                            runner_resume = getattr(getattr(executor, "runner", None), "resume_async", None)
                            if callable(runner_resume):
                                await runner_resume(child.run_id)
                    timeout_seconds = snapshot.budget.max_wall_time_seconds
                    run_child = executor.execute if self.agent_registry is not None else self.child_runner
                    runner = run_child(
                        child_run_id=child.run_id,
                        snapshot=snapshot,
                        views=views,
                        llm_client_name=llm_client_name,
                        llm_model=llm_model,
                    )
                    result = (
                        await asyncio.wait_for(runner, timeout=timeout_seconds)
                        if timeout_seconds is not None
                        else await runner
                    )
                except TimeoutError:
                    self.run_manager.timeout_child_run(
                        child.run_id,
                        error=f"Child Agent exceeded its {snapshot.budget.max_wall_time_seconds}s wall-time budget.",
                    )
                    current = self.run_manager.get_run(child.run_id)
                    from_terminal = getattr(executor, "from_terminal", None)
                    if from_terminal is None:
                        from_terminal = getattr(executor, "_from_terminal", None)
                    if callable(from_terminal) and current is not None:
                        result = from_terminal(current, snapshot)
                    else:
                        result = self._blocked_result(
                            child,
                            step,
                            f"Child Agent exceeded its {snapshot.budget.max_wall_time_seconds}s wall-time budget.",
                            code="timeout",
                        )
                    if result.failure is not None:
                        result = result.model_copy(
                            update={"failure": result.failure.model_copy(update={"retryable": False})}
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - persist child-level execution failure.
                    current = self.run_manager.get_run(child.run_id)
                    if current and current.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING}:
                        self.run_manager.fail_child_run(child.run_id, error_type=type(exc).__name__, error=str(exc))
                    result = self._blocked_result(child, step, str(exc), code=type(exc).__name__)
                finally:
                    if acquired_session:
                        session_slot.release()
                    if acquired_global:
                        self._global_slots.release()
                return attempt, result.model_copy(update={"attempt": attempt})

            tasks = [asyncio.create_task(run_one(step, child, values)) for step, child, values in prepared]
            try:
                outcomes = await self._gather_with_parent_cancellation(tasks, parent_run_id)
            except asyncio.CancelledError:
                self._cancel_children(parent_run_id)
                raise

            waiting_pairs: list[tuple[PlanStep, Any]] = []
            waiting_user_pairs: list[tuple[PlanStep, Any]] = []
            for (step, child, _), (attempt, result) in zip(prepared, outcomes):
                if result.failure and result.failure.code == "waiting_user":
                    plan = self._set_step_status(plan, step.step_id, PlanStepStatus.WAITING)
                    waiting_user_pairs.append((step, child))
                    result_by_step[step.step_id] = result
                    self._save_result(parent_run_id, result)
                    continue
                if result.failure and result.failure.code == "waiting_confirmation":
                    plan = self._set_step_status(plan, step.step_id, PlanStepStatus.WAITING)
                    waiting_pairs.append((step, child))
                    result_by_step[step.step_id] = result
                    self._save_result(parent_run_id, result)
                    continue
                if result.status == TaskResultStatus.COMPLETED:
                    plan = self._set_step_status(plan, step.step_id, PlanStepStatus.COMPLETED)
                    result_by_step[step.step_id] = result
                    produced.append(result)
                    self._save_result(parent_run_id, result)
                    continue
                if result.status == TaskResultStatus.CANCELLED:
                    plan = self._set_step_status(plan, step.step_id, PlanStepStatus.CANCELLED)
                    result_by_step[step.step_id] = result
                    produced.append(result)
                    self._save_result(parent_run_id, result)
                    continue
                if result.failure is not None and result.failure.retryable and attempt <= self.max_retries:
                    plan = self._retry_step(plan, step.step_id)
                    self._append_step_event(
                        parent_run_id, "subtask_retry_scheduled", step, child.run_id, attempt,
                        {"next_attempt": attempt + 1, "failure": result.failure.model_dump(mode="json")},
                    )
                    continue
                plan = self._set_step_status(plan, step.step_id, PlanStepStatus.FAILED)
                result_by_step[step.step_id] = result
                produced.append(result)
                self._save_result(parent_run_id, result)

            plan = self._block_failed_dependencies(plan)
            self._save_plan(parent_run_id, plan, "multi_agent_schedule_progress", {})
            if waiting_pairs:
                return self._result(
                    plan,
                    produced + [result_by_step[step.step_id] for step, _ in waiting_pairs],
                    status="waiting_confirmation",
                    waiting_ids=tuple(child.run_id for _, child in waiting_pairs),
                )
            if waiting_user_pairs:
                return self._result(
                    plan,
                    produced + [result_by_step[step.step_id] for step, _ in waiting_user_pairs],
                    status="waiting_user",
                    waiting_ids=tuple(child.run_id for _, child in waiting_user_pairs),
                )
            # Keep scheduling unrelated ready steps after a failure. The next
            # iteration will terminate once all remaining work is completed or
            # blocked by failed dependencies, preserving a complete partial result.

    async def _prepare_attempt(
        self,
        parent_run_id: str,
        parent: Any,
        plan: Plan,
        step: PlanStep,
        context: SchedulerContext,
        result_by_step: dict[str, TaskResult],
        *,
        force_new_attempt: bool = False,
        request_llm_client_name: str | None = None,
        request_llm_model: str | None = None,
    ) -> tuple[int, Any, Any, Any]:
        definition, _ = self._resolved_executor(step)
        inference_profile = self._resolved_profile(step)
        resolved_version = definition.version if definition is not None else step.agent_version
        step = step.model_copy(update={"agent_version": resolved_version})
        previous = [
            self.run_manager.get_run(child_id)
            for child_id in parent.child_run_ids
        ]
        previous = [
            child for child in previous
            if child is not None and child.plan_id == plan.plan_id and child.step_id == step.step_id
        ]
        previous.sort(key=lambda item: item.attempt or 0)
        if previous:
            latest = previous[-1]
            if latest.metadata.get("agent_id", GENERAL_AGENT_ID) != step.agent_id:
                raise ValueError("Persisted Child Run Agent does not match its PlanStep.")
            if latest.metadata.get("agent_version") not in {None, resolved_version}:
                raise ValueError("Persisted Child Run Agent version is unavailable.")
            if latest.status in {AgentRunStatus.COMPLETED, AgentRunStatus.CANCELLED}:
                force_new_attempt = force_new_attempt or self._has_unconsumed_recovery_patch(
                    parent_run_id, plan, step.step_id, latest.run_id,
                )
            if latest.status in {AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}:
                return int(latest.attempt or 1), latest, None, None
            if latest.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING} or (
                latest.status in {AgentRunStatus.COMPLETED, AgentRunStatus.CANCELLED}
                and not force_new_attempt
            ):
                saved = latest.metadata
                snapshot_data = saved.get("context_snapshot")
                views_data = saved.get("context_views")
                if isinstance(snapshot_data, dict) and isinstance(views_data, dict):
                    from app.core.context_driver import ContextViews
                    from app.core.multi_agent import ContextSnapshot

                    snapshot = ContextSnapshot.model_validate(snapshot_data)
                    views = ContextViews.model_validate(views_data)
                    return int(latest.attempt or 1), latest, snapshot, views
                if latest.status in {AgentRunStatus.COMPLETED, AgentRunStatus.CANCELLED}:
                    return int(latest.attempt or 1), latest, None, None
                if latest.status == AgentRunStatus.RUNNING:
                    self.run_manager.fail_child_run(
                        latest.run_id,
                        error_type="context",
                        error="Running child is missing its persisted immutable context.",
                    )
                    return int(latest.attempt or 1), latest, None, None
                # A crash can occur after child creation but before snapshot
                # attachment. Reuse this queued attempt and derive its context.
                child = latest
                next_attempt = int(latest.attempt or 1)
            else:
                next_attempt = int(latest.attempt or 1) + 1
        else:
            next_attempt = 1
        if force_new_attempt or not previous or previous[-1].status not in {
            AgentRunStatus.QUEUED,
            AgentRunStatus.RUNNING,
            AgentRunStatus.COMPLETED,
            AgentRunStatus.CANCELLED,
        }:
            child = self.run_manager.create_child_run(
                parent_run_id=parent_run_id,
                plan_id=plan.plan_id,
                step_id=step.step_id,
                attempt=next_attempt,
                user_input=step.objective,
                agent_id=step.agent_id,
                agent_version=resolved_version,
                executor_kind=definition.executor_kind if definition is not None else "react",
            )
        dependencies = tuple(result_by_step[dep] for dep in step.depends_on if dep in result_by_step)
        parent_snapshot_id = None
        parent_snapshot_data = parent.metadata.get("context_snapshot")
        if isinstance(parent_snapshot_data, dict):
            snapshot_id = parent_snapshot_data.get("snapshot_id")
            if isinstance(snapshot_id, str) and snapshot_id:
                parent_snapshot_id = snapshot_id
        derived = await self.context_driver.derive(
            ContextRequest(
                snapshot_id=_stable_id("snapshot", child.run_id),
                parent_snapshot_id=parent_snapshot_id,
                child_run_id=child.run_id,
                parent_run_id=parent_run_id,
                session_id=child.session_id,
                plan_id=plan.plan_id,
                plan_step=self._execution_step(step, definition),
                inference_profile=inference_profile,
                request_llm_client_name=request_llm_client_name,
                request_llm_model=request_llm_model,
                dependency_results=dependencies,
                parent_effective_scope=context.parent_effective_scope,
                session_scope=context.session_scope,
                workspace_scope=context.workspace_scope,
                policy_scope=context.policy_scope,
                budget=context.budget,
                policy_version=context.policy_version,
                workspace_version=context.workspace_version,
                permission_version=context.permission_version,
                evidence_candidates=context.evidence_candidates,
                memory_candidates=context.memory_candidates,
                view_mode=context.view_mode,
            )
        )
        if derived.snapshot is None or derived.views is None:
            message = derived.reason or derived.status.value
            self.run_manager.fail_child_run(child.run_id, error_type="context", error=message)
            return next_attempt, child, None, None
        return next_attempt, child, derived.snapshot, derived.views

    def _has_unconsumed_recovery_patch(
        self, parent_run_id: str, plan: Plan, step_id: str, child_run_id: str,
    ) -> bool:
        """A validated reset after this attempt's creation requires fresh context.

        Use the durable parent journal rather than answer text or wall-clock
        ordering. Child creation is atomic with its journal entry, so a queued
        replacement consumes the patch even if context attachment is interrupted.
        Ordinary resume and rejected/other-step patches do not create attempts.
        """
        recovery_ids = {
            record.patch.patch_id for record in plan.patch_history
            if record.patch.target_step_id == step_id
            and record.patch.operation in {
                PlanPatchOperation.RETRY_STEP, PlanPatchOperation.REDUCED_SCOPE,
            }
        }
        if not recovery_ids:
            return False
        for event in reversed(self.run_manager.list_events(parent_run_id)):
            if event.type == "subtask_created" and event.child_run_id == child_run_id:
                return False
            if event.type == "multi_agent_plan_patched" and event.payload.get("plan_id") == plan.plan_id:
                patch = event.payload.get("patch")
                if isinstance(patch, dict) and patch.get("patch_id") in recovery_ids:
                    return True
        return False

    @staticmethod
    def _execution_step(step: PlanStep, definition: Any | None) -> PlanStep:
        """Narrow a validated step to its registered executor's resource model.

        The persisted PlanStep remains the audit record of the upper bound.
        This projection can remove grants but must never add one.
        """
        if definition is None or definition.scope_mode != "workspace_sandbox":
            return step
        scope = step.effective_scope or ScopeGrant(
            allowed_packages=step.allowed_packages,
            allowed_tools=step.allowed_tools,
            side_effect_level=step.side_effect_level,
        )
        level = (
            SideEffectLevel.WRITE
            if scope.side_effect_level == SideEffectLevel.EXTERNAL
            else scope.side_effect_level
        )
        projected = scope.model_copy(update={
            "source_ids": (),
            "account_ids": (),
            "allowed_packages": (),
            "allowed_tools": (),
            "side_effect_level": level,
        })
        return step.model_copy(update={
            "allowed_packages": (),
            "allowed_tools": (),
            "effective_scope": projected,
            "side_effect_level": level,
        })

    async def _gather_with_parent_cancellation(self, tasks: list[asyncio.Task], parent_run_id: str):
        pending = set(tasks)
        results: dict[asyncio.Task, Any] = {}
        while pending:
            parent = self.run_manager.get_run(parent_run_id)
            if parent is None or parent.status == AgentRunStatus.CANCELLED or self.run_manager.is_cancel_requested(parent_run_id):
                self._cancel_children(parent_run_id)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise asyncio.CancelledError("Parent Agent run cancelled.")
            done, pending = await asyncio.wait(pending, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                results[task] = task.result()
        return [results[task] for task in tasks]

    def _assert_parent_active(self, parent_run_id: str) -> None:
        parent = self.run_manager.get_run(parent_run_id)
        if (
            parent is None
            or parent.status == AgentRunStatus.CANCELLED
            or self.run_manager.is_cancel_requested(parent_run_id)
        ):
            raise asyncio.CancelledError

    def _waiting_children(self, parent_run_id: str, plan: Plan) -> list[tuple[PlanStep, Any]]:
        parent = self.run_manager.get_run(parent_run_id)
        if parent is None:
            return []
        waiting = []
        for step in plan.steps:
            if step.status not in {PlanStepStatus.RUNNING, PlanStepStatus.WAITING}:
                continue
            for child_id in parent.child_run_ids:
                child = self.run_manager.get_run(child_id)
                if child and child.plan_id == plan.plan_id and child.step_id == step.step_id and child.status in {AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}:
                    waiting.append((step, child))
        return waiting

    def _result_from_saved_child(self, child: Any, step: PlanStep) -> TaskResult:
        from app.core.multi_agent import ContextSnapshot

        snapshot_data = child.metadata.get("context_snapshot")
        if snapshot_data:
            snapshot_id = ContextSnapshot.model_validate(snapshot_data).snapshot_id
        else:
            snapshot_id = "pending_snapshot"
        review_id = child.metadata.get("confirmation_id")
        from app.core.multi_agent import FailureDetail

        return TaskResult(
            correlation_id=child.trace_id,
            result_id=_stable_id("result", child.run_id),
            attempt=int(child.attempt or 1),
            child_run_id=child.run_id,
            plan_id=child.plan_id,
            step_id=step.step_id,
            snapshot_id=snapshot_id,
            status=TaskResultStatus.BLOCKED,
            summary=(
                "Child Agent is waiting for a user response."
                if child.status == AgentRunStatus.WAITING_USER
                else "Child Agent is waiting for user confirmation."
            ),
            failure=FailureDetail(
                category="user_input" if child.status == AgentRunStatus.WAITING_USER else "safety",
                code="waiting_user" if child.status == AgentRunStatus.WAITING_USER else "waiting_confirmation",
                message=(
                    (child.metadata.get("pending_user_question") or {}).get("question")
                    or "Child Agent is waiting for a user response."
                    if child.status == AgentRunStatus.WAITING_USER
                    else "Child Agent is waiting for user confirmation."
                ),
                recommended_actions=(
                    ("Answer the pending question, then continue the child run.",)
                    if child.status == AgentRunStatus.WAITING_USER
                    else ((f"Resolve confirmation {review_id}.",) if review_id else ("Resolve the pending safety review.",))
                ),
            ),
        )

    def _blocked_result(self, child: Any, step: PlanStep, message: str, *, code: str = "context") -> TaskResult:
        from app.core.multi_agent import FailureDetail

        snapshot_id = child.metadata.get("context_snapshot", {}).get("snapshot_id", "unavailable")
        return TaskResult(
            correlation_id=child.trace_id,
            result_id=_stable_id("result", child.run_id),
            attempt=int(child.attempt or 1),
            child_run_id=child.run_id,
            plan_id=child.plan_id,
            step_id=step.step_id,
            snapshot_id=snapshot_id,
            status=TaskResultStatus.BLOCKED,
            summary=message,
            failure=FailureDetail(category="agent_execution", code=code, message=message, retryable=False),
        )

    def _save_result(self, parent_run_id: str, result: TaskResult) -> None:
        parent = self.run_manager.get_run(parent_run_id)
        if parent is None:
            return
        raw_plan = parent.metadata.get("multi_agent_plan", {})
        plan_id = raw_plan.get("plan_id") if isinstance(raw_plan, dict) else None
        self.run_manager.append_event(
            parent_run_id,
            "subtask_result",
            f"Multi-Agent step {result.step_id} result recorded.",
            stage="subtask",
            payload={"task_result": result.model_dump(mode="json")},
            parent_run_id=parent_run_id,
            child_run_id=result.child_run_id,
            plan_id=plan_id,
            step_id=result.step_id,
        )

    def _restore_results(self, parent_run_id: str) -> dict[str, TaskResult]:
        results: dict[str, TaskResult] = {}
        for event in self.run_manager.list_events(parent_run_id):
            if event.type != "subtask_result":
                continue
            value = event.payload.get("task_result")
            if isinstance(value, dict):
                try:
                    result = TaskResult.model_validate(value)
                    results[result.step_id] = result
                except ValidationError:
                    continue
        return results

    def _restore_attempts(self, parent_run_id: str) -> list[TaskResult]:
        attempts: list[TaskResult] = []
        for event in self.run_manager.list_events(parent_run_id):
            if event.type != "subtask_result":
                continue
            value = event.payload.get("task_result")
            if isinstance(value, dict):
                try:
                    result = TaskResult.model_validate(value)
                except ValidationError:
                    continue
                # A safety hold is a non-terminal scheduler observation, not an
                # execution attempt. Keep it in the durable event stream, but do
                # not let it conflict with the child result recorded after the
                # review is approved and the same attempt finishes.
                if result.failure and result.failure.code in {"waiting_confirmation", "waiting_user"}:
                    continue
                attempts.append(result)
        return attempts

    @staticmethod
    def _authoritative_results(
        plan: Plan, result_by_step: dict[str, TaskResult]
    ) -> list[TaskResult]:
        terminal = {
            PlanStepStatus.COMPLETED,
            PlanStepStatus.FAILED,
            PlanStepStatus.BLOCKED,
            PlanStepStatus.CANCELLED,
        }
        return [
            result_by_step[step.step_id]
            for step in plan.steps
            if step.step_id in result_by_step and step.status in terminal
        ]

    async def _reconcile_waiting_steps(self, parent_run_id: str, plan: Plan) -> tuple[Plan, list[TaskResult]]:
        parent = self.run_manager.get_run(parent_run_id)
        if parent is None:
            return plan, []
        resumed: list[TaskResult] = []
        for step in plan.steps:
            if step.status not in {PlanStepStatus.WAITING, PlanStepStatus.RUNNING}:
                continue
            children = [
                self.run_manager.get_run(child_id) for child_id in parent.child_run_ids
            ]
            children = [
                child for child in children
                if child is not None and child.plan_id == plan.plan_id and child.step_id == step.step_id
            ]
            children.sort(key=lambda child: child.attempt or 0)
            child = children[-1] if children else None
            if child is None or child.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING, AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}:
                continue
            snapshot_data = child.metadata.get("context_snapshot")
            views_data = child.metadata.get("context_views")
            if not isinstance(snapshot_data, dict) or not isinstance(views_data, dict):
                result = self._blocked_result(child, step, "Completed child is missing its persisted context.")
            else:
                from app.core.context_driver import ContextViews
                from app.core.multi_agent import ContextSnapshot

                if child.status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT}:
                    executor = self._executor_for(step.model_copy(update={"agent_version": snapshot_data.get("agent_version")}))
                    run_child = executor.execute if self.agent_registry is not None else self.child_runner
                    result = await run_child(
                        child_run_id=child.run_id,
                        snapshot=ContextSnapshot.model_validate(snapshot_data),
                        views=ContextViews.model_validate(views_data),
                    )
                else:
                    continue
            if result.status == TaskResultStatus.COMPLETED:
                plan = self._set_step_status(plan, step.step_id, PlanStepStatus.RUNNING)
                plan = self._set_step_status(plan, step.step_id, PlanStepStatus.COMPLETED)
            elif result.failure is not None and result.failure.retryable and int(child.attempt or 1) <= self.max_retries:
                plan = self._retry_step(plan, step.step_id)
            elif result.status == TaskResultStatus.CANCELLED:
                plan = self._set_step_status(plan, step.step_id, PlanStepStatus.CANCELLED)
            else:
                if step.status == PlanStepStatus.WAITING:
                    plan = self._set_step_status(plan, step.step_id, PlanStepStatus.RUNNING)
                plan = self._set_step_status(plan, step.step_id, PlanStepStatus.FAILED)
            resumed.append(result.model_copy(update={"attempt": int(child.attempt or 1)}))
        return plan, resumed

    def _set_step_status(self, plan: Plan, step_id: str, status: PlanStepStatus) -> Plan:
        step = next(item for item in plan.steps if item.step_id == step_id)
        if step.status == status:
            return plan
        if step.status == PlanStepStatus.WAITING and status == PlanStepStatus.READY:
            return plan.transition_step(step_id, PlanStepStatus.RUNNING)
        return plan.transition_step(step_id, status)

    def _retry_step(self, plan: Plan, step_id: str) -> Plan:
        """Reset a bounded retry to PENDING after recording the failed attempt."""
        step = next(item for item in plan.steps if item.step_id == step_id)
        if step.status == PlanStepStatus.RUNNING:
            # A retry is a scheduler-owned transition; revalidate the complete
            # immutable plan while retaining the attempt history in child runs.
            return Plan.model_validate(
                {
                    **plan.model_dump(mode="json"),
                    "steps": [
                        {
                            **item.model_dump(mode="json"),
                            "status": PlanStepStatus.PENDING.value,
                        }
                        if item.step_id == step_id
                        else item.model_dump(mode="json")
                        for item in plan.steps
                    ],
                }
            )
        if step.status == PlanStepStatus.WAITING:
            return Plan.model_validate(
                {
                    **plan.model_dump(mode="json"),
                    "steps": [
                        {
                            **item.model_dump(mode="json"),
                            "status": PlanStepStatus.PENDING.value,
                        }
                        if item.step_id == step_id
                        else item.model_dump(mode="json")
                        for item in plan.steps
                    ],
                }
            )
        return plan

    def _block_failed_dependencies(self, plan: Plan) -> Plan:
        failed = {step.step_id for step in plan.steps if step.status in {PlanStepStatus.FAILED, PlanStepStatus.BLOCKED, PlanStepStatus.CANCELLED}}
        if not failed:
            return plan
        for step in plan.steps:
            if step.status in {PlanStepStatus.PENDING, PlanStepStatus.BLOCKED} and any(dep in failed for dep in step.depends_on):
                if step.status == PlanStepStatus.PENDING:
                    plan = plan.transition_step(step.step_id, PlanStepStatus.BLOCKED)
                failed.add(step.step_id)
        return plan

    def _save_plan(self, parent_run_id: str, plan: Plan, event_type: str, payload: dict[str, Any]) -> None:
        self.run_manager.record_multi_agent_plan(
            parent_run_id,
            event_type=event_type,
            payload={"plan_id": plan.plan_id, **payload},
            plan=plan.model_dump(mode="json"),
        )

    def _append_step_event(self, parent_run_id: str, event_type: str, step: PlanStep, child_run_id: str, attempt: int, payload: dict[str, Any]) -> None:
        parent = self.run_manager.get_run(parent_run_id)
        plan_id = None
        if parent is not None:
            raw_plan = parent.metadata.get("multi_agent_plan")
            if isinstance(raw_plan, dict):
                plan_id = raw_plan.get("plan_id")
        self.run_manager.append_event(
            parent_run_id,
            event_type,
            f"Multi-Agent step {step.step_id}: {event_type.replace('_', ' ')}.",
            stage="subtask",
            payload={"objective": step.objective, **payload},
            parent_run_id=parent_run_id,
            child_run_id=child_run_id,
            plan_id=plan_id,
            step_id=step.step_id,
            attempt=attempt,
        )

    def _cancel_children(self, parent_run_id: str) -> None:
        parent = self.run_manager.get_run(parent_run_id)
        if parent is None:
            return
        for child_id in parent.child_run_ids:
            child = self.run_manager.get_run(child_id)
            if child and child.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING, AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}:
                self.run_manager.cancel_run(child_id, reason="Parent Agent run cancelled.")

    def _cancel_unfinished_steps(self, parent_run_id: str, plan: Plan) -> Plan:
        for step in plan.steps:
            if step.step_id == "root_coordinator" or step.status in {
                PlanStepStatus.COMPLETED, PlanStepStatus.FAILED,
                PlanStepStatus.SKIPPED, PlanStepStatus.CANCELLED,
            }:
                continue
            plan = self._set_step_status(plan, step.step_id, PlanStepStatus.CANCELLED)
        return plan

    def _child_tree_execution_evidence(
        self,
        root_run_id: str,
        *,
        expected_parent_id: str,
        expected_plan_id: str,
        expected_step_id: str,
        expected_attempt: int,
        expected_trace_id: str,
    ) -> tuple[bool | None, ConfirmationState]:
        """Combine a coordinator child and every descendant audit conservatively."""

        observed: list[tuple[bool | None, Any]] = []
        visited: set[str] = set()
        malformed_tree = False

        def visit(
            run_id: str,
            expected_parent_id: str | None = None,
            *,
            root: bool = False,
        ) -> None:
            nonlocal malformed_tree
            if run_id in visited:
                malformed_tree = True
                return
            visited.add(run_id)
            child = self.run_manager.get_run(run_id)
            if child is None or (
                expected_parent_id is not None and child.parent_run_id != expected_parent_id
            ) or child.trace_id != expected_trace_id:
                malformed_tree = True
                return
            if not root:
                parent = self.run_manager.get_run(child.parent_run_id)
                parent_plan = parent.metadata.get("multi_agent_plan") if parent else None
                if (
                    parent is None
                    or parent.trace_id != child.trace_id
                    or not isinstance(parent_plan, dict)
                    or parent_plan.get("plan_id") != child.plan_id
                ):
                    malformed_tree = True
                    return
            snapshot = child.metadata.get("context_snapshot")
            if not isinstance(snapshot, dict) or any(
                snapshot.get(field) != value
                for field, value in (
                    ("child_run_id", child.run_id),
                    ("parent_run_id", child.parent_run_id),
                    ("plan_id", child.plan_id),
                    ("step_id", child.step_id),
                    ("session_id", child.session_id),
                )
            ):
                malformed_tree = True
                return
            if root and (
                child.parent_run_id != expected_parent_id
                or child.plan_id != expected_plan_id
                or child.step_id != expected_step_id
                or child.attempt != expected_attempt
            ):
                malformed_tree = True
                return
            events = self.run_manager.list_events(run_id)
            audit_values = []
            for event in events:
                if event.type == "child_tool_audit":
                    value = event.payload.get("audit")
                elif event.type == "subtask_result":
                    value = event.payload.get("child_tool_audit")
                else:
                    continue
                if isinstance(value, dict):
                    audit_values.append(value)
            evidence = derive_child_execution_evidence(
                child,
                events,
                tool_audit=(
                    audit_values[0]
                    if len(audit_values) == 1
                    else None
                ),
            )
            observed.append(evidence)
            for descendant_id in child.child_run_ids:
                visit(descendant_id, child.run_id)

        visit(root_run_id, expected_parent_id, root=True)
        if malformed_tree:
            observed.append((None, ConfirmationState.MISSING))
        actual_values = [actual for actual, _ in observed]
        if any(actual is True for actual in actual_values):
            actual: bool | None = True
        elif any(value is None for value in actual_values):
            actual = None
        else:
            actual = False
        if actual is False:
            confirmation = ConfirmationState.NOT_REQUIRED
        elif any(
            value is None or state != ConfirmationState.APPROVED
            for value, state in observed if value is True
        ) or any(value is None for value in actual_values):
            confirmation = ConfirmationState.MISSING
        else:
            confirmation = ConfirmationState.APPROVED
        return actual, confirmation

    def _result(
        self,
        plan: Plan,
        results: list[TaskResult],
        *,
        status: str,
        waiting_ids: tuple[str, ...] = (),
        failed_ids: tuple[str, ...] = (),
        history: list[TaskResult] | None = None,
    ) -> MultiAgentScheduleResult:
        aggregate = None
        verification = None
        replan_required = False
        if status in {"completed", "failed", "cancelled"}:
            attempts = history if history is not None else list(results)
            expected = tuple(
                step.step_id for step in plan.steps
                if step.step_id != "root_coordinator"
            )
            aggregate = aggregate_task_results(
                plan,
                attempts,
                expected_step_ids=expected,
            )
            contract_results: dict[str, bool] = {}
            for result in aggregate.task_results:
                if result.verification is not None:
                    check = next(
                        (item for item in result.verification.checks if item.check_id == "output_contract"),
                        None,
                    )
                    if check is not None:
                        if check.status == VerificationStatus.PASSED:
                            contract_results[result.step_id] = True
                        elif check.status == VerificationStatus.FAILED:
                            contract_results[result.step_id] = False
            actual_side_effects: dict[str, bool] = {}
            confirmations = {}
            attempts_by_step: dict[str, dict[str, int]] = {}
            result_attempt_refs: dict[str, tuple[str, int]] = {}
            for attempt in aggregate.attempt_history:
                if attempt.child_run_id:
                    result_attempt_refs[attempt.child_run_id] = (
                        attempt.step_id,
                        attempt.attempt,
                    )
                    attempts_by_step.setdefault(attempt.step_id, {})[
                        attempt.child_run_id
                    ] = attempt.attempt
            parent = self.run_manager.get_run(plan.parent_run_id)
            parent_plan = parent.metadata.get("multi_agent_plan") if parent is not None else None
            malformed_parent_tree = (
                parent is None
                or parent.session_id != plan.session_id
                or parent.trace_id != plan.correlation_id
                or not isinstance(parent_plan, dict)
                or parent_plan.get("plan_id") != plan.plan_id
            )
            if parent is not None:
                for child_id in parent.child_run_ids:
                    child = self.run_manager.get_run(child_id)
                    if child is None:
                        malformed_parent_tree = True
                        continue
                    if child.plan_id != plan.plan_id:
                        continue
                    if (
                        child.parent_run_id != parent.run_id
                        or child.step_id not in expected
                        or not isinstance(child.attempt, int)
                        or child.attempt < 1
                    ):
                        malformed_parent_tree = True
                        continue
                    result_ref = result_attempt_refs.get(child.run_id)
                    if result_ref is not None and result_ref != (child.step_id, child.attempt):
                        malformed_parent_tree = True
                    attempts_by_step.setdefault(child.step_id, {})[
                        child.run_id
                    ] = child.attempt
            for result in aggregate.task_results:
                evidence = [
                    self._child_tree_execution_evidence(
                        child_run_id,
                        expected_parent_id=plan.parent_run_id,
                        expected_plan_id=plan.plan_id,
                        expected_step_id=result.step_id,
                        expected_attempt=attempt_number,
                        expected_trace_id=plan.correlation_id,
                    )
                    for child_run_id, attempt_number in attempts_by_step.get(
                        result.step_id,
                        {result.child_run_id: result.attempt}
                        if result.child_run_id
                        else {},
                    ).items()
                ]
                if malformed_parent_tree:
                    evidence.append((None, ConfirmationState.MISSING))
                if not evidence:
                    evidence.append((None, ConfirmationState.MISSING))
                values = [actual for actual, _ in evidence]
                if any(value is True for value in values):
                    actual: bool | None = True
                elif any(value is None for value in values):
                    actual = None
                else:
                    actual = False
                if actual is False:
                    confirmation = ConfirmationState.NOT_REQUIRED
                elif any(value is None for value in values) or any(
                    value is True and state != ConfirmationState.APPROVED
                    for value, state in evidence
                ):
                    confirmation = ConfirmationState.MISSING
                else:
                    confirmation = ConfirmationState.APPROVED
                if actual is not None:
                    actual_side_effects[result.step_id] = actual
                confirmations[result.step_id] = confirmation
            verification = verify_aggregate(
                plan,
                aggregate,
                policy=VerificationPolicy(
                    correlation_id=plan.correlation_id,
                    require_side_effect_audit=True,
                ),
                output_contract_results=contract_results,
                confirmation_by_step=confirmations,
                actual_side_effects_by_step=actual_side_effects,
            )
            audit_inconclusive = "actual_side_effects_unknown" in verification.missing_requirements
            replan_required = (
                aggregate.status.value in {"failed", "blocked", "partial", "conflicting"}
                or verification.status == VerificationStatus.FAILED
                or audit_inconclusive
            )
            parent = self.run_manager.get_run(plan.parent_run_id)
            if parent is not None:
                self.run_manager.record_multi_agent_aggregation(
                    plan.parent_run_id,
                    plan=plan.model_dump(mode="json"),
                    aggregate=aggregate.model_dump(mode="json"),
                    verification=verification.model_dump(mode="json"),
                    replan_required=replan_required,
                )
        return MultiAgentScheduleResult(
            status=status,
            plan=plan,
            task_results=tuple(results),
            waiting_child_run_ids=waiting_ids,
            failed_step_ids=failed_ids,
            aggregate=aggregate,
            verification=verification,
            replan_required=replan_required,
        )


def _stable_id(prefix: str, value: str) -> str:
    digest = sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"
