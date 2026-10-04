"""Server-owned child Agent definitions and execution dispatch.

An Agent's fork role is independent of its implementation. The scheduler only
selects registered definitions; executors own their private execution traces.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.context_driver import ContextViews
from app.core.local_config import AgentInferenceProfile
from app.core.multi_agent import (
    GENERAL_AGENT_ID,
    ContextSnapshot,
    FailureDetail,
    TaskResult,
    TaskResultStatus,
)


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    executor_kind: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=800)
    # Server-owned scope projection. External workspace workers do not receive
    # registry tools or data-source/account grants from a general Agent plan.
    scope_mode: Literal["registry_tools", "workspace_sandbox"] = "registry_tools"
    enabled: bool = True
    can_resume: bool = False
    evaluator_id: str | None = None


class ChildExecutor(Protocol):
    async def execute(
        self,
        *,
        child_run_id: str,
        snapshot: ContextSnapshot,
        views: ContextViews,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
    ) -> TaskResult: ...

    async def resume(self, child_run_id: str) -> None: ...

    def from_terminal(self, child: Any, snapshot: ContextSnapshot) -> TaskResult: ...


class GeneralAgentExecutor:
    """Compatibility adapter around the existing ReAct child execution path."""

    def __init__(self, legacy_executor: Any, run_child: Callable[..., Awaitable[TaskResult]] | None = None) -> None:
        self.legacy_executor = legacy_executor
        self.run_child = run_child or legacy_executor.execute

    async def execute(self, **kwargs: Any) -> TaskResult:
        return await self.run_child(**kwargs)

    async def resume(self, child_run_id: str) -> None:
        resume = getattr(self.legacy_executor.runner, "resume_async", None)
        if callable(resume):
            await resume(child_run_id)

    def from_terminal(self, child: Any, snapshot: ContextSnapshot) -> TaskResult:
        return self.legacy_executor._from_terminal(child, snapshot)


class MockWorkflowScenario(str, Enum):
    """Finite, server-selected outcomes for isolated workflow demonstrations."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    WAITING_CONFIRMATION = "waiting_confirmation"
    CANCEL = "cancel"
    TIMEOUT = "timeout"


