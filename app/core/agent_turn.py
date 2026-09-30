"""Minimal general agent turn loop with package-aware tool calling."""

from __future__ import annotations

import asyncio
import inspect
import json
import queue
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha1
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.core.agent_runs import (
    AgentRunCancelled,
    AgentRunRecord,
    InMemoryAgentRunManager,
)
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_tool_graph import AgentToolLifecycleGraph
from app.core.context_driver import ToolView
from app.core.llm import (
    LLMAuthenticationError,
    LLMClientError,
    LLMMessage,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMProviderStreamError,
    LLMRateLimitError,
    LLMReasoningEffort,
    LLMRequest,
    LLMResponse,
    LLMResponseMode,
    LLMResponseParseError,
    LLMService,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolDefinition,
    TextLLMClient,
)
from app.core.llm.audit import (
    LLMCallRecord,
    build_stream_error_call_record,
    classify_openai_sdk_exception,
    classify_provider_error,
    prompt_metadata,
    stable_llm_call_id,
    usage_token_counts,
)
from app.core.llm.audit import (
    now_iso as llm_audit_now_iso,
)
from app.core.multi_agent import (
    ContextSnapshot,
    ForkCallerKind,
    ForkPolicy,
    ForkPolicyViolation,
    ForkSubtasksOperation,
    ForkValidationContext,
    Plan,
    PlanPatch,
    PlanPatchContext,
    PlanPatchOperation,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    ScopeGrant,
    SideEffectLevel,
    objective_fingerprint,
    validate_fork_subtasks,
)
from app.core.multi_agent_fast_path import (
    FastPathDisposition,
    FastPathEvent,
    FastPathEventType,
    FastPathPolicy,
    FastPathRequest,
    FastPathTemplateKind,
    assess_fast_path,
    assess_fast_path_completion,
    standard_fast_path_templates,
)
from app.core.multi_agent_replan import PlanPatchRejected, apply_plan_patch
from app.core.runtime_context import current_time_payload
from app.core.safety import (
    SafetyReviewDecision,
    SafetyReviewMode,
    SafetyReviewRecord,
    SafetyReviewRequest,
    SafetyReviewStatus,
    stable_safety_review_id,
)
from app.core.sessions import AgentSession, SessionRecentMessage, SessionService
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolResult,
    effective_tool_read_only,
)

DEFAULT_MAX_DECISION_STEPS = 10
ROOT_COORDINATOR_STEP_ID = "root_coordinator"
DECISION_OBSERVATION_MAX_STRING_CHARS = 4_000
DECISION_OBSERVATION_MAX_LIST_ITEMS = 20
LLM_OBSERVATION_MAX_TOTAL_CHARS = 16_000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _fork_subtasks_function_schema() -> dict[str, Any]:
    scope_properties = {
        "workspace_paths": {"type": "array", "items": {"type": "string"}},
        "source_ids": {"type": "array", "items": {"type": "string"}},
        "account_ids": {"type": "array", "items": {"type": "string"}},
        "allowed_packages": {"type": "array", "items": {"type": "string"}},
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "side_effect_level": {
            "type": "string",
            "enum": ["none", "read", "write", "external"],
        },
    }
    subtask_properties = {
        "step_id": {"type": "string"},
        "objective": {"type": "string"},
        "role": {"type": "string"},
        "inference_profile_id": {"type": "string"},
        "agent_id": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "parallel_group": {"type": "string"},
        "requested_scope": {
            "type": "object",
            "additionalProperties": False,
            "properties": scope_properties,
        },
        "input_refs": {"type": "array", "items": {"type": "string"}},
        "output_contract": {"type": "string"},
        "verification_criteria": {"type": "array", "items": {"type": "string"}},
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["operation_id", "parent_step_id", "subtasks"],
        "properties": {
            "operation_id": {"type": "string"}, "parent_step_id": {"type": "string"},
            "subtasks": {"type": "array", "minItems": 1, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["step_id", "objective", "output_contract"],
                "properties": subtask_properties,
            }},
        },
    }


def _plan_patch_function_schema() -> dict[str, Any]:
    scope = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "workspace_paths": {"type": "array", "items": {"type": "string"}},
            "source_ids": {"type": "array", "items": {"type": "string"}},
            "account_ids": {"type": "array", "items": {"type": "string"}},
            "allowed_packages": {"type": "array", "items": {"type": "string"}},
            "allowed_tools": {"type": "array", "items": {"type": "string"}},
            "side_effect_level": {"type": "string", "enum": ["none", "read", "write", "external"]},
        },
    }
    alternative = {
        "type": "object", "additionalProperties": False,
        "required": ["correlation_id", "step_id", "objective", "output_contract"],
        "properties": {
            "correlation_id": {"type": "string"},
            "step_id": {"type": "string"}, "objective": {"type": "string"},
            "role": {"type": ["string", "null"]}, "depends_on": {"type": "array", "items": {"type": "string"}},
            "inference_profile_id": {"type": ["string", "null"]},
            "parallel_group": {"type": ["string", "null"]},
            "input_refs": {"type": "array", "items": {"type": "string"}},
            "allowed_packages": {"type": "array", "items": {"type": "string"}},
            "allowed_tools": {"type": "array", "items": {"type": "string"}},
            "side_effect_level": {"type": "string", "enum": ["none", "read", "write", "external"]},
            "output_contract": {"type": "string"}, "verification_criteria": {"type": "array", "items": {"type": "string"}},
        },
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["patch_id", "plan_id", "expected_revision", "operation", "reason"],
        "properties": {
            "patch_id": {"type": "string"}, "plan_id": {"type": "string"},
            "expected_revision": {"type": "integer", "minimum": 0},
            "operation": {"type": "string", "enum": [item.value for item in PlanPatchOperation]},
            "reason": {"type": "string"}, "target_step_id": {"type": ["string", "null"]},
            "reduced_scope": scope, "alternative_step": alternative,
            "degradation_note": {"type": ["string", "null"]}, "user_question": {"type": ["string", "null"]},
            "budget": {"type": ["object", "null"], "additionalProperties": False,
                "properties": {key: {"type": ["integer", "null"], "minimum": 1} for key in ("max_tokens", "max_llm_calls", "max_tool_calls", "max_wall_time_seconds")}},
        },
    }
def _session_workspace_path(session: AgentSession) -> str | None:
    """Return the already backend-validated session workspace, if configured."""

    return session.workspace.backend_path if session.workspace is not None else None


_turn_llm_client_name: ContextVar[str | None] = ContextVar(
    "turn_llm_client_name",
    default=None,
)
_turn_llm_model: ContextVar[str | None] = ContextVar("turn_llm_model", default=None)
_turn_safety_review_mode: ContextVar[SafetyReviewMode | None] = ContextVar(
    "turn_safety_review_mode", default=None
)
_turn_inference_snapshot: ContextVar[ContextSnapshot | None] = ContextVar(
    "turn_inference_snapshot", default=None
)
_turn_llm_response_mode: ContextVar[LLMResponseMode] = ContextVar(
    "turn_llm_response_mode",
    default=LLMResponseMode.TEXT,
)
_turn_run_manager: ContextVar[InMemoryAgentRunManager | None] = ContextVar(
    "turn_run_manager",
    default=None,
)
_turn_run_id: ContextVar[str | None] = ContextVar("turn_run_id", default=None)


class AgentTurnToolEvent(BaseModel):
    tool_name: str
    selected_at: str
    completed_at: str
    input: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    feedback: dict[str, Any] = Field(default_factory=dict)


class AgentTurnLLMEvent(BaseModel):
    llm_call_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    session_id: str | None = None
    stage: str
    client_name: str | None = None
    provider: str
    model: str | None = None
    response_mode: str | None = None
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    duration_ms: int | None = None
    system_prompt: str
    user_prompt: str
    output: str
    attempt: int = 1
    http_status: int | None = None
    provider_request_id: str | None = None
    provider_error_type: str | None = None
    provider_error_code: str | None = None
    provider_error_param: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    is_retriable: bool | None = None
    finish_reason: str | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None
    total_token_count: int | None = None
    content_length: int | None = None
    prompt_summary: str | None = None
    partial: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    audit_record: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None
    status_code: int | None = None
    retry_after: str | None = None
    error: str | None = None


class AgentTurnDecisionEvent(BaseModel):
    step_index: int
    decided_at: str
    source: str
    action: str
    selected_package: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    answer: str | None = None
    reason: str | None = None
    assistant_message: str | None = None
    operation: dict[str, Any] = Field(default_factory=dict)
    raw_output: str | None = None


class AgentTurnProgressEvent(BaseModel):
    event_index: int
    created_at: str
    type: str
    message: str
    stage: str | None = None
    tool_name: str | None = None
    package_name: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTurnVerificationWarning(BaseModel):
    code: str
    message: str
    severity: str = "warning"
    evidence: dict[str, Any] = Field(default_factory=dict)


class AgentTurnResult(BaseModel):
    run_id: str
    session_id: str
    trace_id: str
    answer: str
    selected_package: str | None = None
    initial_package: str | None = None
    expanded_packages: list[str] = Field(default_factory=list)
    used_packages: list[str] = Field(default_factory=list)
    active_package: str | None = None
    package_catalog: list[dict[str, Any]] = Field(default_factory=list)
    session_context_window: dict[str, Any] = Field(default_factory=dict)
    expanded_tools: list[dict[str, Any]] = Field(default_factory=list)
    decision_events: list[AgentTurnDecisionEvent] = Field(default_factory=list)
    tool_events: list[AgentTurnToolEvent] = Field(default_factory=list)
    progress_events: list[AgentTurnProgressEvent] = Field(default_factory=list)
    verification_warnings: list[AgentTurnVerificationWarning] = Field(default_factory=list)
    llm_events: list[AgentTurnLLMEvent] = Field(default_factory=list)
    log_path: str | None = None


class AgentTurnWorkingSet(BaseModel):
    """Durable-reference-friendly state shared by external ReAct graph nodes.

    Large prompt inputs, LLM audit records and raw tool output remain in artifacts
    and the local run log.  This model intentionally carries only the data needed
    to resume the next control-flow decision.
    """

    run_id: str
    session_id: str
    trace_id: str
    user_input: str
    context_artifact_ref: dict[str, Any] = Field(default_factory=dict)
    route: dict[str, Any] = Field(default_factory=dict)
    initial_package: str | None = None
    active_package: str | None = None
    expanded_packages: list[str] = Field(default_factory=list)
    used_packages: list[str] = Field(default_factory=list)
    step_index: int = 0
    pending_decision: dict[str, Any] = Field(default_factory=dict)
    pending_invocation_id: str | None = None
    pending_tool_name: str | None = None
    pending_tool_input: dict[str, Any] = Field(default_factory=dict)
    pending_review_id: str | None = None
    observation_artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    terminal_answer: str | None = None
    terminal_reason: str | None = None