class MockWorkflowExecutor:
    """Deterministic example of an executor with workflow rather than ReAct steps."""

    def __init__(
        self,
        run_manager: InMemoryAgentRunManager,
        *,
        scenario: MockWorkflowScenario = MockWorkflowScenario.SUCCESS,
    ) -> None:
        self.run_manager = run_manager
        if not isinstance(scenario, MockWorkflowScenario):
            raise TypeError("Mock workflow scenario must be selected by trusted server code.")
        self.scenario = scenario

    async def execute(
        self,
        *,
        child_run_id: str,
        snapshot: ContextSnapshot,
        views: ContextViews,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
    ) -> TaskResult:
        child = self.run_manager.get_run(child_run_id)
        if child is None or child.parent_run_id is None:
            raise ValueError("Mock workflow requires an existing Child Run.")
        if child.status != AgentRunStatus.QUEUED:
            return self.from_terminal(child, snapshot)
        if (
            snapshot.child_run_id != child_run_id
            or snapshot.parent_run_id != child.parent_run_id
            or snapshot.session_id != child.session_id
            or snapshot.plan_id != child.plan_id
            or snapshot.step_id != child.step_id
            or snapshot.agent_id != "mock_workflow"
            or views.tool.snapshot_id != snapshot.snapshot_id
        ):
            raise ValueError("Mock workflow context does not match the Child Run.")
        self.run_manager.attach_child_context(
            child_run_id,
            snapshot=snapshot.model_dump(mode="json"),
            views=views.model_dump(mode="json"),
        )
        if self.run_manager.mark_child_running(child_run_id).status != AgentRunStatus.RUNNING:
            return self.from_terminal(child, snapshot)
        self._append_node_event(child, "prepare", "completed")
        current = self.run_manager.get_run(child_run_id)
        if (
            current is None
            or current.status != AgentRunStatus.RUNNING
            or self.run_manager.is_cancel_requested(child_run_id)
        ):
            return self.from_terminal(child, snapshot)
        if self.scenario == MockWorkflowScenario.WAITING_CONFIRMATION:
            self.run_manager.mark_waiting_confirmation(
                child_run_id,
                confirmation_id=f"mock_confirmation_{child_run_id}",
            )
            self._append_event(
                child,
                "expert.workflow.approval_waiting",
                "Mock workflow is waiting for confirmation.",
                payload={"agent_id": "mock_workflow", "node": "work", "schema_version": 1},
            )
            return self.from_terminal(child, snapshot)
        if self.scenario == MockWorkflowScenario.CANCEL:
            self._append_event(
                child,
                "expert.workflow.cancel_requested",
                "Mock workflow requested cooperative cancellation.",
                payload={"agent_id": "mock_workflow", "node": "work", "schema_version": 1},
            )
            self.run_manager.cancel_run(child_run_id, reason="Mock workflow cancellation scenario.")
            return self.from_terminal(child, snapshot)
        if self.scenario == MockWorkflowScenario.TIMEOUT:
            self._append_event(
                child,
                "expert.workflow.node_timed_out",
                "Mock workflow timed out at the work node.",
                payload={"agent_id": "mock_workflow", "node": "work", "schema_version": 1},
            )
            self.run_manager.timeout_child_run(
                child_run_id,
                error="Mock workflow timeout scenario.",
            )
            return self.from_terminal(child, snapshot)
        if self.scenario == MockWorkflowScenario.FAILURE:
            self._append_node_event(child, "work", "failed")
            self.run_manager.fail_child_run(
                child_run_id,
                error_type="mock_workflow_failure",
                error="Mock workflow failure scenario.",
            )
            return self.from_terminal(child, snapshot)

        for node in ("work", "verify"):
            current = self.run_manager.get_run(child_run_id)
            if current is None or current.status != AgentRunStatus.RUNNING or self.run_manager.is_cancel_requested(child_run_id):
                return self.from_terminal(child, snapshot)
            self._append_node_event(child, node, "completed")
        summary = f"Mock workflow completed: {snapshot.objective}"
        result_snapshot: dict[str, Any] = {"answer": summary}
        if self.scenario == MockWorkflowScenario.PARTIAL:
            result_snapshot.update(
                {
                    "task_result_status": TaskResultStatus.PARTIAL.value,
                    "missing_requirements": ["one optional report section"],
                    "warnings": ["Mock workflow intentionally returned a partial result."],
                }
            )
        self.run_manager.complete_child_run(child_run_id, result_snapshot=result_snapshot)
        return self.from_terminal(child, snapshot)

    def _append_node_event(self, child: Any, node: str, outcome: str) -> None:
        self._append_event(
            child,
            f"expert.workflow.node_{outcome}",
            f"Mock workflow node {node} {outcome}.",
            payload={"agent_id": "mock_workflow", "node": node, "schema_version": 1},
        )

    def _append_event(
        self,
        child: Any,
        event_type: str,
        message: str,
        *,
        payload: dict[str, Any],
    ) -> None:
        self.run_manager.append_event(
            child.run_id,
            event_type,
            message,
            stage="expert_workflow",
            payload=payload,
            parent_run_id=child.parent_run_id,
            child_run_id=child.run_id,
            plan_id=child.plan_id,
            step_id=child.step_id,
            attempt=child.attempt,
        )

    async def resume(self, child_run_id: str) -> None:
        # This deterministic example has no external checkpoint to resume.
        return None

    def from_terminal(self, child: Any, snapshot: ContextSnapshot) -> TaskResult:
        current = self.run_manager.get_run(child.run_id)
        status = current.status if current is not None else AgentRunStatus.FAILED
        mapping = {
            AgentRunStatus.COMPLETED: TaskResultStatus.COMPLETED,
            AgentRunStatus.CANCELLED: TaskResultStatus.CANCELLED,
            AgentRunStatus.TIMED_OUT: TaskResultStatus.TIMED_OUT,
            AgentRunStatus.FAILED: TaskResultStatus.FAILED,
            AgentRunStatus.WAITING_CONFIRMATION: TaskResultStatus.BLOCKED,
            AgentRunStatus.WAITING_USER: TaskResultStatus.BLOCKED,
        }
        result_status = mapping.get(status, TaskResultStatus.BLOCKED)
        result_snapshot = current.result_snapshot if current is not None else None
        if (
            status == AgentRunStatus.COMPLETED
            and isinstance(result_snapshot, dict)
            and result_snapshot.get("task_result_status") == TaskResultStatus.PARTIAL.value
        ):
            result_status = TaskResultStatus.PARTIAL
        failure = None
        if result_status in {TaskResultStatus.FAILED, TaskResultStatus.TIMED_OUT, TaskResultStatus.BLOCKED}:
            failure = FailureDetail(
                category="expert_workflow",
                code=(current.error_type if current else None)
                or ("waiting_confirmation" if status == AgentRunStatus.WAITING_CONFIRMATION else None)
                or ("waiting_user" if status == AgentRunStatus.WAITING_USER else None)
                or status.value,
                message=(current.error if current else None) or f"Mock workflow is {status.value}.",
            )
        summary = (
            str((result_snapshot or {}).get("answer") or "Mock workflow completed.")
            if result_status == TaskResultStatus.COMPLETED
            else (
                str((result_snapshot or {}).get("answer") or "Mock workflow returned a partial result.")
                if result_status == TaskResultStatus.PARTIAL
                else (failure.message if failure else "Mock workflow cancelled.")
            )
        )
        missing_requirements = tuple((result_snapshot or {}).get("missing_requirements", ()))
        warnings = tuple((result_snapshot or {}).get("warnings", ()))
        terminal_at = (
            current.completed_at or current.failed_at or current.cancelled_at
            if current is not None else None
        )
        return TaskResult(
            correlation_id=child.trace_id,
            result_id=f"result_{child.run_id}",
            child_run_id=child.run_id,
            plan_id=child.plan_id or snapshot.plan_id,
            step_id=child.step_id or snapshot.step_id,
            snapshot_id=snapshot.snapshot_id,
            attempt=int(child.attempt or 1),
            status=result_status,
            summary=summary,
            failure=failure,
            missing_requirements=missing_requirements,
            warnings=warnings,
            **({"completed_at": datetime.fromisoformat(terminal_at)} if terminal_at else {}),
        )


class AgentExecutorRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], AgentDefinition] = {}
        self._executors: dict[tuple[str, str], ChildExecutor] = {}
        self._current_versions: dict[str, str] = {}
        self._inference_profiles: dict[str, AgentInferenceProfile] = {}

    def register_inference_profile(self, profile: AgentInferenceProfile) -> None:
        if profile.profile_id in self._inference_profiles:
            raise ValueError(f"Inference profile is already registered: {profile.profile_id}")
        self._inference_profiles[profile.profile_id] = profile

    def resolve_inference_profile(self, profile_id: str) -> AgentInferenceProfile:
        profile = self._inference_profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"Inference profile is unavailable: {profile_id}")
        return profile

    def register(
        self,
        definition: AgentDefinition,
        executor: ChildExecutor,
        *,
        make_current: bool | None = None,
    ) -> None:
        """Register an immutable version; only explicit promotion changes defaults.

        Existing two-argument registrations remain compatible. Unless
        ``make_current=False`` is explicit, the first registered version becomes
        the initial default; later versions coexist without implicit
        last-registered-wins behavior.
        """
        key = (definition.agent_id, definition.version)
        if key in self._definitions:
            raise ValueError(
                f"Agent version is already registered: {definition.agent_id}@{definition.version}"
            )
        if make_current is True and not definition.enabled:
            raise ValueError("A disabled Agent version cannot be current.")
        is_first_version = not any(
            registered_agent_id == definition.agent_id
            for registered_agent_id, _ in self._definitions
        )
        self._definitions[key] = definition
        self._executors[key] = executor
        if make_current is True or (
            is_first_version and make_current is None
        ):
            self._current_versions[definition.agent_id] = definition.version

    def set_current(self, agent_id: str, version: str) -> None:
        """Explicitly choose the default version for newly resolved plan steps."""
        definition = self._definitions.get((agent_id, version))
        if definition is None:
            raise ValueError(f"Agent version is unavailable: {agent_id}@{version}")
        if not definition.enabled:
            raise ValueError(f"Agent is unavailable: {agent_id}")
        self._current_versions[agent_id] = version

    def resolve(self, agent_id: str, version: str | None = None) -> tuple[AgentDefinition, ChildExecutor]:
        selected_version = (
            self._current_versions.get(agent_id) if version is None else version
        )
        if selected_version is None:
            raise ValueError(f"Agent is unavailable: {agent_id}")
        key = (agent_id, selected_version)
        definition = self._definitions.get(key)
        if definition is None:
            if version is None:
                raise ValueError(f"Agent is unavailable: {agent_id}")
            raise ValueError(f"Agent version is unavailable: {agent_id}@{version}")
        if not definition.enabled:
            raise ValueError(f"Agent is unavailable: {agent_id}")
        return definition, self._executors[key]

    @property
    def enabled_ids(self) -> tuple[str, ...]:
        return tuple(
            agent_id
            for agent_id, version in self._current_versions.items()
            if (definition := self._definitions.get((agent_id, version))) is not None
            and definition.enabled
        )

    def discovery_catalog(self, allowed_agent_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        """Expose current allowlisted capability descriptions, not executor internals."""
        catalog = []
        for agent_id in dict.fromkeys(allowed_agent_ids):
            try:
                definition, _ = self.resolve(agent_id)
            except ValueError:
                continue
            catalog.append({key: getattr(definition, key) for key in
                ("agent_id", "version", "description", "executor_kind", "scope_mode", "can_resume")})
        return catalog

    @classmethod
    def with_general_agent(cls, legacy_executor: Any, run_child: Callable[..., Awaitable[TaskResult]] | None = None) -> AgentExecutorRegistry:
        registry = cls()
        registry.register(
            AgentDefinition(agent_id=GENERAL_AGENT_ID, version="1", executor_kind="react", can_resume=True,
                            description="General bounded evidence gathering and problem solving through registered tools; use for substantial independent investigations without a more suitable specialist."),
            GeneralAgentExecutor(legacy_executor, run_child),
        )
        return registry