class AgentTurnLoop:
    """Small first Main Agent Brain slice for routing to packages and calling tools."""

    _llm_admission_guard: ClassVar[threading.Lock] = threading.Lock()
    _llm_admission: ClassVar[
        dict[tuple[str, str], threading.BoundedSemaphore]
    ] = {}

    def __init__(
        self,
        *,
        session_service: SessionService,
        tool_executor: ToolExecutor,
        llm_client: TextLLMClient | LLMService | None,
        log_dir: Path,
        max_decision_steps: int = DEFAULT_MAX_DECISION_STEPS,
        run_manager: InMemoryAgentRunManager | None = None,
        safety_review_mode: SafetyReviewMode | str = SafetyReviewMode.SKIP,
        safety_manual_wait_poll_seconds: float = 0.5,
        tool_invocation_store: SqliteAgentRunStore | None = None,
        fork_policy: ForkPolicy | None = None,
        fork_scope_resolver: Callable[
            [str], tuple[ScopeGrant, ScopeGrant, ScopeGrant]
        ] | None = None,
        fork_execution: Callable[[str], Any] | None = None,
        fork_plan_finalizer: Callable[[str], Any] | None = None,
        fast_path_single_agent_enabled: bool = False,
        default_workspace_root: str | None = None,
    ) -> None:
        self.session_service = session_service
        self.tool_executor = tool_executor
        self.llm_client = llm_client
        self.log_dir = log_dir
        self.run_manager = run_manager
        self.llm_max_attempts = 2
        self.default_rate_limit_wait_seconds = 1.0
        self.max_decision_steps = max(1, int(max_decision_steps))
        self.decision_format_max_attempts = 2
        self.llm_generation_token_budget: int | None = None
        self.session_context_token_budget = 65_536
        self.safety_review_mode = self._normalize_safety_review_mode(safety_review_mode)
        self.safety_manual_wait_poll_seconds = safety_manual_wait_poll_seconds
        self.tool_invocation_store = tool_invocation_store
        self.fork_policy = fork_policy
        self.fork_scope_resolver = fork_scope_resolver
        self.fork_execution = fork_execution
        self.fork_plan_finalizer = fork_plan_finalizer
        self.default_workspace_root = default_workspace_root
        self.fast_path_policy: FastPathPolicy | None = None
        if fast_path_single_agent_enabled:
            single_agent_template = next(
                template
                for template in standard_fast_path_templates(
                    retrieval_capability_id="unavailable-retrieval",
                    inspection_capability_id="unavailable-inspection",
                    verification_capability_id="unavailable-verification",
                )
                if template.template_id == FastPathTemplateKind.SINGLE_AGENT
            )
            self.fast_path_policy = FastPathPolicy(
                correlation_id="server-fast-path-policy",
                enabled=True,
                allowed_templates=(FastPathTemplateKind.SINGLE_AGENT,),
                templates=(single_agent_template,),
            )

    def _normalize_safety_review_mode(
        self,
        mode: SafetyReviewMode | str,
    ) -> SafetyReviewMode:
        if isinstance(mode, SafetyReviewMode):
            return mode
        try:
            return SafetyReviewMode(str(mode))
        except ValueError:
            return SafetyReviewMode.SKIP

    def _effective_safety_review_mode(self) -> SafetyReviewMode:
        """A request may increase review strictness, never weaken local policy."""
        requested = _turn_safety_review_mode.get()
        priority = {
            SafetyReviewMode.SKIP: 0,
            SafetyReviewMode.LLM: 1,
            SafetyReviewMode.MANUAL: 2,
        }
        if requested is None or priority[requested] <= priority[self.safety_review_mode]:
            return self.safety_review_mode
        return requested

    def run(
        self,
        *,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        safety_review_mode: SafetyReviewMode | None = None,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        client_token = _turn_llm_client_name.set(llm_client_name)
        model_token = _turn_llm_model.set(llm_model)
        mode_token = _turn_llm_response_mode.set(llm_response_mode)
        review_mode_token = _turn_safety_review_mode.set(safety_review_mode)
        inference_token = _turn_inference_snapshot.set(None)
        run_manager_token = None
        run_id_token = None
        try:
            if self.run_manager is not None:
                run = (
                    self._get_existing_run(existing_run_id)
                    if existing_run_id
                    else self.create_run_for_turn(
                        session_id=session_id,
                        user_input=user_input,
                    )
                )
                _turn_inference_snapshot.set(
                    self._inference_snapshot_for_run(run)
                )
                session = self.session_service.ensure_session(
                    session_id=run.session_id,
                    title=user_input.strip()[:60] or "Agent Session",
                    metadata={"entrypoint": "agent.turn"},
                )
                trace_id = run.trace_id
                run_id = run.run_id
                run_manager_token = _turn_run_manager.set(self.run_manager)
                run_id_token = _turn_run_id.set(run_id)
                self.run_manager.mark_running(run_id)
                self.run_manager.append_event(
                    run_id,
                    "run_started",
                    "Agent run started.",
                    stage="run",
                    payload={
                        "session_id": session.session_id,
                        "trace_id": trace_id,
                        **self._inference_call_metadata(),
                    },
                )
            else:
                session = self.session_service.ensure_session(
                    session_id=session_id,
                    title=user_input.strip()[:60] or "Agent Session",
                    metadata={"entrypoint": "agent.turn"},
                )
                trace_id = _stable_id("agent_turn", session.session_id, user_input, _now_iso())
                run_id = trace_id
            return self._run(
                session=session,
                trace_id=trace_id,
                run_id=run_id,
                user_input=user_input,
            )
        except AgentRunCancelled as exc:
            self._mark_current_run_cancelled(str(exc) or "Run cancelled.")
            raise
        except Exception as exc:
            self._mark_current_run_failed(type(exc).__name__, str(exc))
            raise
        finally:
            if run_id_token is not None:
                _turn_run_id.reset(run_id_token)
            if run_manager_token is not None:
                _turn_run_manager.reset(run_manager_token)
            _turn_llm_client_name.reset(client_token)
            _turn_llm_model.reset(model_token)
            _turn_llm_response_mode.reset(mode_token)
            _turn_safety_review_mode.reset(review_mode_token)
            _turn_inference_snapshot.reset(inference_token)

    async def run_async(
        self,
        *,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        safety_review_mode: SafetyReviewMode | None = None,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        result_queue: queue.Queue[tuple[bool, AgentTurnResult | BaseException]] = queue.Queue(
            maxsize=1
        )

        def target() -> None:
            try:
                result_queue.put(
                    (
                        True,
                        self.run(
                            session_id=session_id,
                            user_input=user_input,
                            llm_client_name=llm_client_name,
                            llm_model=llm_model,
                            llm_response_mode=llm_response_mode,
                            safety_review_mode=safety_review_mode,
                            existing_run_id=existing_run_id,
                        ),
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - propagate worker cancellation/errors.
                result_queue.put((False, exc))

        thread = threading.Thread(target=target, name="lka-agent-turn", daemon=True)
        thread.start()
        while True:
            try:
                ok, value = result_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if ok:
                return value  # type: ignore[return-value]
            raise value

    def create_run_for_turn(
        self,
        *,
        session_id: str | None,
        user_input: str,
        parent_run_id: str | None = None,
    ) -> AgentRunRecord:
        if self.run_manager is None:
            raise RuntimeError("Agent run manager is not configured.")
        session = self.session_service.ensure_session(
            session_id=session_id,
            title=user_input.strip()[:60] or "Agent Session",
            metadata={"entrypoint": "agent.turn"},
        )
        trace_id = _stable_id("agent_turn", session.session_id, user_input, _now_iso())
        return self.run_manager.create_run(
            session_id=session.session_id,
            user_input=user_input,
            trace_id=trace_id,
            parent_run_id=parent_run_id,
            metadata={"entrypoint": "agent.turn"},
        )

    def _get_existing_run(self, run_id: str) -> AgentRunRecord:
        if self.run_manager is None:
            raise RuntimeError("Agent run manager is not configured.")
        run = self.run_manager.get_run(run_id)
        if run is None:
            raise KeyError(f"Agent run not found: {run_id}")
        return run

    def _inference_snapshot_for_run(
        self, run: AgentRunRecord
    ) -> ContextSnapshot | None:
        if run.parent_run_id is None:
            return None
        raw_snapshot = run.metadata.get("context_snapshot")
        if not isinstance(raw_snapshot, dict):
            return None
        if (
            raw_snapshot.get("inference_profile_id") is None
            and raw_snapshot.get("inference_selection_source", "default") == "default"
        ):
            return None
        snapshot = ContextSnapshot.model_validate(raw_snapshot)
        if (
            snapshot.child_run_id != run.run_id
            or snapshot.session_id != run.session_id
            or (
                snapshot.inference_selection_source == "server_profile"
                and (
                    snapshot.inference_profile_id is None
                    or snapshot.inference_client_name is None
                    or snapshot.inference_model is None
                )
            )
        ):
            raise ValueError("Child inference profile does not match its immutable run snapshot.")
        return snapshot

    def _inference_selection(
        self,
    ) -> tuple[str | None, str | None, LLMReasoningEffort | None, str, str | None]:
        """Apply explicit request > frozen child profile > configured client default."""
        request_client = _turn_llm_client_name.get()
        request_model = _turn_llm_model.get()
        if request_client is not None or request_model is not None:
            return request_client, request_model, None, "request", None
        snapshot = _turn_inference_snapshot.get()
        if snapshot is not None:
            if snapshot.inference_selection_source == "default":
                return None, None, None, "default", None
            effort = (
                LLMReasoningEffort(snapshot.inference_reasoning_effort)
                if snapshot.inference_reasoning_effort is not None
                else None
            )
            if effort is not None:
                supports = getattr(self.llm_client, "supports_reasoning_effort", None)
                if not callable(supports) or not supports(
                    client_name=snapshot.inference_client_name
                ):
                    raise LLMClientError(
                        "The frozen inference profile requests reasoning effort unsupported by its LLM client."
                    )
            return (
                snapshot.inference_client_name,
                snapshot.inference_model,
                effort,
                snapshot.inference_selection_source,
                snapshot.inference_profile_id,
            )
        return None, None, None, "default", None

    def _inference_call_metadata(self) -> dict[str, str]:
        client_name, model, effort, source, profile_id = self._inference_selection()
        metadata = {"inference_selection_source": source}
        if client_name is not None:
            metadata["inference_client_name"] = client_name
        if model is not None:
            metadata["inference_model"] = model
        if effort is not None:
            metadata["inference_reasoning_effort"] = effort.value
        if profile_id is not None:
            metadata["inference_profile_id"] = profile_id
        return metadata

    def _run(
        self,
        *,
        session: AgentSession,
        trace_id: str,
        run_id: str,
        user_input: str,
    ) -> AgentTurnResult:
        context = ToolContext(
            session_id=session.session_id,
            trace_id=trace_id,
            context_id=trace_id,
            workspace_root=_session_workspace_path(session) or self.default_workspace_root,
            tool_view=self._tool_view_for_run(run_id),
            run_id=run_id,
        )
        self.session_service.append_message(
            session_id=session.session_id,
            role="user",
            content=user_input,
            payload={"trace_id": trace_id, "entrypoint": "agent.turn"},
        )
        context_window = self.session_service.get_context_window(
            session_id=session.session_id,
            token_budget=self.session_context_token_budget,
        )
        context_window_payload = context_window.model_dump(mode="json")
        context_window_payload["current_time"] = current_time_payload()
        if session.workspace is not None:
            context_window_payload["workspace"] = session.workspace.model_dump(mode="json")
        cached_tool_observations = self._cached_tool_observations_from_session(
            session_id=session.session_id,
        )
        if cached_tool_observations:
            context_window_payload["cached_tool_observations"] = cached_tool_observations

        self._assess_configured_fast_path(
            run_id=run_id,
            session_id=session.session_id,
            user_input=user_input,
        )

        package_catalog = self._package_catalog(context.tool_view)
        llm_events: list[AgentTurnLLMEvent] = []
        decision_events: list[AgentTurnDecisionEvent] = []
        progress_events: list[AgentTurnProgressEvent] = []
        route = self._route(
            user_input=user_input,
            package_catalog=package_catalog,
            context_window=context_window_payload,
            llm_events=llm_events,
            decision_events=decision_events,
        )
        selected_package = route.get("selected_package")
        self._append_progress(
            progress_events,
            type="package_selected" if isinstance(selected_package, str) else "no_package",
            stage="route",
            package_name=selected_package if isinstance(selected_package, str) else None,
            status="completed",
            message=(
                f"Selected `{selected_package}` package."
                if isinstance(selected_package, str)
                else "No tool package selected; answering from context if possible."
            ),
            metadata={"reason": route.get("reason")},
        )
        tool_events: list[AgentTurnToolEvent] = []
        expanded_tools: list[dict[str, Any]] = []
        answer = ""

        if isinstance(selected_package, str):
            expanded_tools = [
                tool.model_dump(mode="json")
                for tool in self._tools_for_package(
                    selected_package,
                    tool_view=context.tool_view,
                )
            ]
            answer = self._run_package_tools(
                user_input=user_input,
                route=route,
                context_window=context_window_payload,
                context=context,
                tool_events=tool_events,
                llm_events=llm_events,
                decision_events=decision_events,
                progress_events=progress_events,
                expanded_tools=expanded_tools,
                selected_package=selected_package,
            )
        else:
            answer = self._answer_from_context_with_llm(
                user_input=user_input,
                route=route,
                context_window=context_window_payload,
                llm_events=llm_events,
            )
            if answer:
                self._record_decision(
                    decision_events,
                    source="llm",
                    action="answer",
                    answer=answer,
                    reason=route.get("reason")
                    if isinstance(route.get("reason"), str)
                    else "No tool package was needed.",
                )
            else:
                answer = (
                    "我还没有为这个请求选择到可执行工具。当前最小 Agent turn 只支持在需要"
                    "本地上下文时展开合适的 Tool Package。"
                )
                self._record_decision(
                    decision_events,
                    source="local",
                    action="answer",
                    answer=answer,
                    reason="No supported package was selected.",
                )

        self._append_progress(
            progress_events,
            type="final_answer",
            stage="answer",
            status="completed",
            message=self._short_text(answer),
            metadata={"answer": answer},
        )
        verification_warnings = self._verify_final_answer(
            answer=answer,
            tool_events=tool_events,
        )
        self._complete_configured_fast_path(run_id=run_id, answer=answer)
        for warning in verification_warnings:
            self._append_progress(
                progress_events,
                type="verification_warning",
                stage="verify",
                status=warning.severity,
                message=warning.message,
                metadata=warning.model_dump(mode="json"),
            )

        self._finalize_multi_agent_plan(run_id)

        updated_context_window = self.session_service.record_context_exchange(
            session_id=session.session_id,
            user_input=user_input,
            agent_answer=answer,
            trace_id=trace_id,
            token_budget=self.session_context_token_budget,
            context_summarizer=lambda summary, messages, recent_messages, token_budget: (
                self._summarize_context_window(
                    summary=summary,
                    messages_to_summarize=messages,
                    retained_recent_messages=recent_messages,
                    token_budget=token_budget,
                    llm_events=llm_events,
                )
            ),
        )
        expanded_packages = self._packages_from_expanded_tools(expanded_tools)
        used_packages = self._packages_from_tool_events(tool_events)
        active_package = used_packages[-1] if used_packages else None
        result = AgentTurnResult(
            run_id=run_id,
            session_id=session.session_id,
            trace_id=trace_id,
            answer=answer,
            selected_package=selected_package if isinstance(selected_package, str) else None,
            initial_package=selected_package if isinstance(selected_package, str) else None,
            expanded_packages=expanded_packages,
            used_packages=used_packages,
            active_package=active_package,
            package_catalog=package_catalog,
            session_context_window=context_window_payload,
            expanded_tools=expanded_tools,
            decision_events=decision_events,
            tool_events=tool_events,
            progress_events=progress_events,
            verification_warnings=verification_warnings,
            llm_events=llm_events,
        )
        log_path = self._write_log(result=result, user_input=user_input)
        result = result.model_copy(update={"log_path": str(log_path)})
        self.session_service.append_message(
            session_id=session.session_id,
            role="agent",
            content=answer,
            payload={
                "run_id": result.run_id,
                "trace_id": trace_id,
                "selected_package": result.selected_package,
                "initial_package": result.initial_package,
                "expanded_packages": result.expanded_packages,
                "used_packages": result.used_packages,
                "active_package": result.active_package,
                "log_path": result.log_path,
                "context_window": {
                    "token_budget": updated_context_window.token_budget,
                    "token_estimate": updated_context_window.token_estimate,
                    "recent_message_count": len(updated_context_window.recent_messages),
                },
                "decision_events": [event.model_dump(mode="json") for event in decision_events],
                "tool_events": [event.model_dump(mode="json") for event in tool_events],
                "progress_events": [event.model_dump(mode="json") for event in progress_events],
                "verification_warnings": [
                    warning.model_dump(mode="json") for warning in verification_warnings
                ],
            },
        )
        self._complete_current_run(result)
        return result

    def _route(
        self,
        *,
        user_input: str,
        package_catalog: list[dict[str, Any]],
        context_window: dict[str, Any],
        llm_events: list[AgentTurnLLMEvent],
        decision_events: list[AgentTurnDecisionEvent],
    ) -> dict[str, Any]:
        if self.llm_client is None:
            route = self._route_locally(user_input)
            self._record_route_decision(
                decision_events,
                source="local",
                route=route,
                raw_output=None,
            )
            return route

        system_prompt = (
            "You are the Main Agent Brain for Local Knowledge Agent OS. Choose at most one "
            "tool package for the current turn. Do not choose concrete tools yet. Return only "
            "null when the provided session context window "
            "is sufficient to answer without another tool call. Use only package names present "
            "in package_catalog, and use each package description and routing_hints as the "
            "source of truth. Return only strict JSON: "
            '{"selected_package":"<package name or null>","reason":"...",'
            '"search_query":"optional query hint"}'
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "session_context_window": self._context_window_for_llm(context_window),
                "package_catalog": package_catalog,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="route",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"agent_turn_route user_input={user_input[:80]}",
            max_output_tokens=self.llm_generation_token_budget,
            llm_events=llm_events,
        )
        if response is None:
            route = self._route_locally(user_input)
            self._record_route_decision(
                decision_events,
                source="local",
                route=route,
                raw_output=None,
            )
            return route

        parsed = self._parse_json_object(response.content)
        if not isinstance(parsed, dict) or not parsed:
            recovered_route = self._recover_route_from_raw_output(
                raw_output=response.content,
                user_input=user_input,
                package_catalog=package_catalog,
            )
            if recovered_route is not None:
                self._record_route_decision(
                    decision_events,
                    source="llm",
                    route=recovered_route,
                    raw_output=response.content,
                )
                return recovered_route
            route = self._route_locally(user_input)
            self._record_route_decision(
                decision_events,
                source="local",
                route=route,
                raw_output=response.content,
            )
            return route
        selected_package = parsed.get("selected_package")
        available_packages = {
            str(package.get("name"))
            for package in package_catalog
            if isinstance(package.get("name"), str)
        }
        if isinstance(selected_package, str) and selected_package in available_packages:
            self._record_route_decision(
                decision_events,
                source="llm",
                route=parsed,
                raw_output=response.content,
            )
            return parsed
        route = {
            "selected_package": None,
            "reason": parsed.get("reason") or "No package selected.",
        }
        self._record_route_decision(
            decision_events,
            source="llm",
            route=route,
            raw_output=response.content,
        )
        return route

    def _recover_route_from_raw_output(
        self,
        *,
        raw_output: str,
        user_input: str,
        package_catalog: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        compact_output = "".join(raw_output.lower().split())
        for package in package_catalog:
            name = package.get("name")
            if not isinstance(name, str):
                continue
            if f'"selected_package":"{name.lower()}"' in compact_output:
                return {
                    "selected_package": name,
                    "reason": "Recovered registered package from malformed route output.",
                    "search_query": user_input,
                }
        return None

    def _route_locally(self, user_input: str) -> dict[str, Any]:
        return {
            "selected_package": None,
            "reason": "No LLM route was available; Agent core does not use domain heuristics.",
            "search_query": user_input,
        }

    def _run_package_tools(
        self,
        *,
        user_input: str,
        route: dict[str, Any],
        context_window: dict[str, Any],
        context: ToolContext,
        tool_events: list[AgentTurnToolEvent],
        llm_events: list[AgentTurnLLMEvent],
        decision_events: list[AgentTurnDecisionEvent],
        progress_events: list[AgentTurnProgressEvent],
        expanded_tools: list[dict[str, Any]],
        selected_package: str,
    ) -> str:
        if self.llm_client is not None:
            answer = self._run_llm_decision_loop(
                user_input=user_input,
                route=route,
                context_window=context_window,
                context=context,
                tool_events=tool_events,
                llm_events=llm_events,
                decision_events=decision_events,
                progress_events=progress_events,
                expanded_tools=expanded_tools,
                selected_package=selected_package,
            )
            if answer:
                return answer
        answer = (
            f"当前运行时没有获得 `{selected_package}` package 的有效结构化决策，"
            "已停止继续调用工具，避免把未完成的步骤误当作最终结果。请重试该请求。"
        )
        self._record_decision(
            decision_events,
            source="local",
            action="answer",
            answer=answer,
            reason="No package-specific local fallback is implemented.",
        )
        return answer

    def _run_llm_decision_loop(
        self,
        *,
        user_input: str,
        route: dict[str, Any],
        context_window: dict[str, Any],
        context: ToolContext,
        tool_events: list[AgentTurnToolEvent],
        llm_events: list[AgentTurnLLMEvent],
        decision_events: list[AgentTurnDecisionEvent],
        progress_events: list[AgentTurnProgressEvent],
        expanded_tools: list[dict[str, Any]],
        selected_package: str,
    ) -> str | None:
        observations: list[dict[str, Any]] = []
        successful_call_fingerprints: set[str] = set()
        observations.extend(self._cached_tool_observations_from_context_window(context_window))
        package_catalog = self._package_catalog(context.tool_view)
        expanded_package_names = {selected_package}
        allowed_tool_names = {
            str(tool.get("name")) for tool in expanded_tools if isinstance(tool.get("name"), str)
        }
        for step_index in range(1, self.max_decision_steps + 1):
            decision = self._decide_next_action(
                user_input=user_input,
                route=route,
                context_window=context_window,
                package_catalog=package_catalog,
                expanded_package_names=sorted(expanded_package_names),
                expanded_tools=expanded_tools,
                observations=observations,
                llm_events=llm_events,
            )
            if decision is None:
                return None

            action = str(decision.get("action") or "")
            self._record_decision(
                decision_events,
                source="llm",
                action=action,
                selected_package=decision.get("package_name")
                if isinstance(decision.get("package_name"), str)
                else None,
                tool_name=decision.get("tool_name"),
                tool_input=decision.get("tool_input")
                if isinstance(decision.get("tool_input"), dict)
                else {},
                answer=decision.get("answer") if isinstance(decision.get("answer"), str) else None,
                reason=decision.get("reason") if isinstance(decision.get("reason"), str) else None,
                assistant_message=decision.get("assistant_message")
                if isinstance(decision.get("assistant_message"), str)
                else None,
                operation=decision.get("operation")
                if isinstance(decision.get("operation"), dict)
                else {},
                raw_output=decision.get("_raw_output")
                if isinstance(decision.get("_raw_output"), str)
                else None,
                step_index=step_index + 1,
            )
            assistant_message = (
                decision.get("assistant_message")
                if isinstance(decision.get("assistant_message"), str)
                else None
            )
            if assistant_message:
                self._append_progress(
                    progress_events,
                    type="assistant_message",
                    stage="decision",
                    status="completed",
                    tool_name=decision.get("tool_name")
                    if isinstance(decision.get("tool_name"), str)
                    else None,
                    package_name=decision.get("package_name")
                    if isinstance(decision.get("package_name"), str)
                    else None,
                    message=self._short_text(assistant_message),
                    metadata={"action": action, "step_index": step_index + 1},
                )

            if action in {"fork_subtasks", "fork_subtasks_invalid"}:
                run_id = _turn_run_id.get()
                if isinstance(run_id, str):
                    self._upgrade_fast_path_for_multi_agent(run_id)
                outcome = self._handle_fork_subtasks_decision(
                    run_id=run_id,
                    user_input=user_input,
                    operation=decision.get("operation"),
                    parse_error=(
                        decision.get("reason") if action == "fork_subtasks_invalid" else None
                    ),
                )
                observations.append({"action": "fork_subtasks", **outcome})
                self._append_progress(
                    progress_events,
                    type=(
                        "fork_subtasks_validated"
                        if outcome.get("status") == "validated"
                        else "fork_subtasks_rejected"
                    ),
                    stage="planner",
                    status=str(outcome.get("status") or "rejected"),
                    message=str(outcome.get("message") or "Planner fork operation processed."),
                    metadata=self._planner_progress_summary(outcome),
                )
                continue

            if action in {"plan_patch", "plan_patch_invalid"}:
                outcome = self._handle_plan_patch_decision(
                    run_id=_turn_run_id.get(),
                    operation=decision.get("operation"),
                    parse_error=decision.get("reason") if action == "plan_patch_invalid" else None,
                )
                observations.append({"action": "plan_patch", **outcome})
                self._append_progress(
                    progress_events,
                    type="multi_agent_plan_patched" if outcome.get("status") in {"applied", "waiting_user", "aborted"} else "multi_agent_plan_patch_rejected",
                    stage="planner",
                    status=str(outcome.get("status") or "rejected"),
                    message=str(outcome.get("message") or "Planner patch processed."),
                    metadata=self._planner_progress_summary(outcome),
                )
                continue

            if action == "final_answer":
                if self._multi_agent_replan_pending():
                    observations.append({
                        "action": "final_answer",
                        "status": "rejected",
                        "replan_required": True,
                        "error": (
                            "The persisted child plan has unresolved failures. Use a valid "
                            "plan_patch to retry, degrade, ask the user, or abort before answering."
                        ),
                    })
                    self._append_progress(
                        progress_events,
                        type="multi_agent_final_answer_rejected",
                        stage="planner",
                        status="rejected",
                        message="Unresolved child failures require a structured PlanPatch.",
                    )
                    continue
                return self._answer_with_llm(
                    user_input=user_input,
                    route=route,
                    context_window=context_window,
                    observations=observations,
                    final_decision=decision,
                    llm_events=llm_events,
                )

            if action == "malformed_tool_call":
                answer = (
                    "LLM 返回了疑似工具调用的损坏 JSON，系统已停止执行，避免把未执行的"
                    "工具操作误当作最终结果。请重试该请求。"
                )
                return answer

            if action in {"invalid_plain_text_decision", "invalid_empty_decision"}:
                if observations:
                    return self._answer_with_llm(
                        user_input=user_input,
                        route=route,
                        context_window=context_window,
                        observations=observations,
                        final_decision=decision,
                        llm_events=llm_events,
                    )
                return None

            if action == "invalid_final_answer":
                if observations:
                    return self._answer_with_llm(
                        user_input=user_input,
                        route=route,
                        context_window=context_window,
                        observations=observations,
                        final_decision=decision,
                        llm_events=llm_events,
                    )
                return None

            if action == "expand_package":
                package_name = str(decision.get("package_name") or "")
                if not self._package_exists(package_name, tool_view=context.tool_view):
                    observations.append(
                        {
                            "action": "expand_package",
                            "package_name": package_name,
                            "status": "rejected",
                            "error": "Tool package is not registered.",
                        }
                    )
                    self._append_progress(
                        progress_events,
                        type="package_expand_rejected",
                        stage="decision",
                        package_name=package_name,
                        status="rejected",
                        message=f"Rejected package expansion for `{package_name}`.",
                        metadata={"error": "Tool package is not registered."},
                    )
                    continue
                if package_name in expanded_package_names:
                    observations.append(
                        {
                            "action": "expand_package",
                            "package_name": package_name,
                            "status": "completed",
                            "message": "Tool package was already expanded.",
                        }
                    )
                    self._append_progress(
                        progress_events,
                        type="package_expanded",
                        stage="decision",
                        package_name=package_name,
                        status="completed",
                        message=f"`{package_name}` package was already expanded.",
                    )
                    continue
                new_tools = self._tool_payloads_for_package(
                    package_name,
                    tool_view=context.tool_view,
                )
                expanded_tools.extend(new_tools)
                expanded_package_names.add(package_name)
                new_tool_names = [
                    str(tool.get("name")) for tool in new_tools if isinstance(tool.get("name"), str)
                ]
                allowed_tool_names.update(new_tool_names)
                observations.append(
                    {
                        "action": "expand_package",
                        "package_name": package_name,
                        "status": "completed",
                        "expanded_tools": new_tool_names,
                    }
                )
                self._append_progress(
                    progress_events,
                    type="package_expanded",
                    stage="decision",
                    package_name=package_name,
                    status="completed",
                    message=(
                        f"Expanded `{package_name}` package with {len(new_tool_names)} tools."
                    ),
                    metadata={"expanded_tools": new_tool_names},
                )
                continue

            if action != "call_tool":
                return None

            tool_name = str(decision.get("tool_name") or "")
            tool_input = (
                decision.get("tool_input") if isinstance(decision.get("tool_input"), dict) else {}
            )
            if tool_name not in allowed_tool_names:
                observations.append(
                    {
                        "tool_name": tool_name,
                        "status": "rejected",
                        "error": "Tool is not available in the expanded package.",
                    }
                )
                self._append_progress(
                    progress_events,
                    type="tool_rejected",
                    stage="decision",
                    tool_name=tool_name,
                    status="rejected",
                    message=f"Rejected unavailable tool `{tool_name}`.",
                    metadata={"allowed_tools": sorted(allowed_tool_names)},
                )
                continue

            call_fingerprint = self._tool_call_fingerprint(
                tool_name=tool_name,
                tool_input=tool_input,
            )
            operation = decision.get("operation")
            repeat_successful_call = bool(
                operation.get("repeat_successful_call") if isinstance(operation, dict) else False
            )
            if call_fingerprint in successful_call_fingerprints and not repeat_successful_call:
                reason = (
                    "Blocked an identical tool call after a successful result in this turn. "
                    "Existing evidence is available for the answer stage."
                )
                self._append_progress(
                    progress_events,
                    type="tool_duplicate_blocked",
                    stage="decision",
                    tool_name=tool_name,
                    status="blocked",
                    message=f"Skipped duplicate successful call to `{tool_name}`.",
                    metadata={"tool_input": tool_input, "reason": reason},
                )
                self._record_decision(
                    decision_events,
                    source="local",
                    action="final_answer",
                    reason=reason,
                    step_index=step_index + 1,
                )
                return self._answer_with_llm(
                    user_input=user_input,
                    route=route,
                    context_window=context_window,
                    observations=observations,
                    final_decision={"action": "final_answer", "reason": reason},
                    llm_events=llm_events,
                )

            tool_result = self._execute_tool(
                tool_name=tool_name,
                tool_input=tool_input,
                context=context,
                tool_events=tool_events,
                progress_events=progress_events,
                llm_events=llm_events,
                step_index=step_index,
            )
            feedback = self._check_tool_result(
                user_input=user_input,
                tool_package=self._package_for_tool(tool_name),
                decision=decision,
                tool_result=tool_result,
                llm_events=llm_events,
            )
            if tool_events:
                tool_events[-1].feedback = feedback
            self._append_progress(
                progress_events,
                type="tool_feedback",
                stage="tool_result_check",
                tool_name=tool_name,
                status=str(feedback.get("status") or ""),
                message=str(feedback.get("message") or f"{tool_name} feedback recorded."),
                metadata=feedback,
            )
            observations.append(
                self._observation_for_decision_prompt(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_result=tool_result,
                    feedback=feedback,
                )
            )
            if tool_result.status == "completed":
                if self._completed_tool_call_changes_state(
                    tool_name=tool_name,
                    tool_input=tool_input,
                ):
                    # A write can invalidate an earlier read with identical
                    # arguments.  Preserve the duplicate guard for repeated
                    # reads/writes, while allowing deterministic read-back
                    # verification after a successful state change.
                    successful_call_fingerprints.clear()
                successful_call_fingerprints.add(call_fingerprint)

        if self._multi_agent_replan_pending():
            return self._unresolved_multi_agent_answer()
        if observations:
            return self._answer_with_llm(
                user_input=user_input,
                route=route,
                context_window=context_window,
                observations=observations,
                final_decision={
                    "action": "final_answer",
                    "reason": "Step limit reached after loading evidence.",
                },
                llm_events=llm_events,
            )
        return None

    def _multi_agent_replan_pending(self, run_id: str | None = None) -> bool:
        manager = getattr(self, "run_manager", None)
        run_id = run_id or _turn_run_id.get()
        run = manager.get_run(run_id) if manager is not None and run_id else None
        return bool(run and run.metadata.get("multi_agent_replan_required"))

    @staticmethod
    def _unresolved_multi_agent_answer() -> str:
        return (
            "子任务仍有未解决的失败或阻塞，当前无法将多 Agent 计划报告为完成。"
            "本次运行已停止；请查看子任务状态并重试或调整任务。"
        )

    def _fork_caller_kind_for_run(self, run: AgentRunRecord) -> ForkCallerKind:
        """Resolve child fork authority from immutable server-owned lineage.

        Parentage alone does not imply Coordinator authority. Missing or legacy
        role metadata fails closed to LEAF rather than upgrading a child.
        """
        if run.parent_run_id is None:
            return ForkCallerKind.ROOT_PLANNER
        snapshot = run.metadata.get("context_snapshot")
        kind_value = snapshot.get("agent_kind") if isinstance(snapshot, dict) else None
        if kind_value is None:
            parent = self.run_manager.get_run(run.parent_run_id) if self.run_manager else None
            raw_plan = parent.metadata.get("multi_agent_plan") if parent is not None else None
            if isinstance(raw_plan, dict):
                try:
                    parent_plan = Plan.model_validate(raw_plan)
                    parent_step = next(
                        (step for step in parent_plan.steps if step.step_id == run.step_id),
                        None,
                    )
                    kind_value = parent_step.agent_kind if parent_step is not None else None
                except (ValidationError, ValueError):
                    kind_value = None
        try:
            resolved = (
                kind_value
                if isinstance(kind_value, ForkCallerKind)
                else ForkCallerKind(str(kind_value))
            )
        except ValueError:
            return ForkCallerKind.LEAF
        return (
            ForkCallerKind.COORDINATOR
            if resolved == ForkCallerKind.COORDINATOR
            else ForkCallerKind.LEAF
        )

    def _handle_fork_subtasks_decision(
        self,
        *,
        run_id: str | None,
        user_input: str,
        operation: Any,
        parse_error: str | None = None,
    ) -> dict[str, Any]:
        raw_operation = operation if isinstance(operation, dict) else {}
        operation_id = str(raw_operation.get("operation_id") or "")
        if run_id is None or self.run_manager is None:
            return {"status": "rejected", "message": "Run persistence is unavailable."}
        run = self.run_manager.get_run(run_id)
        if run is None:
            return {"status": "rejected", "message": "Agent run no longer exists."}
        if self.fork_policy is None:
            parse_error = parse_error or "Structured multi-agent planning is disabled by server policy."
        if parse_error is not None:
            payload = {
                "operation_id": operation_id,
                "requested_operation": raw_operation,
                "status": "rejected",
                "error": parse_error,
            }
            self.run_manager.record_multi_agent_plan(
                run_id,
                event_type="fork_subtasks_rejected",
                payload=payload,
            )
            return {"status": "rejected", "operation_id": operation_id, "message": parse_error}

        try:
            fork_operation = ForkSubtasksOperation.model_validate(operation)
            assert run is not None and self.fork_policy is not None
            if run.status.value != "running":
                raise ForkPolicyViolation("Fork parent run is not active.")
            prior_operation = run.metadata.get("fork_operations", {}).get(
                fork_operation.operation_id
            )
            if prior_operation:
                if (
                    prior_operation.get("status") == "rejected"
                    and prior_operation.get("requested_operation") == raw_operation
                ):
                    return {
                        "status": "rejected",
                        "operation_id": fork_operation.operation_id,
                        "message": str(prior_operation.get("error") or "Fork operation was rejected."),
                    }
                if prior_operation.get("status") != "validated":
                    raise ForkPolicyViolation("Fork operation_id has already been used.")
                return self._execute_fork_plan(run_id=run_id, operation_id=operation_id)

            self.run_manager.append_event(
                run_id,
                "fork_subtasks_requested",
                "Planner proposed a structured fork operation.",
                stage="planner",
                payload={"operation_id": operation_id, "requested_operation": raw_operation},
            )

            raw_plan = run.metadata.get("multi_agent_plan")
            if raw_plan is None:
                plan = Plan(
                    correlation_id=run.trace_id,
                    plan_id=_stable_id("plan", run.run_id),
                    parent_run_id=run.run_id,
                    session_id=run.session_id,
                    objective=user_input,
                    steps=(
                        PlanStep(
                            correlation_id=run.trace_id,
                            step_id=ROOT_COORDINATOR_STEP_ID,
                            objective=user_input,
                            output_contract="Coordinate the task and produce the user-facing answer.",
                            status=PlanStepStatus.RUNNING,
                        ),
                    ),
                ).transition_to(PlanStatus.VALIDATED).transition_to(
                    PlanStatus.QUEUED
                ).transition_to(PlanStatus.RUNNING)
            else:
                plan = Plan.model_validate(raw_plan)

            active_step = next(
                (step for step in plan.steps if step.step_id == fork_operation.parent_step_id),
                None,
            )
            if active_step is None:
                raise ForkPolicyViolation("Fork parent_step_id does not exist in the active plan.")
            if active_step.status != PlanStepStatus.RUNNING:
                raise ForkPolicyViolation("Fork parent step is not active.")
            if any(
                active_step.step_id in subtask.depends_on
                for subtask in fork_operation.subtasks
            ):
                raise ForkPolicyViolation(
                    "A forked subtask cannot depend on its still-running coordinator step."
                )
            known_step_ids = tuple(step.step_id for step in plan.steps)
            existing_fork_count = sum(step.fork_operation_id is not None for step in plan.steps)
            scope_resolver = getattr(self, "fork_scope_resolver", None)
            if scope_resolver is not None:
                parent_scope, session_scope, workspace_scope = scope_resolver(run_id)
            elif run.parent_run_id is not None:
                attached_snapshot = run.metadata.get("context_snapshot", {})
                parent_scope = ScopeGrant.model_validate(
                    attached_snapshot.get("effective_scope", {})
                )
                session_scope = parent_scope
                workspace_scope = parent_scope
            else:
                # Compatibility fallback for direct AgentTurnLoop construction.
                # Runtime supplies the authoritative resolver in normal operation.
                parent_scope = session_scope = workspace_scope = self.fork_policy.allowed_scope
            current_depth = 0
            if run.parent_run_id is not None:
                parent = self.run_manager.get_run(run.parent_run_id)
                parent_plan = (
                    Plan.model_validate(parent.metadata["multi_agent_plan"])
                    if parent is not None and parent.metadata.get("multi_agent_plan")
                    else None
                )
                parent_step = next(
                    (
                        step
                        for step in (parent_plan.steps if parent_plan is not None else ())
                        if step.step_id == run.step_id
                    ),
                    None,
                )
                current_depth = parent_step.fork_depth if parent_step and parent_step.fork_depth else 0
            validation_context = ForkValidationContext(
                parent_effective_scope=parent_scope,
                session_scope=session_scope,
                workspace_scope=workspace_scope,
                parent_step_status=active_step.status,
                caller_kind=self._fork_caller_kind_for_run(run),
                created_by_run_id=run.run_id,
                current_depth=current_depth,
                # Coordinator generations are bounded separately from overall
                # DAG depth: root-created agents are the sole coordinator tier;
                # coordinators can create leaves only, never new coordinators.
                coordinator_depth=0,
                existing_child_count=max(existing_fork_count, len(run.child_run_ids)),
                known_step_ids=known_step_ids,
                current_objective_fingerprint=objective_fingerprint(run.user_input),
                ancestor_objective_fingerprints=tuple(dict.fromkeys(
                    objective_fingerprint(parent_run.user_input)
                    for parent_run in self._ancestor_runs(run)
                    if parent_run.user_input.strip()
                )),
            )
            policy_scope = ScopeGrant(
                workspace_paths=self.fork_policy.allowed_scope.workspace_paths,
                source_ids=tuple(sorted(set(parent_scope.source_ids) & set(session_scope.source_ids) & set(workspace_scope.source_ids))),
                account_ids=tuple(sorted(set(parent_scope.account_ids) & set(session_scope.account_ids) & set(workspace_scope.account_ids))),
                allowed_packages=self.fork_policy.allowed_scope.allowed_packages,
                allowed_tools=self.fork_policy.allowed_scope.allowed_tools,
                side_effect_level=self.fork_policy.allowed_scope.side_effect_level,
            )
            effective_policy = self.fork_policy.model_copy(update={"allowed_scope": policy_scope})
            validated = validate_fork_subtasks(
                fork_operation,
                policy=effective_policy,
                context=validation_context,
            )
            updated_plan = Plan.model_validate(
                {
                    **plan.model_dump(mode="python"),
                    "steps": (*plan.steps, *validated.validated_steps),
                }
            )
            payload = {
                "operation_id": fork_operation.operation_id,
                "requested_operation": fork_operation.model_dump(mode="json"),
                "validated_steps": [
                    step.model_dump(mode="json") for step in validated.validated_steps
                ],
                "scope_adjustments": [
                    adjustment.model_dump(mode="json")
                    for adjustment in validated.scope_adjustments
                ],
                "plan_id": updated_plan.plan_id,
                "status": "validated",
            }
            self.run_manager.record_multi_agent_plan(
                run_id,
                event_type="fork_subtasks_validated",
                payload=payload,
                plan=updated_plan.model_dump(mode="json"),
            )
            return self._execute_fork_plan(
                run_id=run_id,
                operation_id=fork_operation.operation_id,
                step_ids=[step.step_id for step in validated.validated_steps],
            )
        except (ForkPolicyViolation, ValueError, ValidationError) as exc:
            error = str(exc)
            payload = {
                "operation_id": operation_id,
                "requested_operation": raw_operation,
                "status": "rejected",
                "error": error,
            }
            self.run_manager.record_multi_agent_plan(
                run_id,
                event_type="fork_subtasks_rejected",
                payload=payload,
            )
            return {"status": "rejected", "operation_id": operation_id, "message": error}

    def _ancestor_runs(self, run: Any) -> list[Any]:
        ancestors = []
        seen = {run.run_id}
        parent_run_id = run.parent_run_id
        while parent_run_id and parent_run_id not in seen:
            seen.add(parent_run_id)
            parent = self.run_manager.get_run(parent_run_id) if self.run_manager is not None else None
            if parent is None:
                break
            ancestors.append(parent)
            parent_run_id = parent.parent_run_id
        return ancestors

    @staticmethod
    def _planner_progress_summary(outcome: dict[str, Any]) -> dict[str, Any]:
        """Keep progress metadata compact; authoritative task data stays internal."""
        aggregate = outcome.get("aggregate")
        verification = outcome.get("verification")
        summary = {
            key: outcome[key]
            for key in (
                "status", "operation_id", "plan_id", "patch_revision",
                "execution_status", "waiting_child_run_ids", "failed_step_ids",
                "replan_required",
            )
            if key in outcome
        }
        summary["aggregation_status"] = aggregate.get("status") if isinstance(aggregate, dict) else None
        summary["verification_status"] = verification.get("status") if isinstance(verification, dict) else None
        summary["task_result_count"] = len(outcome.get("task_results", ()))
        return summary

    def _handle_plan_patch_decision(
        self,
        *,
        run_id: str | None,
        operation: Any,
        parse_error: str | None = None,
    ) -> dict[str, Any]:
        if run_id is None or self.run_manager is None:
            return {"status": "rejected", "message": "Run persistence is unavailable."}
        run = self.run_manager.get_run(run_id)
        if run is None:
            return {"status": "rejected", "message": "Agent run no longer exists."}
        if self.fork_policy is None:
            parse_error = parse_error or "Structured multi-agent planning is disabled by server policy."
        if parse_error:
            self._record_plan_patch_rejection(
                run_id, operation, "Structured patch payload was rejected.", code="invalid_patch"
            )
            return {"status": "rejected", "message": parse_error}
        try:
            patch = PlanPatch.model_validate(operation)
            if run.status.value not in {"running", "waiting_user"}:
                raise PlanPatchRejected("Plan patch requires an active parent run.")
            raw_plan = run.metadata.get("multi_agent_plan")
            if not isinstance(raw_plan, dict):
                raise PlanPatchRejected("No persisted multi-Agent plan exists for this run.")
            plan = Plan.model_validate(raw_plan)
            prior_patch = next(
                (record.patch for record in plan.patch_history if record.patch.patch_id == patch.patch_id),
                None,
            )
            if prior_patch is not None:
                if prior_patch != patch:
                    raise PlanPatchRejected("Patch ID was already used for a different operation.")
                if prior_patch.operation == PlanPatchOperation.ASK_USER:
                    return self._continue_or_wait_for_plan_question(
                        run=run,
                        patch=prior_patch,
                        plan=plan,
                    )
                if patch.operation == PlanPatchOperation.ABORT:
                    self.run_manager._update_run(
                        run_id,
                        status=run.status,
                        metadata_patch={"multi_agent_replan_required": False},
                    )
                    return {
                        "status": "aborted", "plan_id": plan.plan_id,
                        "patch_revision": plan.patch_revision,
                        "message": "Previously persisted abort patch replayed idempotently.",
                    }
                target_ids = [patch.target_step_id] if patch.target_step_id else []
                if patch.alternative_step is not None:
                    target_ids.append(patch.alternative_step.step_id)
                resumed = self._execute_fork_plan(
                    run_id=run_id,
                    operation_id=f"patch:{patch.patch_id}",
                    step_ids=target_ids,
                )
                return {
                    **resumed,
                    "status": "applied",
                    "patch_revision": plan.patch_revision,
                    "patch_operation": patch.operation.value,
                    "message": "Previously persisted PlanPatch resumed idempotently.",
                }
            if not run.metadata.get("multi_agent_replan_required"):
                raise PlanPatchRejected("The persisted plan does not currently require replanning.")
            scope_resolver = getattr(self, "fork_scope_resolver", None)
            if scope_resolver is not None:
                parent_scope, session_scope, workspace_scope = scope_resolver(run_id)
            elif run.parent_run_id is not None:
                attached = run.metadata.get("context_snapshot", {})
                parent_scope = ScopeGrant.model_validate(attached.get("effective_scope", {}))
                session_scope = workspace_scope = parent_scope
            else:
                parent_scope = session_scope = workspace_scope = self.fork_policy.allowed_scope
            policy_scope = ScopeGrant(
                workspace_paths=self.fork_policy.allowed_scope.workspace_paths,
                source_ids=tuple(sorted(set(parent_scope.source_ids) & set(session_scope.source_ids) & set(workspace_scope.source_ids))),
                account_ids=tuple(sorted(set(parent_scope.account_ids) & set(session_scope.account_ids) & set(workspace_scope.account_ids))),
                allowed_packages=self.fork_policy.allowed_scope.allowed_packages,
                allowed_tools=self.fork_policy.allowed_scope.allowed_tools,
                side_effect_level=self.fork_policy.allowed_scope.side_effect_level,
            )
            effective_policy = self.fork_policy.model_copy(update={"allowed_scope": policy_scope})
            target = next((item for item in plan.steps if item.step_id == patch.target_step_id), None)
            if patch.budget is not None and (target is None or target.budget is None):
                raise PlanPatchRejected("A patch cannot introduce a budget without a server-owned step budget ceiling.")
            application = apply_plan_patch(
                plan,
                patch,
                PlanPatchContext(
                    fork_policy=effective_policy,
                    parent_effective_scope=parent_scope,
                    session_scope=session_scope,
                    workspace_scope=workspace_scope,
                    remaining_budget=target.budget if target is not None else None,
                    max_retries_per_step=int(getattr(self, "multi_agent_max_retries", 1)),
                ),
            )
            updated = application.plan
            payload = {
                "plan_id": updated.plan_id,
                "patch": patch.model_dump(mode="json"),
                "patch_record": application.record.model_dump(mode="json"),
                "patch_revision": updated.patch_revision,
                "status": updated.status.value,
            }
            self.run_manager.record_multi_agent_plan(
                run_id,
                event_type="multi_agent_plan_patched",
                payload=payload,
                plan=updated.model_dump(mode="json"),
            )
            if application.requires_user_input:
                return self._continue_or_wait_for_plan_question(
                    run=run,
                    patch=patch,
                    plan=updated,
                )
            if application.aborted:
                self.run_manager._update_run(
                    run_id, status=run.status, metadata_patch={"multi_agent_replan_required": False}
                )
                return {
                    "status": "aborted", "plan_id": updated.plan_id,
                    "patch_revision": updated.patch_revision,
                    "message": "Plan was explicitly aborted by a validated patch.",
                }
            target_ids = [patch.target_step_id] if patch.target_step_id else []
            if patch.alternative_step is not None:
                target_ids.append(patch.alternative_step.step_id)
            outcome = self._execute_fork_plan(
                run_id=run_id,
                operation_id=f"patch:{patch.patch_id}",
                step_ids=target_ids,
            )
            return {
                **outcome,
                "status": "applied",
                "patch_revision": updated.patch_revision,
                "patch_operation": patch.operation.value,
                "message": "Validated PlanPatch was applied and the DAG resumed.",
            }
        except (PlanPatchRejected, ValidationError, ValueError) as exc:
            safe_reason = "Patch schema validation failed." if isinstance(exc, ValidationError) else str(exc)
            self._record_plan_patch_rejection(
                run_id,
                operation,
                safe_reason,
                code="policy_rejected" if isinstance(exc, PlanPatchRejected) else "invalid_patch",
            )
            return {"status": "rejected", "message": safe_reason}

    def _continue_or_wait_for_plan_question(
        self,
        *,
        run: Any,
        patch: PlanPatch,
        plan: Plan,
    ) -> dict[str, Any]:
        """Repair/serve an ASK_USER patch from the durable question journal."""
        if self.run_manager is None:
            raise PlanPatchRejected("Run persistence is unavailable.")
        question = (patch.user_question or "").strip()
        if not question:
            raise PlanPatchRejected("ASK_USER patches require a question.")
        question_id = _stable_id("multi_agent_user_question", run.run_id, patch.patch_id)
        current = self.run_manager.get_run(run.run_id)
        if current is None:
            raise PlanPatchRejected("Agent run no longer exists.")
        pending_question = current.metadata.get("pending_user_question")
        command_id = current.metadata.get("pending_user_answer_command_id")
        # A prior question remains in metadata after its answer is consumed so
        # graph replay can recover from a crash.  It is not an answer to a new,
        # sequential ASK_USER patch; correlate both the patch and question IDs.
        is_same_question = (
            isinstance(pending_question, dict)
            and pending_question.get("patch_id") == patch.patch_id
            and pending_question.get("question_id") == question_id
        )
        if is_same_question and isinstance(command_id, str) and command_id:
            answer = self.run_manager.get_user_continuation(run.run_id, command_id)
            if answer is not None:
                return {
                    "status": "answer_ready",
                    "plan_id": plan.plan_id,
                    "patch_revision": plan.patch_revision,
                    "question_id": question_id,
                    "command_id": command_id,
                    "message": "User continuation is available from the trusted journal.",
                }
        self.run_manager.mark_waiting_user(
            run.run_id,
            question_id=question_id,
            question=question,
            patch_id=patch.patch_id,
        )
        return {
            "status": "waiting_user",
            "plan_id": plan.plan_id,
            "patch_revision": plan.patch_revision,
            "question_id": question_id,
            "user_question": question,
            "message": "Plan paused for a user answer; this is not a safety approval request.",
        }

    def resume_user_question_plan(self, run_id: str, command_id: str) -> tuple[str, str] | None:
        """Move the persisted plan out of WAITING_USER after journal acceptance."""
        if self.run_manager is None:
            return None
        run = self.run_manager.get_run(run_id)
        if run is None or run.metadata.get("pending_user_answer_command_id") != command_id:
            return None
        answer = self.run_manager.get_user_continuation(run_id, command_id)
        question = run.metadata.get("pending_user_question")
        question_id = question.get("question_id") if isinstance(question, dict) else None
        if answer is None or not isinstance(question_id, str):
            return None
        raw_plan = run.metadata.get("multi_agent_plan")
        if isinstance(raw_plan, dict):
            plan = Plan.model_validate(raw_plan)
            if plan.status == PlanStatus.WAITING_USER:
                resumed = plan.transition_to(PlanStatus.REPLANNING)
                self.run_manager.record_multi_agent_plan(
                    run_id,
                    event_type="multi_agent_user_question_answered",
                    payload={"question_id": question_id, "command_id": command_id},
                    plan=resumed.model_dump(mode="json"),
                )
                if not any(
                    step.step_id != ROOT_COORDINATOR_STEP_ID
                    and step.status in {PlanStepStatus.FAILED, PlanStepStatus.BLOCKED}
                    for step in resumed.steps
                ):
                    self.run_manager._update_run(
                        run_id,
                        status=run.status,
                        metadata_patch={"multi_agent_replan_required": False},
                    )
        return question_id, answer

    def _record_plan_patch_rejection(
        self,
        run_id: str,
        operation: Any,
        reason: str,
        *,
        code: str,
    ) -> None:
        raw = operation if isinstance(operation, dict) else {}
        fingerprint = sha1(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        self.run_manager.append_event(
            run_id,
            "multi_agent_plan_patch_rejected",
            "Planner PlanPatch proposal was rejected.",
            stage="planner",
            payload={
                "patch_id": str(raw.get("patch_id") or "")[:200],
                "request_fingerprint": fingerprint,
                "failure_code": code,
                "reason": reason[:500],
            },
        )

    def _execute_fork_plan(
        self,
        *,
        run_id: str,
        operation_id: str,
        step_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        run = self.run_manager.get_run(run_id) if self.run_manager is not None else None
        raw_plan = run.metadata.get("multi_agent_plan") if run is not None else None
        plan = Plan.model_validate(raw_plan) if raw_plan else None
        fork_execution = getattr(self, "fork_execution", None)
        if fork_execution is None:
            return {
                "status": "validated",
                "operation_id": operation_id,
                "plan_id": plan.plan_id if plan else None,
                "step_ids": step_ids or [],
                "message": "DAG validated and recorded; no child scheduler is configured.",
                "task_results": [],
            }
        schedule = fork_execution(run_id)
        task_results = tuple(getattr(schedule, "task_results", ()))
        waiting = str(getattr(schedule, "status", "")) == "waiting_confirmation"
        waiting_user = str(getattr(schedule, "status", "")) == "waiting_user"
        if waiting and self.run_manager is not None:
            # Pending results are placeholders, not observations. The graph
            # checkpoints at the wait node and will rerun the fork operation
            # after approval, when durable terminal results can be supplied.
            task_results = ()
            waiting_ids = tuple(getattr(schedule, "waiting_child_run_ids", ()))
            confirmation_id = None
            for child_id in waiting_ids:
                child = self.run_manager.get_run(child_id)
                if child is not None and child.status.value == "waiting_confirmation":
                    confirmation_id = child.metadata.get("confirmation_id") or child_id
                    break
            self.run_manager.mark_waiting_confirmation(
                run_id,
                confirmation_id=str(confirmation_id or waiting_ids[0] if waiting_ids else "child_review_pending"),
            )
        elif waiting_user:
            # The child's pending question is exposed by its own run record;
            # the parent graph only parks until that child resumes.
            task_results = ()
        elif self.run_manager is not None and not waiting_user:
            current = self.run_manager.get_run(run_id)
            if current is not None and current.status.value == "waiting_confirmation":
                self.run_manager.resume_running(run_id)
        return {
            "status": "validated",
            "execution_status": str(getattr(schedule, "status", "running")),
            "operation_id": operation_id,
            "plan_id": plan.plan_id if plan else None,
            "step_ids": step_ids or [],
            "task_results": [
                result.model_dump(mode="json")
                if hasattr(result, "model_dump")
                else dict(result)
                for result in task_results
            ],
            "waiting_child_run_ids": list(getattr(schedule, "waiting_child_run_ids", ())),
            "failed_step_ids": list(getattr(schedule, "failed_step_ids", ())),
            "aggregate": (
                schedule.aggregate.model_dump(mode="json")
                if getattr(schedule, "aggregate", None) is not None else None
            ),
            "verification": (
                schedule.verification.model_dump(mode="json")
                if getattr(schedule, "verification", None) is not None else None
            ),
            "replan_required": bool(getattr(schedule, "replan_required", False)),
            "message": (
                "Child execution is waiting for safety confirmation."
                if waiting
                else "Child execution is waiting for a user response."
                if waiting_user
                else "Child plan execution advanced."
            ),
        }

    def _finalize_multi_agent_plan(self, run_id: str) -> None:
        plan_finalizer = getattr(self, "fork_plan_finalizer", None)
        if plan_finalizer is None or self.run_manager is None:
            return
        run = self.run_manager.get_run(run_id)
        raw_plan = run.metadata.get("multi_agent_plan") if run is not None else None
        if isinstance(raw_plan, dict) and raw_plan.get("parent_run_id") == run_id:
            plan_finalizer(run_id)

    @staticmethod
    def _tool_call_fingerprint(*, tool_name: str, tool_input: dict[str, Any]) -> str:
        return json.dumps(
            {"tool_name": tool_name, "tool_input": tool_input},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _completed_tool_call_changes_state(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        tool = self.tool_executor.registry.get_tool_or_none(tool_name)
        return tool is not None and effective_tool_read_only(tool, tool_input) is not True

    def _decide_next_action(
        self,
        *,
        user_input: str,
        route: dict[str, Any],
        context_window: dict[str, Any],
        package_catalog: list[dict[str, Any]],
        expanded_package_names: list[str],
        expanded_tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        llm_events: list[AgentTurnLLMEvent],
    ) -> dict[str, Any] | None:
        prompt_observations = self._observations_within_prompt_budget(observations)
        native_tools, native_tool_actions = self._native_decision_tools(
            package_catalog=package_catalog,
            expanded_tools=expanded_tools,
        )
        if native_tools and self._supports_function_calling():
            native_decision = self._decide_next_action_with_native_tools(
                user_input=user_input,
                route=route,
                context_window=context_window,
                package_catalog=package_catalog,
                expanded_package_names=expanded_package_names,
                expanded_tools=expanded_tools,
                observations=prompt_observations,
                completed_tool_calls=self._completed_tool_call_summaries(observations),
                tools=native_tools,
                actions=native_tool_actions,
                llm_events=llm_events,
            )
            if native_decision is not None:
                return native_decision
        operation_types = "tool_call|expand_package|final_answer|request_confirmation|no_op"
        fork_guidance = ""
        if self.fork_policy is not None:
            operation_types += "|fork_subtasks"
            operation_types += "|plan_patch"
            fork_guidance = (
                f" You may propose fork_subtasks only for independent work that benefits from "
                f"separate bounded agents. Use parent_step_id={ROOT_COORDINATOR_STEP_ID!r}; "
                "The operation fields are operation_id, parent_step_id, and subtasks; every "
                "subtask needs step_id, objective, and output_contract, and may include "
                "depends_on, requested_scope, input_refs, verification_criteria, and "
                f"agent_id (registered choices: {self.fork_policy.allowed_agent_ids}; "
                "omit it for the default general Agent). "
                f"inference_profile_id may only be one of the server-allowlisted IDs: {self.fork_policy.allowed_inference_profile_ids}. "
                "If no profile is allowlisted, omit inference_profile_id and use the existing request/default model selection. "
                "When prior results require re-planning, use plan_patch with a bounded structured "
                "retry_step, reduced_scope, alternative_step, skip_and_degrade, ask_user, or abort. "
                "A patch supplies patch_id, plan_id, expected_revision, operation, reason, and only "
                "the fields required by that operation. ask_user pauses this run for a distinct "
                "user answer; it is separate from safety approval. "
                "Patches cannot increase any scope or policy limit. "
                "The server applies the caller's registered-tool, session, workspace, safety, "
                "budget, and fork-depth limits, then schedules child Agents. A child's results "
                "return as observations before you continue. You may fork recursively only when "
                "the server's depth policy allows it. Natural language or assistant_message "
                "never requests a fork."
            )
        base_system_prompt = (
            "You are the Main Agent Brain for Local Knowledge Agent OS. Choose the next "
            "single action for this agent turn. You may call one available tool, expand a "
            "package, request confirmation, or enter the final answer stage. "
            "Use expanded_package_names as the authoritative set of currently available "
            "packages. Use package_catalog, package decision_hints, expanded_tools, tool descriptions, input_schema, "
            "side_effects, risk, observations, and session context as the source of truth. "
            "Use tools only when more local evidence, deterministic context, or persistence is "
            "needed. Reuse cached observations when they are relevant. Before every tool call, "
            "check completed_tool_calls and observations: if a completed result already supplies "
            "the requested evidence, you must choose final_answer. Never repeat the same tool "
            "with identical input after it succeeded in this turn merely to increase confidence. "
            "Only when the user explicitly requested repeated execution may you repeat it; then "
            "set operation.repeat_successful_call=true and state that user requirement in reason. "
            "When observations and "
            "session context are sufficient to answer the user, return operation.type "
            "final_answer with a concise reason, then stop; do not write final natural "
            "language prose in this decision stage. If the currently expanded tools are insufficient, "
            "use operation.type expand_package with package_name set to one registered package; "
            "do not call tools from a package until that package appears in "
            "expanded_package_names. Follow each expanded tool input_schema exactly: include "
            "required fields, respect allowed_values/enums, and do not invent unsupported "
            "field values. Use the deterministic context supplied in session_context_window "
            "when resolving relative references. Return only strict JSON using this operation-first "
            "envelope: "
            f'{{"operation":{{"type":"{operation_types}",'
            '"package_name":null,"tool_name":"<expanded tool name or null>","tool_input":{},'
            '"repeat_successful_call":false,"final_answer":null,"reason":"...","confidence":"low|medium|high"},'
            '"assistant_message":"short user-visible progress text"}. '
            "The operation object is the only executable control channel and must come first. "
            "assistant_message is display-only progress text; it never selects tools and never "
            "becomes the final answer. For a final answer, set operation.type to final_answer, "
            "set operation.final_answer to null if present, and explain only why the answer "
            "stage can now run. For a tool call, put all executable details in operation and "
            "only optional progress text in assistant_message. Never return plain text outside "
            "JSON."
            + fork_guidance
        )
        decision_retry: dict[str, Any] | None = None
        for format_attempt in range(1, self.decision_format_max_attempts + 1):
            system_prompt = base_system_prompt
            if decision_retry is not None:
                system_prompt = (
                    f"{base_system_prompt} The previous decision output was rejected because "
                    f"{decision_retry['error']}. You must now correct that specific output. "
                    "Return only the JSON envelope. If the rejected text "
                    "said you would read, load, fetch, or inspect details, produce a "
                    "tool_call JSON operation using the available tools and stable resource "
                    "identifiers from observations. Do not repeat the rejected plain text."
                )
            prompt_payload = {
                "user_input": user_input,
                "session_context_window": self._context_window_for_llm(context_window),
                "route_context": self._route_context(route),
                "package_catalog": package_catalog,
                "expanded_package_names": expanded_package_names,
                "expanded_tools": expanded_tools,
                "observations": prompt_observations,
                "completed_tool_calls": self._completed_tool_call_summaries(observations),
            }
            if decision_retry is not None:
                prompt_payload["decision_retry"] = decision_retry
            user_prompt = json.dumps(
                prompt_payload,
                ensure_ascii=False,
                indent=2,
            )
            response = self._complete_text_with_retry(
                stage="decision",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_summary=(
                    f"agent_turn_decision observations={len(observations)} "
                    f"format_attempt={format_attempt}"
                ),
                max_output_tokens=self.llm_generation_token_budget,
                llm_events=llm_events,
            )
            if response is None:
                return None
            parsed = self._parse_json_object(response.content)
            if isinstance(parsed, dict) and parsed:
                return self._normalize_decision_output(parsed, raw_output=response.content)
            if self._looks_like_tool_operation(response.content):
                repaired = self._repair_malformed_decision_output(
                    raw_output=response.content,
                    user_input=user_input,
                    route=route,
                    expanded_tools=expanded_tools,
                    observations=prompt_observations,
                    llm_events=llm_events,
                )
                if repaired is not None:
                    return repaired
                return {
                    "action": "malformed_tool_call",
                    "reason": "LLM returned malformed JSON that looked like a tool call.",
                    "_raw_output": response.content,
                }
            answer = response.content.strip()
            if not answer:
                if format_attempt < self.decision_format_max_attempts:
                    decision_retry = {
                        "error": "previous_decision_output_was_empty",
                        "invalid_output": "",
                        "required_response": (
                            "Return strict JSON with operation first. Do not return an empty "
                            "response. Use tool_call if more evidence is needed; use "
                            "final_answer only to enter the separate answer stage when "
                            "observations are sufficient."
                        ),
                    }
                    continue
                return {
                    "action": "invalid_empty_decision",
                    "operation": {
                        "type": "invalid",
                        "reason": "Decision output was empty after the format retry.",
                    },
                    "reason": "Rejected empty decision output.",
                    "_raw_output": response.content,
                }
            if format_attempt < self.decision_format_max_attempts:
                decision_retry = {
                    "error": "previous_decision_output_was_plain_text_not_json",
                    "invalid_output": answer,
                    "required_response": (
                        "Return strict JSON with operation first. Use tool_call if more "
                        "evidence is needed; use final_answer only to enter the separate "
                        "answer stage when observations are sufficient."
                    ),
                }
                continue
            return {
                "action": "invalid_plain_text_decision",
                "assistant_message": answer,
                "operation": {
                    "type": "invalid",
                    "reason": "Decision output must be strict JSON with operation first.",
                },
                "reason": "Rejected non-JSON decision output.",
                "_raw_output": response.content,
            }
        return None

    def _supports_function_calling(self) -> bool:
        supports = getattr(self.llm_client, "supports_function_calling", None)
        if callable(supports):
            return bool(supports(client_name=_turn_llm_client_name.get()))
        return bool(supports)

    def _native_decision_tools(
        self,
        *,
        package_catalog: list[dict[str, Any]],
        expanded_tools: list[dict[str, Any]],
    ) -> tuple[list[LLMToolDefinition], dict[str, dict[str, Any]]]:
        definitions: list[LLMToolDefinition] = []
        actions: dict[str, dict[str, Any]] = {}
        for index, tool in enumerate(expanded_tools):
            tool_name = tool.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            function_name = f"tool_{index}_{tool_name.replace('.', '_').replace('-', '_')}"
            definitions.append(
                LLMToolDefinition(
                    name=function_name,
                    description=str(tool.get("description") or tool_name),
                    parameters=self._json_schema_from_tool_schema(tool.get("input_schema")),
                    strict=True,
                )
            )
            actions[function_name] = {"action": "call_tool", "tool_name": tool_name}

        package_names = [
            package.get("name")
            for package in package_catalog
            if isinstance(package.get("name"), str) and package.get("name")
        ]
        if package_names:
            function_name = "agent_expand_package"
            definitions.append(
                LLMToolDefinition(
                    name=function_name,
                    description="Expand one registered tool package so its tools become available.",
                    strict=True,
                    parameters={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["package_name"],
                        "properties": {"package_name": {"type": "string", "enum": package_names}},
                    },
                )
            )
            actions[function_name] = {"action": "expand_package"}
        if self.fork_policy is not None:
            definitions.append(
                LLMToolDefinition(
                    name="agent_fork_subtasks",
                    description=(
                        "Propose a bounded DAG of independent child tasks. The server applies "
                        "the caller's access and fork-depth limits and schedules child Agents."
                    ),
                    strict=False,
                    parameters=_fork_subtasks_function_schema(),
                )
            )
            actions["agent_fork_subtasks"] = {"action": "fork_subtasks"}
            run_id = _turn_run_id.get()
            run = self.run_manager.get_run(run_id) if self.run_manager is not None and run_id else None
            if run is not None and run.metadata.get("multi_agent_plan"):
                definitions.append(
                    LLMToolDefinition(
                        name="agent_plan_patch",
                        description="Apply a validated structured patch to a persisted multi-Agent plan after results require replanning.",
                        strict=False,
                        parameters=_plan_patch_function_schema(),
                    )
                )
                actions["agent_plan_patch"] = {"action": "plan_patch"}
        return definitions, actions

    def _decide_next_action_with_native_tools(
        self,
        *,
        user_input: str,
        route: dict[str, Any],
        context_window: dict[str, Any],
        package_catalog: list[dict[str, Any]],
        expanded_package_names: list[str],
        expanded_tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        completed_tool_calls: list[dict[str, Any]],
        tools: list[LLMToolDefinition],
        actions: dict[str, dict[str, Any]],
        llm_events: list[AgentTurnLLMEvent],
    ) -> dict[str, Any] | None:
        system_prompt = (
            "You are the Main Agent Brain for Local Knowledge Agent OS. Choose the next "
            "single action. Call exactly one provided function when a tool, package expansion, "
            "or enabled structured Planner operation is needed. When observations and session context are sufficient, do not "
            "call a function; return a short plain-text reason that the separate answer stage "
            "can now run. Never repeat a completed successful tool call with identical input "
            "unless the user explicitly requested it. Follow function schemas exactly."
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "session_context_window": self._context_window_for_llm(context_window),
                "route_context": self._route_context(route),
                "package_catalog": package_catalog,
                "expanded_package_names": expanded_package_names,
                "expanded_tools": expanded_tools,
                "observations": observations,
                "completed_tool_calls": completed_tool_calls,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="decision",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"agent_turn_native_decision observations={len(observations)}",
            max_output_tokens=self.llm_generation_token_budget,
            llm_events=llm_events,
            tools=tools,
            tool_choice="auto",
        )
        if response is None:
            return None
        if not response.tool_calls:
            return {
                "action": "final_answer",
                "answer": None,
                "assistant_message": response.content.strip() or None,
                "operation": {"type": "final_answer", "reason": "Native tool selection completed."},
                "reason": "Provider selected no further tool call.",
                "_raw_output": response.content,
            }
        if len(response.tool_calls) != 1:
            return {
                "action": "malformed_tool_call",
                "reason": "Provider returned more than one tool call for a single-action step.",
                "_raw_output": response.content,
            }
        call = response.tool_calls[0]
        action = actions.get(call.name)
        if action is None or call.arguments is None:
            return {
                "action": "malformed_tool_call",
                "reason": "Provider returned an unknown function or invalid function arguments.",
                "_raw_output": response.content,
            }
        if action["action"] == "call_tool":
            return {
                "action": "call_tool",
                "tool_name": action["tool_name"],
                "tool_input": call.arguments,
                "operation": {
                    "type": "tool_call",
                    "tool_name": action["tool_name"],
                    "tool_input": call.arguments,
                },
                "reason": "Provider-native function call.",
                "_raw_output": response.content,
            }
        if action["action"] == "fork_subtasks":
            return self._normalize_decision_output(
                {"operation": {"type": "fork_subtasks", **call.arguments}},
                raw_output=response.content,
            )
        if action["action"] == "plan_patch":
            return self._normalize_decision_output(
                {"operation": {"type": "plan_patch", **call.arguments}},
                raw_output=response.content,
            )
        package_name = call.arguments.get("package_name")
        return {
            "action": "expand_package",
            "package_name": package_name,
            "operation": {"type": "expand_package", "package_name": package_name},
            "reason": "Provider-native function call.",
            "_raw_output": response.content,
        }

    def _json_schema_from_tool_schema(self, schema: Any) -> dict[str, Any]:
        if not isinstance(schema, dict):
            return {"type": "object", "additionalProperties": False, "properties": {}}
        if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
            source = schema
        else:
            source = {"type": "object", "properties": schema}
        properties = source.get("properties")
        normalized_properties = {
            key: self._json_schema_value(value)
            for key, value in properties.items()
            if isinstance(key, str)
        }
        required = source.get("required")
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": normalized_properties,
            "required": [key for key in required if isinstance(key, str)]
            if isinstance(required, list)
            else [],
        }

    def _json_schema_value(self, value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            return {"type": value}
        if not isinstance(value, dict):
            return {}
        result = {
            key: item
            for key, item in value.items()
            if key not in {"allowed_values", "properties", "items", "required"}
        }
        if isinstance(value.get("allowed_values"), list):
            result["enum"] = value["allowed_values"]
        if isinstance(value.get("properties"), dict):
            result["properties"] = {
                key: self._json_schema_value(item)
                for key, item in value["properties"].items()
                if isinstance(key, str)
            }
            result["additionalProperties"] = False
        if isinstance(value.get("required"), list):
            result["required"] = [key for key in value["required"] if isinstance(key, str)]
        if isinstance(value.get("items"), dict):
            result["items"] = self._json_schema_value(value["items"])
        return result

    def _record_route_decision(
        self,
        decision_events: list[AgentTurnDecisionEvent],
        *,
        source: str,
        route: dict[str, Any],
        raw_output: str | None,
    ) -> None:
        selected_package = route.get("selected_package")
        self._record_decision(
            decision_events,
            source=source,
            action="select_package",
            selected_package=selected_package if isinstance(selected_package, str) else None,
            reason=route.get("reason") if isinstance(route.get("reason"), str) else None,
            raw_output=raw_output,
        )

    def _record_decision(
        self,
        decision_events: list[AgentTurnDecisionEvent],
        *,
        source: str,
        action: str,
        selected_package: str | None = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        answer: str | None = None,
        reason: str | None = None,
        assistant_message: str | None = None,
        operation: dict[str, Any] | None = None,
        raw_output: str | None = None,
        step_index: int | None = None,
    ) -> None:
        decision_events.append(
            AgentTurnDecisionEvent(
                step_index=step_index or len(decision_events) + 1,
                decided_at=_now_iso(),
                source=source,
                action=action,
                selected_package=selected_package,
                tool_name=tool_name,
                tool_input=tool_input or {},
                answer=answer,
                reason=reason,
                assistant_message=assistant_message,
                operation=operation or {},
                raw_output=raw_output,
            )
        )

    def _normalize_decision_output(
        self,
        parsed: dict[str, Any],
        *,
        raw_output: str,
    ) -> dict[str, Any]:
        operation = parsed.get("operation")
        if isinstance(operation, dict):
            operation_type = str(operation.get("type") or "").strip()
            assistant_message = (
                parsed.get("assistant_message")
                if isinstance(parsed.get("assistant_message"), str)
                else None
            )
            reason = (
                operation.get("reason")
                if isinstance(operation.get("reason"), str)
                else parsed.get("reason")
                if isinstance(parsed.get("reason"), str)
                else None
            )
            if operation_type == "fork_subtasks":
                request_payload = {
                    key: value for key, value in operation.items() if key != "type"
                }
                request_payload["operation"] = "fork_subtasks"
                request_payload.setdefault(
                    "correlation_id",
                    _stable_id(
                        "fork_correlation",
                        _turn_run_id.get(),
                        str(request_payload.get("operation_id") or "unknown"),
                    ),
                )
                try:
                    fork_operation = ForkSubtasksOperation.model_validate(request_payload)
                except ValidationError as exc:
                    return {
                        "action": "fork_subtasks_invalid",
                        "operation": operation,
                        "reason": str(exc),
                        "assistant_message": assistant_message,
                        "_raw_output": raw_output,
                    }
                return {
                    "action": "fork_subtasks",
                    "operation": fork_operation.model_dump(mode="json"),
                    "assistant_message": assistant_message,
                    "reason": reason,
                    "_raw_output": raw_output,
                }
            if operation_type == "plan_patch":
                try:
                    patch = PlanPatch.model_validate({key: value for key, value in operation.items() if key != "type"})
                except ValidationError as exc:
                    return {
                        "action": "plan_patch_invalid",
                        "operation": operation,
                        "reason": str(exc),
                        "assistant_message": assistant_message,
                        "_raw_output": raw_output,
                    }
                return {
                    "action": "plan_patch",
                    "operation": patch.model_dump(mode="json"),
                    "assistant_message": assistant_message,
                    "reason": reason,
                    "_raw_output": raw_output,
                }
            if operation_type in {"tool_call", "call_tool"}:
                return {
                    "action": "call_tool",
                    "tool_name": operation.get("tool_name"),
                    "tool_input": operation.get("tool_input")
                    if isinstance(operation.get("tool_input"), dict)
                    else {},
                    "assistant_message": assistant_message,
                    "operation": operation,
                    "reason": reason,
                    "_raw_output": raw_output,
                }
            if operation_type == "expand_package":
                return {
                    "action": "expand_package",
                    "package_name": operation.get("package_name"),
                    "assistant_message": assistant_message,
                    "operation": operation,
                    "reason": reason,
                    "_raw_output": raw_output,
                }
            if operation_type == "final_answer":
                return {
                    "action": "final_answer",
                    "answer": None,
                    "assistant_message": assistant_message,
                    "operation": operation,
                    "reason": reason,
                    "_raw_output": raw_output,
                }
            if operation_type in {"request_confirmation", "no_op"}:
                answer = assistant_message or reason or ""
                return {
                    "action": operation_type,
                    "answer": answer,
                    "assistant_message": assistant_message,
                    "operation": operation,
                    "reason": reason,
                    "_raw_output": raw_output,
                }

        action = parsed.get("action")
        if isinstance(action, str):
            normalized = dict(parsed)
            normalized["_raw_output"] = raw_output
            if action == "expand_package" and not isinstance(
                normalized.get("package_name"),
                str,
            ):
                package_name = parsed.get("selected_package")
                if isinstance(package_name, str):
                    normalized["package_name"] = package_name
            if "assistant_message" not in normalized and isinstance(parsed.get("answer"), str):
                normalized["assistant_message"] = parsed["answer"]
            if "operation" not in normalized:
                if action == "call_tool":
                    normalized["operation"] = {
                        "type": "tool_call",
                        "tool_name": parsed.get("tool_name"),
                        "tool_input": parsed.get("tool_input")
                        if isinstance(parsed.get("tool_input"), dict)
                        else {},
                        "final_answer": None,
                        "reason": parsed.get("reason"),
                    }
                elif action == "expand_package":
                    normalized["operation"] = {
                        "type": "expand_package",
                        "package_name": parsed.get("package_name")
                        or parsed.get("selected_package"),
                        "reason": parsed.get("reason"),
                    }
                elif action == "answer":
                    normalized["operation"] = {
                        "type": "final_answer",
                        "tool_name": None,
                        "tool_input": {},
                        "final_answer": None,
                        "reason": parsed.get("reason"),
                    }
                    normalized["action"] = "final_answer"
                    normalized["answer"] = None
            return normalized

        parsed["_raw_output"] = raw_output
        return parsed

    def _looks_like_tool_operation(self, raw_output: str) -> bool:
        compact = "".join(raw_output.lower().split())
        return any(
            marker in compact
            for marker in [
                '"action":"call_tool"',
                '"type":"tool_call"',
                '"tool_name"',
                '"tool_input"',
            ]
        )

    def _repair_malformed_decision_output(
        self,
        *,
        raw_output: str,
        user_input: str,
        route: dict[str, Any],
        expanded_tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        llm_events: list[AgentTurnLLMEvent],
    ) -> dict[str, Any] | None:
        if self.llm_client is None:
            return None
        system_prompt = (
            "You repair one malformed Main Agent Brain decision. Return only strict JSON "
            'using the operation-first envelope: {"operation":{"type":'
            '"tool_call|expand_package|final_answer|request_confirmation|no_op",'
            '"package_name":null,"tool_name":null,"tool_input":{},'
            '"final_answer":null,"reason":"...",'
            '"confidence":"low|medium|high"},"assistant_message":"..."}. '
            "Preserve a tool call only when the "
            "malformed output clearly includes the tool name and complete tool input. Do not "
            "invent missing required tool arguments."
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "route_context": self._route_context(route),
                "expanded_tools": expanded_tools,
                "observations": observations,
                "malformed_output": raw_output,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="decision_repair",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary="agent_turn_decision_repair",
            max_output_tokens=self.llm_generation_token_budget,
            llm_events=llm_events,
        )
        if response is None:
            return None
        parsed = self._parse_json_object(response.content)
        if not isinstance(parsed, dict) or not parsed:
            return None
        normalized = self._normalize_decision_output(
            parsed,
            raw_output=response.content,
        )
        if normalized.get("action") not in {"call_tool", "expand_package", "final_answer"}:
            return None
        normalized["_raw_output"] = raw_output
        normalized["_repair_output"] = response.content
        normalized["reason"] = (
            normalized.get("reason") or "Repaired malformed tool-call decision output."
        )
        return normalized

    def _tool_view_for_run(self, run_id: str) -> ToolView | None:
        if self.run_manager is None:
            return None
        run = self.run_manager.get_run(run_id)
        views = run.metadata.get("context_views", {}) if run is not None else {}
        tool_view_data = views.get("tool") if isinstance(views, dict) else None
        return ToolView.model_validate(tool_view_data) if tool_view_data else None

    def _configured_fast_path_request(
        self, *, run_id: str, session_id: str, user_input: str
    ) -> FastPathRequest | None:
        policy = self.fast_path_policy
        if policy is None or not policy.enabled or self.run_manager is None:
            return None
        run = self.run_manager.get_run(run_id)
        if run is None:
            return None
        registered = self.tool_executor.registry.list_tools()
        tool_view = self._tool_view_for_run(run_id)
        if tool_view is None:
            allowed = registered
            workspace_paths: tuple[str, ...] = ()
            source_ids: tuple[str, ...] = ()
            account_ids: tuple[str, ...] = ()
            side_effect_level = SideEffectLevel.EXTERNAL
        else:
            allowed = [
                spec
                for spec in registered
                if tool_view.allows_tool(
                    tool_name=spec.name,
                    package=spec.package,
                    read_only=effective_tool_read_only(spec, {}),
                )
            ]
            workspace_paths = tool_view.allowed_paths
            source_ids = tool_view.allowed_source_ids
            account_ids = tool_view.allowed_account_ids
            side_effect_level = tool_view.side_effect_level
        tool_names = tuple(sorted(spec.name for spec in allowed))
        package_names = tuple(sorted({spec.package for spec in allowed if spec.package}))
        if not tool_names or not package_names:
            return None
        return FastPathRequest(
            correlation_id=f"fast-path:{run_id}",
            run_id=run_id,
            session_id=session_id,
            user_input=user_input,
            objective=user_input,
            requested_template=FastPathTemplateKind.SINGLE_AGENT,
            authorized_scope=ScopeGrant(
                workspace_paths=workspace_paths,
                source_ids=source_ids,
                account_ids=account_ids,
                allowed_packages=package_names,
                allowed_tools=tool_names,
                side_effect_level=side_effect_level,
            ),
            react_agent_available=True,
        )

    def _assess_configured_fast_path(
        self, *, run_id: str, session_id: str, user_input: str
    ) -> None:
        if self.run_manager is None or self.fast_path_policy is None:
            return
        prior_events = self.run_manager.list_events(run_id)
        if any(event.type.startswith("fast_path_") for event in prior_events):
            return
        request = self._configured_fast_path_request(
            run_id=run_id, session_id=session_id, user_input=user_input
        )
        if request is None:
            event = FastPathEvent(
                correlation_id=f"fast-path:{run_id}",
                event_type=FastPathEventType.UPGRADED,
                run_id=run_id,
                template_id=FastPathTemplateKind.SINGLE_AGENT,
                reason_code="fast_path_agent_unavailable",
            )
            self.run_manager.append_event(
                run_id,
                "fast_path_upgraded",
                "Configured fast-path could not establish an Agent capability scope; ordinary ReAct continues.",
                stage="fast_path",
                payload={"event": event.model_dump(mode="json"), "reason_code": event.reason_code},
            )
            return
        decision = assess_fast_path(request, self.fast_path_policy, self.tool_executor.registry)
        if decision.disposition == FastPathDisposition.MATCHED and decision.plan is not None:
            self.run_manager.append_event(
                run_id,
                "fast_path_hit",
                "Configured single-agent fast-path policy validated; ordinary ReAct execution continues.",
                stage="fast_path",
                payload={
                    "event": decision.event.model_dump(mode="json"),
                    "template_id": decision.template.template_id.value if decision.template else None,
                    "plan": decision.plan.model_dump(mode="json"),
                },
            )
            return
        self.run_manager.append_event(
            run_id,
            "fast_path_upgraded",
            "Fast-path policy did not match; the ordinary ReAct path continues.",
            stage="fast_path",
            payload={
                "event": decision.event.model_dump(mode="json"),
                "reason_code": decision.reason_code,
                "escalation": (
                    decision.escalation.model_dump(mode="json")
                    if decision.escalation is not None
                    else None
                ),
            },
        )

    def _complete_configured_fast_path(self, *, run_id: str, answer: str) -> None:
        if self.run_manager is None or self.fast_path_policy is None:
            return
        events = self.run_manager.list_events(run_id)
        hit = next((event for event in events if event.type == "fast_path_hit"), None)
        if hit is None or any(
            event.type in {"fast_path_completed", "fast_path_upgraded", "fast_path_error_completion"}
            for event in events
            if event.sequence > hit.sequence
        ):
            return
        payload = hit.payload
        try:
            plan = Plan.model_validate(payload["plan"])
            template_id = FastPathTemplateKind(str(payload["template_id"]))
            template = next(
                item for item in self.fast_path_policy.templates if item.template_id == template_id
            )
            run = self.run_manager.get_run(run_id)
            if run is None:
                return
            request = self._configured_fast_path_request(
                run_id=run_id, session_id=run.session_id, user_input=run.user_input
            )
            if request is None:
                return
            completion = assess_fast_path_completion(
                request=request,
                template=template,
                plan=plan,
                task_results=(),
                evidence_refs=(),
                answer_text=answer,
            )
        except (KeyError, StopIteration, TypeError, ValueError):
            self.run_manager.append_event(
                run_id,
                "fast_path_error_completion",
                "Fast-path completion could not be verified; ordinary run result is retained.",
                stage="fast_path",
                payload={"reason_code": "fast_path_completion_record_invalid"},
            )
            return
        event_type = {
            FastPathEventType.COMPLETED: "fast_path_completed",
            FastPathEventType.UPGRADED: "fast_path_upgraded",
            FastPathEventType.ERROR_COMPLETION: "fast_path_error_completion",
            FastPathEventType.HIT: "fast_path_hit",
            FastPathEventType.BYPASSED: "fast_path_upgraded",
        }[completion.event.event_type]
        self.run_manager.append_event(
            run_id,
            event_type,
            "Configured fast-path completion assessed; ordinary Agent answer remains authoritative.",
            stage="fast_path",
            payload={
                "event": completion.event.model_dump(mode="json"),
                "completed": completion.completed,
                "reason_code": completion.reason_code,
                "escalation": (
                    completion.escalation.model_dump(mode="json")
                    if completion.escalation is not None
                    else None
                ),
            },
        )

    def _upgrade_fast_path_for_multi_agent(self, run_id: str) -> None:
        """Record a safe escalation before honoring an explicit Planner fork."""
        if self.run_manager is None or self.fast_path_policy is None:
            return
        events = self.run_manager.list_events(run_id)
        hit = next((event for event in events if event.type == "fast_path_hit"), None)
        if hit is None or any(
            event.type == "fast_path_upgraded" and event.sequence > hit.sequence
            for event in events
        ):
            return
        event = FastPathEvent(
            correlation_id=f"fast-path:{run_id}",
            event_type=FastPathEventType.UPGRADED,
            run_id=run_id,
            template_id=FastPathTemplateKind.SINGLE_AGENT,
            reason_code="planner_requested_multi_agent",
        )
        self.run_manager.append_event(
            run_id,
            "fast_path_upgraded",
            "Planner requested multi-agent work; continuing through the existing scheduler path.",
            stage="fast_path",
            payload={
                "event": event.model_dump(mode="json"),
                "reason_code": event.reason_code,
            },
        )

    def _tools_for_package(
        self,
        package_name: str,
        *,
        tool_view: ToolView | None = None,
    ) -> list[Any]:
        tools = self.tool_executor.registry.list_tools(package=package_name)
        if tool_view is None:
            return tools
        return [
            tool
            for tool in tools
            if tool_view.allows_tool(
                tool_name=tool.name,
                package=tool.package,
                read_only=(
                    effective_tool_read_only(
                        self.tool_executor.registry.get_tool(tool.name), {}
                    )
                ),
            )
        ]

    def _package_catalog(self, tool_view: ToolView | None = None) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for package in self.tool_executor.registry.list_packages():
            tools = self._tools_for_package(package.name, tool_view=tool_view)
            if not tools:
                continue
            payload = package.model_dump(mode="json")
            payload["tool_names"] = [tool.name for tool in tools]
            catalog.append(payload)
        return catalog

    def _package_exists(
        self,
        package_name: str,
        *,
        tool_view: ToolView | None = None,
    ) -> bool:
        return bool(self._tools_for_package(package_name, tool_view=tool_view))

    def _tool_payloads_for_package(
        self,
        package_name: str,
        *,
        tool_view: ToolView | None = None,
    ) -> list[dict[str, Any]]:
        return [
            tool.model_dump(mode="json")
            for tool in self._tools_for_package(package_name, tool_view=tool_view)
        ]

    def _route_context(self, route: dict[str, Any]) -> dict[str, Any]:
        return {
            key: route.get(key) for key in ("reason", "search_query") if route.get(key) is not None
        }

    def _package_for_tool(self, tool_name: str) -> str | None:
        tool = self.tool_executor.registry.get_tool_or_none(tool_name)
        if tool is None:
            return None
        return tool.spec.package

    def _packages_from_expanded_tools(self, expanded_tools: list[dict[str, Any]]) -> list[str]:
        packages: list[str] = []
        for tool in expanded_tools:
            package = tool.get("package")
            if isinstance(package, str) and package not in packages:
                packages.append(package)
        return packages

    def _packages_from_tool_events(
        self,
        tool_events: list[AgentTurnToolEvent],
    ) -> list[str]:
        packages: list[str] = []
        for event in tool_events:
            package = self._package_for_tool(event.tool_name)
            if package and package not in packages:
                packages.append(package)
        return packages

    def _cached_tool_observations_from_session(
        self,
        *,
        session_id: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        cacheable_tool_names = self._cacheable_tool_names()
        if not cacheable_tool_names:
            return []
        detail = self.session_service.get_session(session_id=session_id)
        cached_observations: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        for session_message in reversed(detail.messages):
            tool_events = session_message.payload.get("tool_events")
            if not isinstance(tool_events, list):
                continue
            for tool_event in reversed(tool_events):
                if not isinstance(tool_event, dict):
                    continue
                tool_name = tool_event.get("tool_name")
                if not isinstance(tool_name, str) or tool_name not in cacheable_tool_names:
                    continue
                result = tool_event.get("result")
                if not isinstance(result, dict):
                    continue
                fingerprint = json.dumps(
                    {
                        "tool_name": tool_name,
                        "input": tool_event.get("input"),
                        "result": result,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if fingerprint in seen_fingerprints:
                    continue
                seen_fingerprints.add(fingerprint)
                cached_observations.append(
                    self._cached_observation_for_decision_prompt(
                        tool_name=tool_name,
                        tool_input=tool_event.get("input")
                        if isinstance(tool_event.get("input"), dict)
                        else {},
                        result=result,
                        feedback=tool_event.get("feedback")
                        if isinstance(tool_event.get("feedback"), dict)
                        else {},
                        cache_info={
                            "source": "session_tool_result",
                            "trace_id": session_message.payload.get("trace_id"),
                            "log_path": session_message.payload.get("log_path"),
                        },
                    )
                )
                if len(cached_observations) >= limit:
                    return list(reversed(cached_observations))
        return list(reversed(cached_observations))

    def _cacheable_tool_names(self) -> set[str]:
        cacheable: set[str] = set()
        for package in self.tool_executor.registry.list_packages():
            tool_names = package.observation_cache.get("tool_names")
            if not isinstance(tool_names, list):
                continue
            cacheable.update(tool_name for tool_name in tool_names if isinstance(tool_name, str))
        return cacheable

    def _cached_tool_observations_from_context_window(
        self,
        context_window: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cached_observations = context_window.get("cached_tool_observations")
        if not isinstance(cached_observations, list):
            return []
        observations: list[dict[str, Any]] = []
        for observation in cached_observations:
            if not isinstance(observation, dict):
                continue
            tool_name = observation.get("tool_name")
            result = observation.get("result")
            if not isinstance(tool_name, str) or not isinstance(result, dict):
                continue
            observation_payload = {
                "tool_name": tool_name,
                "input": observation.get("input")
                if isinstance(observation.get("input"), dict)
                else {},
            }
            compacted_result, compacted = self._compact_for_decision_prompt(result)
            observation_payload["result"] = compacted_result
            feedback = observation.get("feedback")
            if isinstance(feedback, dict):
                observation_payload["feedback"] = feedback
            cache_info = observation.get("_cache")
            if isinstance(cache_info, dict):
                observation_payload["_cache"] = {
                    "source": "session_tool_result",
                    **cache_info,
                }
            if compacted or observation.get("_prompt_compacted"):
                observation_payload["_prompt_compacted"] = True
            observations.append(observation_payload)
        return observations

    def _observation_for_decision_prompt(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: ToolResult,
        feedback: dict[str, Any],
    ) -> dict[str, Any]:
        result_payload = tool_result.model_dump(mode="json")
        compacted_result, compacted = self._compact_for_decision_prompt(result_payload)
        observation = {
            "tool_name": tool_name,
            "input": tool_input,
            "result": compacted_result,
            "feedback": feedback,
        }
        if compacted:
            observation["_prompt_compacted"] = True
        return observation

    def _cached_observation_for_decision_prompt(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        result: dict[str, Any],
        feedback: dict[str, Any],
        cache_info: dict[str, Any],
    ) -> dict[str, Any]:
        compacted_result, compacted = self._compact_for_decision_prompt(result)
        observation = {
            "tool_name": tool_name,
            "input": tool_input,
            "result": compacted_result,
            "feedback": feedback,
            "_cache": cache_info,
        }
        if compacted:
            observation["_prompt_compacted"] = True
        return observation

    def _compact_for_decision_prompt(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, str):
            if len(value) <= DECISION_OBSERVATION_MAX_STRING_CHARS:
                return value, False
            keep = DECISION_OBSERVATION_MAX_STRING_CHARS // 2
            omitted = len(value) - (keep * 2)
            return (
                value[:keep]
                + f"\n...[truncated {omitted} chars for decision prompt]...\n"
                + value[-keep:],
                True,
            )
        if isinstance(value, list):
            compacted_items: list[Any] = []
            changed = False
            for item in value[:DECISION_OBSERVATION_MAX_LIST_ITEMS]:
                compacted_item, item_changed = self._compact_for_decision_prompt(item)
                compacted_items.append(compacted_item)
                changed = changed or item_changed
            if len(value) > DECISION_OBSERVATION_MAX_LIST_ITEMS:
                changed = True
            return compacted_items, changed
        if isinstance(value, dict):
            compacted_dict: dict[str, Any] = {}
            changed = False
            for key, item in value.items():
                compacted_item, item_changed = self._compact_for_decision_prompt(item)
                compacted_dict[key] = compacted_item
                changed = changed or item_changed
            return compacted_dict, changed
        return value, False

    def _observations_within_prompt_budget(
        self,
        observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep recent actionable observations without allowing aggregate prompt growth."""

        kept_reversed: list[dict[str, Any]] = []
        remaining = LLM_OBSERVATION_MAX_TOTAL_CHARS
        omitted = 0
        for observation in reversed(observations):
            compacted, compacted_changed = self._compact_for_decision_prompt(observation)
            serialized = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) > remaining and observation.get("action") == "fork_subtasks":
                # A large child result must not hide the failure and re-plan
                # signal from the next Planner decision. Full data remains in
                # the durable result/event store.
                compacted = self._planner_feedback_summary(observation)
                compacted_changed = True
                serialized = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) <= remaining:
                if compacted_changed and isinstance(compacted, dict):
                    compacted["_prompt_compacted"] = True
                kept_reversed.append(compacted)
                remaining -= len(serialized)
                continue
            omitted += 1
        bounded = list(reversed(kept_reversed))
        if omitted:
            bounded.insert(
                0,
                {
                    "_prompt_compacted": True,
                    "summary": (
                        f"{omitted} earlier tool observations were omitted from this LLM prompt "
                        "to preserve the context budget. Full results remain in the run log."
                    ),
                },
            )
        return bounded

    @staticmethod
    def _planner_feedback_summary(observation: dict[str, Any]) -> dict[str, Any]:
        def brief(value: Any, limit: int = 320) -> str:
            return str(value or "")[:limit]

        summary = {
            key: observation.get(key)
            for key in (
                "action", "status", "execution_status", "operation_id", "plan_id",
                "failed_step_ids", "replan_required", "waiting_child_run_ids",
            )
            if key in observation
        }
        results = observation.get("task_results")
        if isinstance(results, list):
            summary["task_results"] = []
            for result in results[:20]:
                if not isinstance(result, dict):
                    continue
                projected = {
                    key: result.get(key)
                    for key in ("step_id", "child_run_id", "status", "attempt")
                    if key in result
                }
                projected["summary"] = brief(result.get("summary"))
                failure = result.get("failure")
                if isinstance(failure, dict):
                    projected["failure"] = {
                        key: failure.get(key)
                        for key in ("category", "code", "retryable")
                        if key in failure
                    }
                    projected["failure"]["message"] = brief(failure.get("message"))
                    projected["failure"]["recommended_actions"] = [
                        brief(item, 160) for item in failure.get("recommended_actions", [])[:4]
                    ] if isinstance(failure.get("recommended_actions"), list) else []
                for key in ("missing_requirements", "warnings"):
                    values = result.get(key)
                    if isinstance(values, list):
                        projected[key] = [brief(item, 160) for item in values[:6]]
                summary["task_results"].append(projected)
            summary["omitted_task_result_count"] = max(0, len(results) - 20)
        aggregate = observation.get("aggregate")
        if isinstance(aggregate, dict):
            summary["aggregate"] = {
                key: aggregate.get(key)
                for key in (
                    "status", "completed_step_ids", "partial_step_ids", "blocked_step_ids",
                    "failed_step_ids", "missing_step_ids",
                )
                if key in aggregate
            }
            conflicts = aggregate.get("conflicts")
            if isinstance(conflicts, list):
                summary["aggregate"]["conflicts"] = [
                    {"summary": brief(item.get("summary")), "references": item.get("references", [])[:4]}
                    for item in conflicts[:6] if isinstance(item, dict)
                ]
        verification = observation.get("verification")
        if isinstance(verification, dict):
            summary["verification"] = {
                "status": verification.get("status"),
                "summary": brief(verification.get("summary")),
                "missing_requirements": [
                    brief(item, 160) for item in verification.get("missing_requirements", [])[:6]
                ] if isinstance(verification.get("missing_requirements"), list) else [],
                "recommended_actions": [
                    brief(item, 160) for item in verification.get("recommended_actions", [])[:6]
                ] if isinstance(verification.get("recommended_actions"), list) else [],
            }
        summary["_prompt_compacted"] = True
        return summary

    @staticmethod
    def _completed_tool_call_summaries(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for observation in observations:
            tool_name = observation.get("tool_name")
            tool_input = observation.get("input")
            result = observation.get("result")
            if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
                continue
            if not isinstance(result, dict) or result.get("status") != "completed":
                continue
            summaries.append(
                {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "evidence_available": True,
                    "replay_policy": "do_not_repeat_same_input_in_current_turn",
                }
            )
        return summaries

    @staticmethod
    def _context_window_for_llm(context_window: dict[str, Any]) -> dict[str, Any]:
        """Cached tool evidence is supplied separately as budgeted observations."""

        sanitized = dict(context_window)
        sanitized.pop("cached_tool_observations", None)
        return sanitized

    def _execute_tool(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        tool_events: list[AgentTurnToolEvent],
        progress_events: list[AgentTurnProgressEvent] | None = None,
        llm_events: list[AgentTurnLLMEvent] | None = None,
        step_index: int = 0,
    ) -> ToolResult:
        selected_at = _now_iso()
        run_id = _turn_run_id.get() or context.trace_id or "standalone"
        invocation_id = _stable_id(
            "tool_invocation",
            run_id,
            str(step_index),
            tool_name,
            self._tool_call_fingerprint(tool_name=tool_name, tool_input=tool_input),
        )
        lifecycle_graph = AgentToolLifecycleGraph(
            tool_executor=self.tool_executor,
            review_tool_call=lambda invocation_id, name, input_value, tool_context: (
                self._review_tool_call_before_execute(
                    invocation_id=invocation_id,
                    tool_name=name,
                    tool_input=input_value,
                    context=tool_context,
                    progress_events=progress_events,
                    llm_events=llm_events,
                )
            ),
            append_progress=lambda event_type, status, message, metadata: (
                self._append_progress(
                    progress_events,
                    type=event_type,
                    stage="tool_execute",
                    tool_name=tool_name,
                    status=status,
                    message=message,
                    metadata=metadata,
                )
                if progress_events is not None
                else None
            ),
            raise_if_cancel_requested=self._raise_if_cancel_requested,
            artifact_store=self.tool_invocation_store,
        )
        result = lifecycle_graph.run(
            invocation_id=invocation_id,
            run_id=_turn_run_id.get(),
            tool_name=tool_name,
            tool_input=tool_input,
            context=context,
        )
        tool_events.append(
            AgentTurnToolEvent(
                tool_name=tool_name,
                selected_at=selected_at,
                completed_at=_now_iso(),
                input=tool_input,
                result=result.model_dump(mode="json"),
                feedback=self._local_tool_feedback(tool_name=tool_name, result=result),
            )
        )
        return result

    def prepare_graph_safety_review(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        progress_events: list[AgentTurnProgressEvent],
        llm_events: list[AgentTurnLLMEvent],
    ) -> tuple[ToolResult | None, SafetyReviewRecord | None]:
        """Create or resolve one review without blocking a LangGraph worker.

        Legacy turns retain their Condition-based waiting behaviour.  Graph turns
        call this method at a checkpointed safety node and use an interrupt for a
        pending manual decision instead.
        """
        tool = self.tool_executor.registry.get_tool_or_none(tool_name)
        if tool is None:
            return None, None
        read_only = effective_tool_read_only(tool, tool_input)
        if read_only is True:
            return None, None
        review_mode = self._effective_safety_review_mode()
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return (
                ToolResult(
                    invocation_id=invocation_id,
                    tool_name=tool_name,
                    status="rejected",
                    error="Safety review is required but no Agent run is active.",
                ),
                None,
            )
        review_id = stable_safety_review_id(run_id, invocation_id, tool_name)
        review = run_manager.get_safety_review(review_id)
        if review is None:
            review = run_manager.create_safety_review(
                SafetyReviewRequest(
                    review_id=review_id,
                    run_id=run_id,
                    session_id=context.session_id,
                    trace_id=context.trace_id or "",
                    invocation_id=invocation_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_risk=tool.spec.risk,
                    side_effects=tool.spec.side_effects,
                    read_only=read_only,
                    mode=review_mode,
                    reason=self._safety_review_reason(read_only=read_only),
                    created_at=_now_iso(),
                )
            )
            self._append_safety_review_progress(
                progress_events=progress_events,
                review=review,
                status="required",
                message=f"Safety review required for `{tool_name}`.",
            )
            self._append_run_event(
                type="safety_review_required",
                message=f"Safety review required for `{tool_name}`.",
                stage="safety_review",
                payload={"review": review.model_dump(mode="json")},
            )
        if review.status == SafetyReviewStatus.PENDING:
            if review_mode == SafetyReviewMode.SKIP:
                review = run_manager.decide_safety_review(
                    review_id=review.review_id,
                    decision=SafetyReviewDecision.APPROVE,
                    decided_by="system.skip",
                    reason="Safety review mode is skip; review recorded and automatically approved.",
                )
            elif review_mode == SafetyReviewMode.LLM:
                review = self._decide_safety_review_with_llm(review, llm_events=llm_events)
            else:
                return None, review
        self._append_safety_review_progress(
            progress_events=progress_events,
            review=review,
            status=review.status.value,
            message=f"Safety review {review.status.value} for `{tool_name}`.",
        )
        if review.status == SafetyReviewStatus.APPROVED:
            return None, review
        return (
            self._safety_rejected_tool_result(
                invocation_id=invocation_id,
                tool_name=tool_name,
                review=review,
            ),
            None,
        )

    def execute_graph_tool(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        approved_review: SafetyReviewRecord | None,
        tool_events: list[AgentTurnToolEvent],
        progress_events: list[AgentTurnProgressEvent],
    ) -> ToolResult:
        """Use the project tool lifecycle graph after graph-level safety approval."""
        selected_at = _now_iso()
        lifecycle_graph = AgentToolLifecycleGraph(
            tool_executor=self.tool_executor,
            review_tool_call=lambda *_args: (None, approved_review),
            append_progress=lambda event_type, status, message, metadata: self._append_progress(
                progress_events,
                type=event_type,
                stage="tool_execute",
                tool_name=tool_name,
                status=status,
                message=message,
                metadata=metadata,
            ),
            raise_if_cancel_requested=self._raise_if_cancel_requested,
            artifact_store=self.tool_invocation_store,
        )
        result = lifecycle_graph.run(
            invocation_id=invocation_id,
            run_id=_turn_run_id.get(),
            tool_name=tool_name,
            tool_input=tool_input,
            context=context,
        )
        tool_events.append(
            AgentTurnToolEvent(
                tool_name=tool_name,
                selected_at=selected_at,
                completed_at=_now_iso(),
                input=tool_input,
                result=result.model_dump(mode="json"),
                feedback=self._local_tool_feedback(tool_name=tool_name, result=result),
            )
        )
        return result

    def _review_tool_call_before_execute(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        progress_events: list[AgentTurnProgressEvent] | None,
        llm_events: list[AgentTurnLLMEvent] | None,
    ) -> tuple[ToolResult | None, SafetyReviewRecord | None]:
        tool = self.tool_executor.registry.get_tool_or_none(tool_name)
        if tool is None:
            return None, None
        read_only = effective_tool_read_only(tool, tool_input)
        if read_only is True:
            return None, None
        review_mode = self._effective_safety_review_mode()
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return (
                ToolResult(
                    invocation_id=invocation_id,
                    tool_name=tool_name,
                    status="rejected",
                    error="Safety review is required but no Agent run is active.",
                    output={
                        "safety_review": {
                            "status": "rejected",
                            "reason": "No active Agent run can host the safety review.",
                        }
                    },
                ),
                None,
            )
        reason = self._safety_review_reason(read_only=read_only)
        review = run_manager.create_safety_review(
            SafetyReviewRequest(
                review_id=stable_safety_review_id(run_id, invocation_id, tool_name),
                run_id=run_id,
                session_id=context.session_id,
                trace_id=context.trace_id or "",
                invocation_id=invocation_id,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_risk=tool.spec.risk,
                side_effects=tool.spec.side_effects,
                read_only=read_only,
                mode=review_mode,
                reason=reason,
                created_at=_now_iso(),
            )
        )
        self._append_safety_review_progress(
            progress_events=progress_events,
            review=review,
            status="required",
            message=f"Safety review required for `{tool_name}`.",
        )
        self._append_run_event(
            type="safety_review_required",
            message=f"Safety review required for `{tool_name}`.",
            stage="safety_review",
            payload={"review": review.model_dump(mode="json")},
        )
        if review_mode == SafetyReviewMode.SKIP:
            decided = run_manager.decide_safety_review(
                review_id=review.review_id,
                decision=SafetyReviewDecision.APPROVE,
                decided_by="system.skip",
                reason="Safety review mode is skip; review recorded and automatically approved.",
            )
            self._append_safety_review_progress(
                progress_events=progress_events,
                review=decided,
                status="approved",
                message=f"Safety review skipped for `{tool_name}`.",
            )
            return None, decided
        if review_mode == SafetyReviewMode.LLM:
            decided = self._decide_safety_review_with_llm(
                review,
                llm_events=llm_events,
            )
            self._append_safety_review_progress(
                progress_events=progress_events,
                review=decided,
                status=decided.status.value,
                message=f"Safety review {decided.status.value} for `{tool_name}`.",
            )
            if decided.status == SafetyReviewStatus.APPROVED:
                return None, decided
            return (
                self._safety_rejected_tool_result(
                    invocation_id=invocation_id,
                    tool_name=tool_name,
                    review=decided,
                ),
                None,
            )

        decided = run_manager.safety_reviews.wait_for_decision(
            review.review_id,
            timeout_seconds=self.safety_manual_wait_poll_seconds,
            cancel_check=lambda: run_manager.is_cancel_requested(run_id),
        )
        self._raise_if_cancel_requested()
        self._append_safety_review_progress(
            progress_events=progress_events,
            review=decided,
            status=decided.status.value,
            message=f"Safety review {decided.status.value} for `{tool_name}`.",
        )
        if decided.status == SafetyReviewStatus.APPROVED:
            return None, decided
        return (
            self._safety_rejected_tool_result(
                invocation_id=invocation_id,
                tool_name=tool_name,
                review=decided,
            ),
            None,
        )

    def _safety_review_reason(self, *, read_only: bool | None) -> str:
        if read_only is None:
            return "Tool did not explicitly declare read_only=true."
        return "Tool is explicitly non-read-only."

    def _append_safety_review_progress(
        self,
        *,
        progress_events: list[AgentTurnProgressEvent] | None,
        review: SafetyReviewRecord,
        status: str,
        message: str,
    ) -> None:
        if progress_events is None:
            return
        self._append_progress(
            progress_events,
            type="safety_review",
            stage="safety_review",
            tool_name=review.tool_name,
            status=status,
            message=message,
            metadata={"review": review.model_dump(mode="json")},
        )

    def _safety_rejected_tool_result(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        review: SafetyReviewRecord,
    ) -> ToolResult:
        return ToolResult(
            invocation_id=invocation_id,
            tool_name=tool_name,
            status="rejected",
            output={"safety_review": review.model_dump(mode="json")},
            error=review.decision_reason or "Safety review rejected tool call.",
        )

    def _decide_safety_review_with_llm(
        self,
        review: SafetyReviewRecord,
        *,
        llm_events: list[AgentTurnLLMEvent] | None,
    ) -> SafetyReviewRecord:
        run_manager = _turn_run_manager.get()
        if run_manager is None:
            return review
        if self.llm_client is None:
            return run_manager.decide_safety_review(
                review_id=review.review_id,
                decision=SafetyReviewDecision.REJECT,
                decided_by="system.llm_unavailable",
                reason="Safety review mode is llm but no LLM client is available.",
            )
        system_prompt = (
            "You are a safety reviewer for local agent tool calls. Decide whether this "
            "single non-read-only operation may proceed. Return only strict JSON: "
            '{"approve":true|false,"reason":"..."}. Approve only when the '
            "operation is clearly requested by the user, scoped, and consistent with the "
            "tool metadata. Reject ambiguous, destructive, broad, or unsupported operations."
        )
        user_prompt = json.dumps(
            {"review": review.model_dump(mode="json")},
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="safety_review",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"safety_review tool={review.tool_name}",
            max_output_tokens=512,
            llm_events=llm_events if llm_events is not None else [],
        )
        parsed = self._parse_json_object(response.content) if response is not None else None
        approve = bool(parsed.get("approve")) if isinstance(parsed, dict) else False
        reason = (
            str(parsed.get("reason"))
            if isinstance(parsed, dict) and parsed.get("reason")
            else "LLM safety review did not return an approval."
        )
        decided = run_manager.decide_safety_review(
            review_id=review.review_id,
            decision=SafetyReviewDecision.APPROVE if approve else SafetyReviewDecision.REJECT,
            decided_by="llm",
            reason=reason,
        )
        return run_manager.attach_safety_review_llm_output(
            review_id=decided.review_id,
            llm_output=response.content if response is not None else None,
        )

    def _append_progress(
        self,
        progress_events: list[AgentTurnProgressEvent],
        *,
        type: str,
        message: str,
        stage: str | None = None,
        tool_name: str | None = None,
        package_name: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        progress_events.append(
            AgentTurnProgressEvent(
                event_index=len(progress_events) + 1,
                created_at=_now_iso(),
                type=type,
                message=message,
                stage=stage,
                tool_name=tool_name,
                package_name=package_name,
                status=status,
                metadata=metadata or {},
            )
        )
        self._append_run_event(
            type=type,
            message=message,
            stage=stage,
            payload={
                "tool_name": tool_name,
                "package_name": package_name,
                "status": status,
                "metadata": metadata or {},
            },
        )

    def _tool_progress_message(self, *, tool_name: str, result: ToolResult) -> str:
        if result.status != "completed":
            return f"`{tool_name}` failed: {result.error or 'unknown error'}"
        output = result.output
        count_summary = self._generic_output_count_summary(output)
        if count_summary:
            return f"`{tool_name}` completed with {count_summary}."
        return f"`{tool_name}` completed."

    def _generic_output_count_summary(self, output: dict[str, Any]) -> str:
        for key, value in output.items():
            if isinstance(value, list):
                return f"{len(value)} `{key}` items"
            if isinstance(value, int) and key.endswith("_count"):
                return f"{value} `{key}`"
        return ""

    def _short_text(self, value: str, *, limit: int = 500) -> str:
        compact = " ".join(value.strip().split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    def _verify_final_answer(
        self,
        *,
        answer: str,
        tool_events: list[AgentTurnToolEvent],
    ) -> list[AgentTurnVerificationWarning]:
        successful_tools = {
            event.tool_name for event in tool_events if event.result.get("status") == "completed"
        }
        warnings: list[AgentTurnVerificationWarning] = []
        _ = (answer, successful_tools)
        return warnings

    def _local_tool_feedback(
        self,
        *,
        tool_name: str,
        result: ToolResult,
    ) -> dict[str, Any]:
        domain_summary = self._tool_domain_summary(tool_name=tool_name, result=result)
        validation_errors = self.tool_executor.validate_output(
            tool_name=tool_name,
            result=result,
        )
        if result.status == "completed" and not validation_errors:
            feedback = {
                "source": "local",
                "status": "accepted",
                "message": f"{tool_name} executed successfully.",
                "protocol_status": "valid",
            }
            if domain_summary:
                feedback["domain_summary"] = domain_summary
            return feedback
        feedback = {
            "source": "local",
            "status": "failed",
            "message": f"{tool_name} result failed local protocol validation.",
            "error": result.error,
            "protocol_status": "invalid",
            "validation_errors": validation_errors,
        }
        if domain_summary:
            feedback["domain_summary"] = domain_summary
        return feedback

    def _tool_domain_summary(
        self,
        *,
        tool_name: str,
        result: ToolResult,
    ) -> dict[str, Any]:
        _ = (tool_name, result)
        return {}

    def _check_tool_result(
        self,
        *,
        user_input: str,
        tool_package: str | None,
        decision: dict[str, Any],
        tool_result: ToolResult,
        llm_events: list[AgentTurnLLMEvent],
    ) -> dict[str, Any]:
        local_feedback = self._local_tool_feedback(
            tool_name=tool_result.tool_name,
            result=tool_result,
        )
        if local_feedback.get("status") == "accepted" or self.llm_client is None:
            return local_feedback

        system_prompt = (
            "You are the Tool Result Checker for Local Knowledge Agent OS. Check whether "
            "the just-executed tool result is a valid observation for the prior tool-call "
            "decision after local protocol validation failed or the tool did not complete. "
            "Do not make a final user answer. Do not claim success when ToolResult.status "
            "is failed or when tool_feedback.protocol_status is invalid. Distinguish tool "
            "execution status from domain record status: ToolResult.status=completed means "
            "the tool ran, not that a business object is done. If tool_feedback.domain_summary "
            "is present, use it as the authoritative structured summary for business records. "
            'Return only strict JSON: {"status":'
            '"accepted|needs_retry|failed","message":"...","remaining_work":"..."}.'
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "tool_package": tool_package,
                "decision": decision,
                "tool_result": tool_result.model_dump(mode="json"),
                "tool_feedback": local_feedback,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="tool_result_check",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"tool_result_check tool={tool_result.tool_name}",
            max_output_tokens=self.llm_generation_token_budget,
            llm_events=llm_events,
        )
        if response is None:
            return local_feedback

        parsed = self._parse_json_object(response.content)
        if not isinstance(parsed, dict) or not parsed:
            checked = dict(local_feedback)
            checked.update(
                {
                    "source": "local",
                    "llm_check_status": "unparsed",
                    "llm_output": response.content,
                }
            )
            return checked

        status = parsed.get("status")
        if status not in {"accepted", "needs_retry", "failed"}:
            status = local_feedback["status"]
        if tool_result.status != "completed" and status == "accepted":
            status = "failed"
        message = parsed.get("message") if isinstance(parsed.get("message"), str) else None
        remaining_work = (
            parsed.get("remaining_work") if isinstance(parsed.get("remaining_work"), str) else None
        )
        feedback = {
            "source": "llm",
            "status": status,
            "message": message or local_feedback["message"],
            "remaining_work": remaining_work,
            "local_status": local_feedback["status"],
            "llm_output": response.content,
        }
        if local_feedback.get("domain_summary"):
            feedback["domain_summary"] = local_feedback["domain_summary"]
        return feedback

    def _answer_with_llm(
        self,
        *,
        user_input: str,
        route: dict[str, Any],
        context_window: dict[str, Any],
        observations: list[dict[str, Any]],
        final_decision: dict[str, Any] | None,
        llm_events: list[AgentTurnLLMEvent],
    ) -> str | None:
        if self._multi_agent_replan_pending():
            return self._unresolved_multi_agent_answer()
        if self.llm_client is None:
            return None
        answer_decision = self._decision_context_for_answer_stage(final_decision)
        system_prompt = (
            "You are the Final Answer Writer for Local Knowledge Agent OS. Use the provided "
            "session context window, tool observations, and decision reason to answer the "
            "user directly in Chinese. The decision stage is only a structured control step; "
            "do not treat any decision-stage final_answer text as authoritative final prose. "
            "Base the answer on evidence from observations and session context. Mention "
            "uncertainty when evidence is incomplete. Do not wrap the answer in JSON."
        )
        prompt_observations = self._observations_within_prompt_budget(observations)
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "route_context": self._route_context(route),
                "session_context_window": self._context_window_for_llm(context_window),
                "observations": prompt_observations,
                "answer_stage_decision": answer_decision,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="answer",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"agent_turn_answer observations={len(prompt_observations)}",
            max_output_tokens=None,
            llm_events=llm_events,
        )
        if response is None:
            return None
        return response.content

    def _decision_context_for_answer_stage(
        self,
        decision: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(decision, dict):
            return {}
        operation = decision.get("operation") if isinstance(decision.get("operation"), dict) else {}
        operation_for_answer = dict(operation)
        operation_for_answer.pop("final_answer", None)
        return {
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "assistant_message": decision.get("assistant_message"),
            "operation": operation_for_answer,
        }

    def _answer_from_context_with_llm(
        self,
        *,
        user_input: str,
        route: dict[str, Any],
        context_window: dict[str, Any],
        llm_events: list[AgentTurnLLMEvent],
    ) -> str | None:
        if self.llm_client is None:
            return None
        system_prompt = (
            "You are the Main Agent Brain for Local Knowledge Agent OS. Answer the "
            "current user turn directly in Chinese using only the provided session "
            "context window when it is sufficient. Do not invent unavailable local "
            "facts. If the context is insufficient and no tool package was selected, "
            "explain what information is missing. Do not wrap the answer in JSON."
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "route_context": self._route_context(route),
                "session_context_window": self._context_window_for_llm(context_window),
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="context_answer",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"agent_turn_context_answer user_input={user_input[:80]}",
            max_output_tokens=self.llm_generation_token_budget,
            llm_events=llm_events,
        )
        if response is None:
            return None
        return response.content.strip() or None

    def _should_stream_llm_call(self) -> bool:
        return (
            _turn_llm_response_mode.get() == LLMResponseMode.STREAM
            and self.llm_client is not None
            and hasattr(self.llm_client, "stream")
        )

    def _llm_content_role_for_stage(self, stage: str) -> str:
        return {
            "route": "route_decision",
            "decision": "agent_decision",
            "decision_repair": "decision_repair",
            "tool_result_check": "tool_result_check",
            "context_summarize": "context_summary",
            "context_answer": "context_answer",
            "answer": "final_answer",
        }.get(stage, "llm_output")

    def _llm_display_target_for_stage(self, stage: str) -> str:
        if stage in {"answer", "context_answer"}:
            return "assistant_answer"
        return "agent_process"

    def _llm_stage_requires_json(self, stage: str) -> bool:
        return stage not in {"answer", "context_answer"}

    def _stream_text_once(
        self,
        *,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        max_output_tokens: int | None,
        llm_call_id: str,
        tools: list[LLMToolDefinition] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        if self.llm_client is None:
            raise LLMClientError("No LLM client is configured.")
        content_role = self._llm_content_role_for_stage(stage)
        display_target = self._llm_display_target_for_stage(stage)

        async def collect() -> tuple[str, dict[str, Any]]:
            client_name, model, reasoning_effort, selection_source, profile_id = (
                self._inference_selection()
            )
            metadata = {
                "stage": stage,
                "inference_selection_source": selection_source,
            }
            if profile_id is not None:
                metadata["inference_profile_id"] = profile_id
            request = LLMRequest(
                client_name=client_name,
                model=model,
                response_mode=LLMResponseMode.STREAM,
                messages=[
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=user_prompt),
                ],
                prompt_summary=prompt_summary,
                temperature=0.0,
                reasoning_effort=reasoning_effort,
                max_output_tokens=max_output_tokens,
                require_json=self._llm_stage_requires_json(stage),
                tools=tools or [],
                tool_choice=tool_choice,
                metadata=metadata,
            )
            snapshot = ""
            stream_metadata: dict[str, Any] = {}
            async for event in self.llm_client.stream(request):  # type: ignore[union-attr]
                stream_metadata.update(
                    {
                        "client_name": event.client_name,
                        "provider": event.provider,
                        "model": event.model,
                    }
                )
                if event.metadata:
                    stream_metadata.update(event.metadata)
                if event.event_type == "llm_failed":
                    error_event = (
                        event.metadata.get("error_event")
                        if isinstance(event.metadata.get("error_event"), dict)
                        else None
                    )
                    if error_event is not None:
                        headers = (
                            event.metadata.get("headers")
                            if isinstance(event.metadata.get("headers"), dict)
                            else {}
                        )
                        provider_request_id = event.metadata.get("provider_request_id")
                        raise LLMProviderStreamError(
                            message=event.error or "LLM provider stream failed.",
                            error_event=error_event,
                            partial_content=event.content_snapshot
                            or str(event.metadata.get("partial_content") or ""),
                            headers={str(key): str(value) for key, value in headers.items()},
                            provider_request_id=provider_request_id
                            if isinstance(provider_request_id, str)
                            else None,
                        )
                    raise LLMClientError(event.error or "LLM stream failed.")
                if event.event_type == "llm_completed":
                    if event.content_snapshot:
                        snapshot = event.content_snapshot
                    continue
                if event.event_type != "llm_delta" or not event.delta:
                    continue
                snapshot = event.content_snapshot or snapshot + event.delta
                self._append_run_event(
                    type="llm_delta",
                    message=event.delta,
                    stage=stage,
                    payload={
                        "stream_part": "llm_delta",
                        "content_role": content_role,
                        "display_target": display_target,
                        "llm_call_id": llm_call_id,
                        "client_name": event.client_name,
                        "provider": event.provider,
                        "model": event.model,
                        "provider_request_id": stream_metadata.get("provider_request_id"),
                        "delta": event.delta,
                        "content_snapshot": snapshot,
                    },
                )
            return snapshot, stream_metadata

        content, stream_metadata = asyncio.run(collect())
        return LLMResponse(
            provider=stream_metadata.get("provider") or type(self.llm_client).__name__,
            client_name=stream_metadata.get("client_name")
            or _turn_llm_client_name.get()
            or "default",
            model=stream_metadata.get("model") or _turn_llm_model.get() or "default",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
            response_mode=LLMResponseMode.STREAM,
            finish_reason=stream_metadata.get("finish_reason") or "stream_completed",
            provider_request_id=stream_metadata.get("provider_request_id"),
            usage=stream_metadata.get("usage") or {},
            metadata={"streamed": True, "content_role": content_role},
            tool_calls=self._tool_calls_from_stream_metadata(stream_metadata),
        )

    @staticmethod
    def _tool_calls_from_stream_metadata(metadata: dict[str, Any]) -> list[LLMToolCall]:
        raw_calls = metadata.get("tool_calls")
        if not isinstance(raw_calls, list):
            return []
        try:
            return [LLMToolCall.model_validate(call) for call in raw_calls]
        except (TypeError, ValueError):
            return []

    def _summarize_context_window(
        self,
        *,
        summary: str,
        messages_to_summarize: list[SessionRecentMessage],
        retained_recent_messages: list[SessionRecentMessage],
        token_budget: int,
        llm_events: list[AgentTurnLLMEvent],
    ) -> str | None:
        if self.llm_client is None:
            return None
        system_prompt = (
            "You are the Session Context Compressor for Local Knowledge Agent OS. "
            "Summarize older conversation history for future turns. Preserve user goals, "
            "preferences, constraints, unresolved tasks, important facts, and references to "
            "trace ids when useful. Do not include tool execution logs, raw prompts, or verbose "
            'transcripts. Return only strict JSON: {"summary":"..."}'
        )
        user_prompt = json.dumps(
            {
                "existing_summary": summary,
                "messages_to_summarize": [
                    message.model_dump(mode="json") for message in messages_to_summarize
                ],
                "retained_recent_messages": [
                    message.model_dump(mode="json") for message in retained_recent_messages
                ],
                "token_budget": token_budget,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="context_summarize",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"context_summarize messages={len(messages_to_summarize)}",
            max_output_tokens=self.llm_generation_token_budget,
            llm_events=llm_events,
        )
        if response is None:
            return None
        parsed = self._parse_json_object(response.content)
        if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str):
            return parsed["summary"].strip() or None
        return response.content.strip() or None

    def _complete_text_with_retry(
        self,
        *,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        max_output_tokens: int | None,
        llm_events: list[AgentTurnLLMEvent],
        tools: list[LLMToolDefinition] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse | None:
        if self.llm_client is None:
            return None

        provider = type(self.llm_client).__name__
        use_stream = self._should_stream_llm_call()
        response_mode = LLMResponseMode.STREAM if use_stream else _turn_llm_response_mode.get()
        content_role = self._llm_content_role_for_stage(stage)
        for attempt in range(1, self.llm_max_attempts + 1):
            child_output_cap = self._enforce_child_runtime_budget(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if child_output_cap is not None:
                max_output_tokens = (
                    child_output_cap
                    if max_output_tokens is None
                    else min(max_output_tokens, child_output_cap)
                )
            started_at = llm_audit_now_iso()
            perf_start = time.perf_counter()
            llm_call_id = stable_llm_call_id(
                _turn_run_id.get(),
                stage,
                response_mode.value,
                str(attempt),
                started_at,
            )
            self._append_run_event(
                type="llm_started",
                message=(
                    f"LLM stream started for `{stage}`."
                    if use_stream
                    else f"LLM call started for `{stage}`."
                ),
                stage=stage,
                payload={
                    "stream_part": "llm_audit",
                    "content_role": content_role,
                    "llm_call_id": llm_call_id,
                    "provider": provider,
                    "attempt": attempt,
                    "response_mode": response_mode.value,
                },
            )
            try:
                with self._admit_llm(provider):
                    if use_stream:
                        response = self._stream_text_once(
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            prompt_summary=prompt_summary,
                            max_output_tokens=max_output_tokens,
                            stage=stage,
                            llm_call_id=llm_call_id,
                            tools=tools,
                            tool_choice=tool_choice,
                        )
                    else:
                        response = self._complete_text_once(
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            prompt_summary=prompt_summary,
                            max_output_tokens=max_output_tokens,
                            stage=stage,
                            tools=tools,
                            tool_choice=tool_choice,
                        )
            except LLMRateLimitError as exc:
                duration_ms = self._duration_ms(perf_start)
                llm_event = self._llm_error_event(
                    stage=stage,
                    provider=provider,
                    attempt=attempt,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    prompt_summary=prompt_summary,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    llm_call_id=llm_call_id,
                    exc=exc,
                )
                self._append_run_event(
                    type="llm_failed",
                    message=(
                        f"LLM stream rate limited for `{stage}`."
                        if use_stream
                        else f"LLM call rate limited for `{stage}`."
                    ),
                    stage=stage,
                    payload={
                        "stream_part": "llm_audit",
                        "content_role": content_role,
                        "llm_call_id": llm_call_id,
                        "provider": provider,
                        "attempt": attempt,
                        "response_mode": response_mode.value,
                        "status": "rate_limited",
                        "status_code": exc.status_code,
                        "retry_after": exc.retry_after,
                        "error_category": llm_event.error_category,
                        "is_retriable": llm_event.is_retriable,
                        "audit_record": llm_event.audit_record,
                    },
                )
                llm_events.append(llm_event)
                if attempt >= self.llm_max_attempts:
                    return None
                time.sleep(self._retry_after_seconds(exc.retry_after))
                continue
            except LLMClientError as exc:
                duration_ms = self._duration_ms(perf_start)
                llm_event = self._llm_error_event(
                    stage=stage,
                    provider=provider,
                    attempt=attempt,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    prompt_summary=prompt_summary,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    llm_call_id=llm_call_id,
                    exc=exc,
                )
                self._append_run_event(
                    type="llm_failed",
                    message=(
                        f"LLM stream failed for `{stage}`."
                        if use_stream
                        else f"LLM call failed for `{stage}`."
                    ),
                    stage=stage,
                    payload={
                        "stream_part": "llm_audit",
                        "content_role": content_role,
                        "llm_call_id": llm_call_id,
                        "provider": provider,
                        "attempt": attempt,
                        "response_mode": response_mode.value,
                        "status": self._llm_error_status(exc),
                        "error_type": type(exc).__name__,
                        "error_category": llm_event.error_category,
                        "is_retriable": llm_event.is_retriable,
                        "audit_record": llm_event.audit_record,
                    },
                )
                llm_events.append(llm_event)
                return None

            duration_ms = self._duration_ms(perf_start)
            llm_event = self._llm_completed_event(
                stage=stage,
                attempt=attempt,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_summary=prompt_summary,
                started_at=started_at,
                duration_ms=duration_ms,
                llm_call_id=llm_call_id,
                response=response,
            )
            llm_events.append(llm_event)
            self._append_run_event(
                type="llm_completed",
                message=(
                    f"LLM stream completed for `{stage}`."
                    if use_stream
                    else f"LLM call completed for `{stage}`."
                ),
                stage=stage,
                payload={
                    "stream_part": "llm_audit",
                    "content_role": content_role,
                    "llm_call_id": llm_call_id,
                    "provider": response.provider,
                    "attempt": attempt,
                    "response_mode": response.response_mode.value,
                    "status": response.status,
                    "content_length": len(response.content),
                    "budget_token_count": (
                        llm_event.total_token_count
                        if llm_event.total_token_count is not None
                        else max((len(system_prompt) + len(user_prompt) + len(response.content) + 3) // 4, 1)
                    ),
                    "provider_request_id": response.provider_request_id,
                    "finish_reason": response.finish_reason,
                    "audit_record": llm_event.audit_record,
                },
            )
            return response
        return None

    @contextmanager
    def _admit_llm(self, provider: str):
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        run = run_manager.get_run(run_id) if run_manager is not None and run_id else None
        session_id = run.session_id if run is not None else "unscoped"
        provider_key = _turn_llm_client_name.get() or provider
        identities = (("global", "all", 16), ("provider", provider_key, 4), ("session", session_id, 4))
        semaphores: list[threading.BoundedSemaphore] = []
        acquired: list[threading.BoundedSemaphore] = []
        with self._llm_admission_guard:
            for kind, key, limit in identities:
                identity = (kind, key)
                semaphore = self._llm_admission.get(identity)
                if semaphore is None:
                    semaphore = threading.BoundedSemaphore(limit)
                    self._llm_admission[identity] = semaphore
                semaphores.append(semaphore)
        try:
            for semaphore in semaphores:
                while not semaphore.acquire(timeout=0.1):
                    self._raise_if_cancel_requested()
                acquired.append(semaphore)
            self._raise_if_cancel_requested()
            yield
        finally:
            for semaphore in reversed(acquired):
                semaphore.release()

    def _complete_text_once(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        max_output_tokens: int | None,
        stage: str,
        tools: list[LLMToolDefinition] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        if self.llm_client is None:
            raise LLMClientError("No LLM client is configured.")
        client_name, model, reasoning_effort, selection_source, profile_id = (
            self._inference_selection()
        )
        request_metadata = {
            "stage": stage,
            "inference_selection_source": selection_source,
        }
        if profile_id is not None:
            request_metadata["inference_profile_id"] = profile_id
        request_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prompt_summary": prompt_summary,
            "temperature": 0.0,
            "max_output_tokens": max_output_tokens,
            "client_name": client_name,
            "model": model,
            "response_mode": _turn_llm_response_mode.get(),
            "reasoning_effort": reasoning_effort,
            "require_json": self._llm_stage_requires_json(stage),
            "metadata": request_metadata,
        }
        if tools is not None:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = tool_choice
        try:
            result = self.llm_client.complete_text(**request_kwargs)
        except TypeError as exc:
            if (
                selection_source != "default"
                or reasoning_effort is not None
                or profile_id is not None
            ):
                raise LLMClientError(
                    "The configured LLM client cannot honor the selected inference profile."
                ) from exc
            result = self.llm_client.complete_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_summary=prompt_summary,
                temperature=0.0,
                max_output_tokens=max_output_tokens,
            )
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    def _llm_error_event(
        self,
        *,
        stage: str,
        provider: str,
        attempt: int,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        started_at: str,
        duration_ms: int,
        llm_call_id: str,
        exc: LLMClientError,
    ) -> AgentTurnLLMEvent:
        if isinstance(exc, LLMProviderStreamError):
            run_context = self._current_run_context()
            call_record = build_stream_error_call_record(
                llm_call_id=llm_call_id,
                stage=stage,
                started_at=started_at,
                duration_ms=duration_ms,
                client_name=_turn_llm_client_name.get() or "default",
                provider=provider,
                model=_turn_llm_model.get() or "default",
                response_mode=_turn_llm_response_mode.get().value,
                error_event=exc.error_event,
                partial_content=exc.partial_content,
                provider_request_id=exc.provider_request_id,
                headers=exc.headers,
                run_id=run_context.get("run_id"),
                trace_id=run_context.get("trace_id"),
                session_id=run_context.get("session_id"),
            )
            metadata = dict(call_record.metadata)
            metadata.update(prompt_metadata(system_prompt=system_prompt, user_prompt=user_prompt))
            call_record = call_record.model_copy(update={"metadata": metadata})
            return AgentTurnLLMEvent(
                **self._event_fields_from_call_record(call_record),
                stage=stage,
                provider=provider,
                status=call_record.status,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                output=exc.partial_content,
                attempt=attempt,
                error_type=type(exc).__name__,
                error=str(exc),
            )

        status_code = exc.status_code if isinstance(exc, LLMProviderHTTPError) else None
        retry_after = exc.retry_after if isinstance(exc, LLMProviderHTTPError) else None
        provider_error_type = (
            exc.provider_error_type if isinstance(exc, LLMProviderHTTPError) else None
        )
        provider_error_code = (
            exc.provider_error_code if isinstance(exc, LLMProviderHTTPError) else None
        )
        provider_error_param = (
            exc.provider_error_param if isinstance(exc, LLMProviderHTTPError) else None
        )
        if isinstance(exc, LLMProviderHTTPError):
            category, retriable = classify_provider_error(
                http_status=exc.status_code,
                provider_error_type=provider_error_type,
                provider_error_code=provider_error_code,
            )
            category = exc.error_category or category
            retriable = exc.is_retriable if exc.is_retriable is not None else retriable
            headers = exc.headers
        else:
            category, retriable = classify_openai_sdk_exception(exc)
            headers = {}
        call_record = self._llm_call_record(
            llm_call_id=llm_call_id,
            stage=stage,
            client_name=_turn_llm_client_name.get() or "default",
            provider=provider,
            model=_turn_llm_model.get() or "default",
            response_mode=_turn_llm_response_mode.get().value,
            status=self._llm_error_status(exc),
            started_at=started_at,
            failed_at=llm_audit_now_iso(),
            duration_ms=duration_ms,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=prompt_summary,
            http_status=status_code,
            retry_after=retry_after,
            provider_error_type=provider_error_type,
            provider_error_code=provider_error_code,
            provider_error_param=provider_error_param,
            error_category=category,
            error_message=str(exc),
            is_retriable=retriable,
            metadata={"headers": headers},
        )
        return AgentTurnLLMEvent(
            **self._event_fields_from_call_record(call_record),
            stage=stage,
            provider=provider,
            status=self._llm_error_status(exc),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output="",
            attempt=attempt,
            error_type=type(exc).__name__,
            status_code=status_code,
            retry_after=retry_after,
            error=str(exc),
        )

    def _llm_completed_event(
        self,
        *,
        stage: str,
        attempt: int,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        started_at: str,
        duration_ms: int,
        llm_call_id: str,
        response: LLMResponse,
    ) -> AgentTurnLLMEvent:
        input_tokens, output_tokens, total_tokens = usage_token_counts(response.usage)
        call_record = self._llm_call_record(
            llm_call_id=llm_call_id,
            stage=stage,
            client_name=response.client_name or _turn_llm_client_name.get() or "default",
            provider=response.provider,
            model=response.model or _turn_llm_model.get() or "default",
            response_mode=response.response_mode.value,
            status=response.status,
            started_at=started_at,
            completed_at=llm_audit_now_iso(),
            duration_ms=duration_ms,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=prompt_summary,
            provider_request_id=response.provider_request_id,
            finish_reason=response.finish_reason,
            input_token_count=input_tokens,
            output_token_count=output_tokens,
            total_token_count=total_tokens,
            content_length=len(response.content),
            partial=response.partial,
            metadata=response.metadata,
        )
        return AgentTurnLLMEvent(
            **self._event_fields_from_call_record(call_record),
            stage=stage,
            provider=response.provider,
            status=response.status,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output=response.content,
            attempt=attempt,
        )

    def _llm_call_record(
        self,
        *,
        llm_call_id: str,
        stage: str,
        client_name: str,
        provider: str,
        model: str,
        response_mode: str,
        status: str,
        started_at: str,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        completed_at: str | None = None,
        failed_at: str | None = None,
        duration_ms: int | None = None,
        http_status: int | None = None,
        provider_request_id: str | None = None,
        retry_after: str | None = None,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
        provider_error_param: str | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
        is_retriable: bool | None = None,
        finish_reason: str | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
        total_token_count: int | None = None,
        content_length: int | None = None,
        partial: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> LLMCallRecord:
        record_metadata = {}
        record_metadata.update(
            prompt_metadata(system_prompt=system_prompt, user_prompt=user_prompt)
        )
        if metadata:
            record_metadata.update(metadata)
        run_context = self._current_run_context()
        return LLMCallRecord(
            llm_call_id=llm_call_id,
            run_id=run_context.get("run_id"),
            trace_id=run_context.get("trace_id"),
            session_id=run_context.get("session_id"),
            stage=stage,
            client_name=client_name,
            provider=provider,
            model=model,
            response_mode=response_mode,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            failed_at=failed_at,
            duration_ms=duration_ms,
            http_status=http_status,
            provider_request_id=provider_request_id,
            retry_after=retry_after,
            provider_error_type=provider_error_type,
            provider_error_code=provider_error_code,
            provider_error_param=provider_error_param,
            error_category=error_category,
            error_message=error_message,
            is_retriable=is_retriable,
            finish_reason=finish_reason,
            input_token_count=input_token_count,
            output_token_count=output_token_count,
            total_token_count=total_token_count,
            content_length=content_length,
            prompt_summary=prompt_summary,
            partial=partial,
            metadata=record_metadata,
        )

    def _event_fields_from_call_record(
        self,
        record: LLMCallRecord,
    ) -> dict[str, Any]:
        payload = record.model_dump(mode="json")
        return {
            "llm_call_id": record.llm_call_id,
            "run_id": record.run_id,
            "trace_id": record.trace_id,
            "session_id": record.session_id,
            "client_name": record.client_name,
            "model": record.model,
            "response_mode": record.response_mode,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "failed_at": record.failed_at,
            "duration_ms": record.duration_ms,
            "http_status": record.http_status,
            "provider_request_id": record.provider_request_id,
            "provider_error_type": record.provider_error_type,
            "provider_error_code": record.provider_error_code,
            "provider_error_param": record.provider_error_param,
            "error_category": record.error_category,
            "error_message": record.error_message,
            "is_retriable": record.is_retriable,
            "finish_reason": record.finish_reason,
            "input_token_count": record.input_token_count,
            "output_token_count": record.output_token_count,
            "total_token_count": record.total_token_count,
            "content_length": record.content_length,
            "prompt_summary": record.prompt_summary,
            "partial": record.partial,
            "metadata": record.metadata,
            "audit_record": payload,
        }

    def _current_run_context(self) -> dict[str, str | None]:
        run_id = _turn_run_id.get()
        run_manager = _turn_run_manager.get()
        if run_id is None or run_manager is None:
            return {"run_id": run_id, "trace_id": None, "session_id": None}
        run = run_manager.get_run(run_id)
        if run is None:
            return {"run_id": run_id, "trace_id": None, "session_id": None}
        return {
            "run_id": run.run_id,
            "trace_id": run.trace_id,
            "session_id": run.session_id,
        }

    def _duration_ms(self, started_at: float) -> int:
        return max(int((time.perf_counter() - started_at) * 1000), 0)

    def _llm_error_status(self, exc: LLMClientError) -> str:
        if isinstance(exc, LLMRateLimitError):
            return "rate_limited"
        if isinstance(exc, LLMAuthenticationError):
            return "authentication_failed"
        if isinstance(exc, LLMNetworkError):
            return "network_failed"
        if isinstance(exc, LLMTimeoutError):
            return "timeout"
        if isinstance(exc, LLMResponseParseError):
            return "response_parse_failed"
        if isinstance(exc, LLMProviderStreamError):
            return "partial_failed" if exc.partial_content else "provider_stream_failed"
        if isinstance(exc, LLMProviderHTTPError):
            return "http_failed"
        return "failed"

    def _retry_after_seconds(self, retry_after: str | None) -> float:
        if not retry_after:
            return self.default_rate_limit_wait_seconds
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(retry_after)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delta = retry_at - datetime.now(UTC)
            return max(delta.total_seconds(), 0.0)
        except (TypeError, ValueError):
            return self.default_rate_limit_wait_seconds

    def _append_run_event(
        self,
        *,
        type: str,
        message: str,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return
        run_manager.append_event(
            run_id,
            type,
            message,
            stage=stage,
            payload=payload or {},
        )

    def _complete_current_run(self, result: AgentTurnResult) -> None:
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return
        if not run_manager.claim_completion(run_id):
            self._raise_if_cancel_requested()
            raise RuntimeError(f"Agent run cannot claim completion: {run_id}")
        try:
            run_manager.flush_events()
        except Exception as exc:
            run_manager.fail_completion_claim(
                run_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        current_run = run_manager.get_run(run_id)
        raw_plan = current_run.metadata.get("multi_agent_plan") if current_run else None
        if isinstance(raw_plan, dict) and raw_plan.get("status") == PlanStatus.FAILED.value:
            reason = "Multi-Agent plan ended with failed or blocked child steps."
            run_manager.append_event(
                run_id,
                "run_failed",
                reason,
                stage="run",
                payload={"error_type": "MultiAgentPlanFailed"},
            )
            run_manager.fail_completion_claim(
                run_id,
                error_type="MultiAgentPlanFailed",
                error=reason,
                log_path=result.log_path,
            )
            return
        run_manager.append_event(
            run_id,
            "run_completed",
            "Agent run completed.",
            stage="run",
            payload={
                "answer_length": len(result.answer),
                "selected_package": result.selected_package,
                "initial_package": result.initial_package,
                "expanded_packages": result.expanded_packages,
                "used_packages": result.used_packages,
                "active_package": result.active_package,
                "tool_event_count": len(result.tool_events),
                "llm_event_count": len(result.llm_events),
                "log_path": result.log_path,
            },
        )
        result_snapshot = {
                "session_id": result.session_id,
                "trace_id": result.trace_id,
                "answer": result.answer,
                "selected_package": result.selected_package,
                "initial_package": result.initial_package,
                "expanded_packages": result.expanded_packages,
                "used_packages": result.used_packages,
                "active_package": result.active_package,
            }
        current_run = run_manager.get_run(run_id)
        if current_run is not None and current_run.parent_run_id is not None:
            run_manager.complete_child_run(
                run_id,
                result_snapshot=result_snapshot,
                log_path=result.log_path,
            )
        else:
            run_manager.complete_run(
                run_id,
                result_snapshot=result_snapshot,
                log_path=result.log_path,
            )

    def _mark_current_run_failed(self, error_type: str, error: str) -> None:
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return
        run_manager.append_event(
            run_id,
            "run_failed",
            error or "Agent run failed.",
            stage="run",
            payload={"error_type": error_type},
        )
        current_run = run_manager.get_run(run_id)
        if current_run is not None and current_run.parent_run_id is not None:
            run_manager.fail_child_run(run_id, error_type=error_type, error=error)
        else:
            run_manager.fail_run(run_id, error_type=error_type, error=error)

    def _mark_current_run_cancelled(self, reason: str) -> None:
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return
        run_manager.cancel_run(run_id, reason=reason)

    def _raise_if_cancel_requested(self) -> None:
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return
        if run_manager.is_cancel_requested(run_id):
            reason = run_manager.cancel_reason(run_id) or "Run cancelled."
            raise AgentRunCancelled(reason)

    def _enforce_child_runtime_budget(
        self, *, system_prompt: str, user_prompt: str
    ) -> int | None:
        """Check durable child counters immediately before each provider request."""
        manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if manager is None or run_id is None:
            return None
        run = manager.get_run(run_id)
        if run is None or run.parent_run_id is None:
            return None
        self._raise_if_cancel_requested()
        snapshot = run.metadata.get("context_snapshot", {})
        budget = snapshot.get("budget", {}) if isinstance(snapshot, dict) else {}
        if not isinstance(budget, dict):
            return None
        expires_at = snapshot.get("expires_at") if isinstance(snapshot, dict) else None
        if expires_at:
            try:
                expiry = datetime.fromisoformat(str(expires_at))
            except ValueError:
                expiry = None
            if expiry is not None and expiry <= datetime.now(UTC):
                manager.timeout_child_run(run_id, error="Child Agent exceeded its wall-time budget.")
                raise AgentRunCancelled("Child Agent exceeded its wall-time budget.")
        events = manager.list_events(run_id)
        max_calls = budget.get("max_llm_calls")
        if max_calls is not None and sum(event.type == "llm_started" for event in events) >= int(max_calls):
            raise RuntimeError("Child Agent exceeded its LLM-call budget.")
        max_tokens = budget.get("max_tokens")
        remaining_tokens: int | None = None
        if max_tokens is not None:
            consumed = 0
            for event in events:
                if event.type != "llm_completed":
                    continue
                count = event.payload.get("budget_token_count")
                audit = event.payload.get("audit_record", {})
                if count is None and isinstance(audit, dict):
                    count = audit.get("total_token_count") or audit.get("content_length")
                consumed += int(count or 0)
            prompt_estimate = max((len(system_prompt) + len(user_prompt) + 3) // 4, 1)
            remaining_tokens = int(max_tokens) - consumed - prompt_estimate
            if remaining_tokens <= 0:
                raise RuntimeError("Child Agent exceeded its token budget.")
        return remaining_tokens

    def _write_log(self, *, result: AgentTurnResult, user_input: str) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{result.trace_id}.md"
        sections = [
            "# Agent Turn Log",
            "",
            f"- generated_at: `{_now_iso()}`",
            f"- session_id: `{result.session_id}`",
            f"- run_id: `{result.run_id}`",
            f"- trace_id: `{result.trace_id}`",
            f"- initial_package: `{result.initial_package}`",
            f"- selected_package: `{result.selected_package}`",
            f"- expanded_packages: `{', '.join(result.expanded_packages) or 'none'}`",
            f"- used_packages: `{', '.join(result.used_packages) or 'none'}`",
            f"- active_package: `{result.active_package}`",
            "",
            "## User Input",
            "",
            self._text_block(user_input),
            "",
            "## Package Catalog",
            "",
            self._json_block(result.package_catalog),
            "",
            "## Session Context Window",
            "",
            self._json_block(result.session_context_window),
            "",
            "## Expanded Tools",
            "",
            self._json_block(result.expanded_tools),
            "",
            "## Decision Events",
            "",
            self._json_block([event.model_dump(mode="json") for event in result.decision_events]),
            "",
            "## Tool Events",
            "",
            self._json_block([event.model_dump(mode="json") for event in result.tool_events]),
            "",
            "## Progress Events",
            "",
            self._json_block([event.model_dump(mode="json") for event in result.progress_events]),
            "",
            "## Verification Warnings",
            "",
            self._json_block(
                [warning.model_dump(mode="json") for warning in result.verification_warnings]
            ),
            "",
            "## LLM Events",
            "",
            self._json_block([event.model_dump(mode="json") for event in result.llm_events]),
            "",
            "## Answer",
            "",
            self._text_block(result.answer),
            "",
        ]
        path.write_text("\n".join(sections), encoding="utf-8")
        return path

    def _parse_json_object(self, content: str) -> Any:
        clean_content = content.strip()
        if clean_content.startswith("```"):
            clean_content = clean_content.strip("`").strip()
            if clean_content.startswith("json"):
                clean_content = clean_content[4:].strip()
        try:
            return json.loads(clean_content)
        except json.JSONDecodeError:
            start = clean_content.find("{")
            end = clean_content.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return {}
            try:
                return json.loads(clean_content[start : end + 1])
            except json.JSONDecodeError:
                return {}

    def _json_block(self, value: Any) -> str:
        return (
            "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"
        )

    def _text_block(self, value: str) -> str:
        return "```text\n" + value + "\n```"
