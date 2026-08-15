"""Minimal general agent turn loop with package-aware tool calling."""

from __future__ import annotations

import json
import asyncio
import inspect
import queue
import threading
import time
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.agent_runs import (
    AgentRunCancelled,
    AgentRunRecord,
    InMemoryAgentRunManager,
)
from app.core.llm.audit import (
    LLMCallRecord,
    classify_openai_sdk_exception,
    classify_provider_error,
    now_iso as llm_audit_now_iso,
    prompt_metadata,
    stable_llm_call_id,
    usage_token_counts,
)
from app.core.llm import (
    LLMAuthenticationError,
    LLMClientError,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseParseError,
    LLMResponseMode,
    LLMService,
    LLMTimeoutError,
    TextLLMClient,
)
from app.core.runtime_context import current_time_payload
from app.core.sessions import SessionService
from app.core.sessions import AgentSession
from app.core.sessions import SessionRecentMessage
from app.core.tools import ToolContext, ToolExecutor, ToolResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


_turn_llm_client_name: ContextVar[str | None] = ContextVar(
    "turn_llm_client_name",
    default=None,
)
_turn_llm_model: ContextVar[str | None] = ContextVar("turn_llm_model", default=None)
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
    package_catalog: list[dict[str, Any]] = Field(default_factory=list)
    session_context_window: dict[str, Any] = Field(default_factory=dict)
    expanded_tools: list[dict[str, Any]] = Field(default_factory=list)
    decision_events: list[AgentTurnDecisionEvent] = Field(default_factory=list)
    tool_events: list[AgentTurnToolEvent] = Field(default_factory=list)
    progress_events: list[AgentTurnProgressEvent] = Field(default_factory=list)
    verification_warnings: list[AgentTurnVerificationWarning] = Field(default_factory=list)
    llm_events: list[AgentTurnLLMEvent] = Field(default_factory=list)
    log_path: str | None = None


class AgentTurnLoop:
    """Small first Main Agent Brain slice for routing to packages and calling tools."""

    def __init__(
        self,
        *,
        session_service: SessionService,
        tool_executor: ToolExecutor,
        llm_client: TextLLMClient | LLMService | None,
        log_dir: Path,
        run_manager: InMemoryAgentRunManager | None = None,
    ) -> None:
        self.session_service = session_service
        self.tool_executor = tool_executor
        self.llm_client = llm_client
        self.log_dir = log_dir
        self.run_manager = run_manager
        self.llm_max_attempts = 2
        self.default_rate_limit_wait_seconds = 1.0
        self.max_decision_steps = 6
        self.llm_generation_token_budget: int | None = None
        self.session_context_token_budget = 65_536

    def run(
        self,
        *,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        client_token = _turn_llm_client_name.set(llm_client_name)
        model_token = _turn_llm_model.set(llm_model)
        mode_token = _turn_llm_response_mode.set(llm_response_mode)
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
                    payload={"session_id": session.session_id, "trace_id": trace_id},
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

    async def run_async(
        self,
        *,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
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
                            existing_run_id=existing_run_id,
                        ),
                    )
                )
            except BaseException as exc:
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
        cached_mail_messages = self._cached_mail_messages_from_session(
            session_id=session.session_id,
        )
        if cached_mail_messages:
            context_window_payload["cached_mail_messages"] = cached_mail_messages

        package_catalog = [
            package.model_dump(mode="json")
            for package in self.tool_executor.registry.list_packages()
        ]
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
                for tool in self.tool_executor.registry.list_tools(package=selected_package)
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
                    "本地邮件上下文时展开 mail package。"
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
        )
        verification_warnings = self._verify_final_answer(
            answer=answer,
            tool_events=tool_events,
        )
        for warning in verification_warnings:
            self._append_progress(
                progress_events,
                type="verification_warning",
                stage="verify",
                status=warning.severity,
                message=warning.message,
                metadata=warning.model_dump(mode="json"),
            )

        updated_context_window = self.session_service.record_context_exchange(
            session_id=session.session_id,
            user_input=user_input,
            agent_answer=answer,
            trace_id=trace_id,
            token_budget=self.session_context_token_budget,
            context_summarizer=lambda summary, messages, recent_messages, token_budget: self._summarize_context_window(
                summary=summary,
                messages_to_summarize=messages,
                retained_recent_messages=recent_messages,
                token_budget=token_budget,
                llm_events=llm_events,
            ),
        )
        result = AgentTurnResult(
            run_id=run_id,
            session_id=session.session_id,
            trace_id=trace_id,
            answer=answer,
            selected_package=selected_package if isinstance(selected_package, str) else None,
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
                "log_path": result.log_path,
                "context_window": {
                    "token_budget": updated_context_window.token_budget,
                    "token_estimate": updated_context_window.token_estimate,
                    "recent_message_count": len(updated_context_window.recent_messages),
                },
                "decision_events": [
                    event.model_dump(mode="json") for event in decision_events
                ],
                "tool_events": [event.model_dump(mode="json") for event in tool_events],
                "progress_events": [
                    event.model_dump(mode="json") for event in progress_events
                ],
                "verification_warnings": [
                    warning.model_dump(mode="json")
                    for warning in verification_warnings
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
            "null when the provided session context window, including cached local resources, "
            "is sufficient to answer without another tool call. Return only "
            'strict JSON: {"selected_package":"mail|matter|runtime|null","reason":"...",'
            '"search_query":"..."} Use mail for email evidence, matter for local tasks/events, '
            "and runtime for direct current-time questions."
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "session_context_window": context_window,
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
    ) -> dict[str, Any] | None:
        compact_output = "".join(raw_output.lower().split())
        if '"selected_package":"mail"' not in compact_output:
            return None
        return {
            "selected_package": "mail",
            "reason": "Recovered mail package from malformed route output.",
            "search_query": self._local_mail_search_query(user_input),
        }

    def _route_locally(self, user_input: str) -> dict[str, Any]:
        lower = user_input.lower()
        mail_markers = [
            "mail",
            "email",
            "邮件",
            "收件",
            "发件",
            "ntu",
            "ntuso",
            "ica",
            "visa",
            "student pass",
            "签证",
            "日程",
            "通知",
        ]
        matter_markers = [
            "matter",
            "task",
            "todo",
            "event",
            "事务",
            "事项",
            "待办",
            "任务",
            "提醒",
            "日程管理",
        ]
        runtime_markers = [
            "current time",
            "now",
            "today",
            "现在几点",
            "当前时间",
            "今天日期",
        ]
        if any(marker in lower for marker in mail_markers):
            return {
                "selected_package": "mail",
                "reason": "Local routing matched mail-related terms.",
                "search_query": self._local_mail_search_query(user_input),
            }
        if any(marker in lower for marker in matter_markers):
            return {
                "selected_package": "matter",
                "reason": "Local routing matched matter-related terms.",
                "search_query": user_input,
            }
        if any(marker in lower for marker in runtime_markers):
            return {
                "selected_package": "runtime",
                "reason": "Local routing matched runtime context terms.",
                "search_query": "",
            }
        return {"selected_package": None, "reason": "No local routing marker matched."}

    def _local_mail_search_query(self, user_input: str) -> str:
        lower = user_input.lower()
        if "ntuso" in lower:
            return "NTUSO"
        if "ica" in lower or "student pass" in lower or "签证" in lower:
            return "ICA student pass"
        if "ntu" in lower and ("日程" in lower or "schedule" in lower):
            return "NTU schedule"
        return user_input

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
        if selected_package != "mail":
            answer = (
                f"当前未配置可用 LLM，无法用本地 heuristic 完成 `{selected_package}` "
                "package 的多步骤决策。"
            )
            self._record_decision(
                decision_events,
                source="local",
                action="answer",
                answer=answer,
                reason="No local fallback is implemented for this package.",
            )
            return answer
        return self._run_mail_tools_locally(
            user_input=user_input,
            route=route,
            context=context,
            tool_events=tool_events,
            decision_events=decision_events,
            progress_events=progress_events,
        )

    def _run_mail_tools_locally(
        self,
        *,
        user_input: str,
        route: dict[str, Any],
        context: ToolContext,
        tool_events: list[AgentTurnToolEvent],
        decision_events: list[AgentTurnDecisionEvent],
        progress_events: list[AgentTurnProgressEvent],
    ) -> str:
        query = str(route.get("search_query") or user_input)
        self._record_decision(
            decision_events,
            source="local",
            action="call_tool",
            tool_name="mail.search",
            tool_input={"query": query, "limit": 8},
            reason="Local fallback searches mail before loading full messages.",
        )
        search_result = self._execute_tool(
            tool_name="mail.search",
            tool_input={"query": query, "limit": 8},
            context=context,
            tool_events=tool_events,
            progress_events=progress_events,
        )
        messages = search_result.output.get("messages", [])
        message_ids = [
            str(message["message_id"])
            for message in messages
            if isinstance(message, dict) and message.get("message_id")
        ]
        if not message_ids:
            answer = f"我在本地邮件中没有找到与 `{query}` 相关的结果。"
            self._record_decision(
                decision_events,
                source="local",
                action="answer",
                answer=answer,
                reason="mail.search returned no message ids.",
            )
            return answer

        load_input = {"message_ids": message_ids[:5]}
        self._record_decision(
            decision_events,
            source="local",
            action="call_tool",
            tool_name="mail.load_messages",
            tool_input=load_input,
            reason="Load full message bodies for the final answer.",
        )
        load_result = self._execute_tool(
            tool_name="mail.load_messages",
            tool_input=load_input,
            context=context,
            tool_events=tool_events,
            progress_events=progress_events,
        )
        loaded_messages = [
            message
            for message in load_result.output.get("messages", [])
            if isinstance(message, dict)
        ]
        answer = self._answer_locally(user_input=user_input, loaded_messages=loaded_messages)
        self._record_decision(
            decision_events,
            source="local",
            action="answer",
            answer=answer,
            reason="Local fallback answer generated from loaded messages.",
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
        cached_observation = self._cached_mail_observation_from_context_window(
            context_window,
        )
        if cached_observation is not None:
            observations.append(cached_observation)
        package_catalog = [
            package.model_dump(mode="json")
            for package in self.tool_executor.registry.list_packages()
        ]
        expanded_package_names = {selected_package}
        allowed_tool_names = {
            str(tool.get("name"))
            for tool in expanded_tools
            if isinstance(tool.get("name"), str)
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
                selected_package=selected_package,
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

            if action == "answer":
                answer = str(decision.get("answer") or "").strip()
                return answer or None

            if action == "malformed_tool_call":
                answer = (
                    "LLM 返回了疑似工具调用的损坏 JSON，系统已停止执行，避免把未执行的"
                    "工具操作误当作最终结果。请重试该请求。"
                )
                return answer

            if action == "invalid_plain_text_decision":
                loaded_messages = self._loaded_messages_from_observations(observations)
                if loaded_messages:
                    return self._answer_with_llm(
                        user_input=user_input,
                        loaded_messages=loaded_messages,
                        llm_events=llm_events,
                    )
                return None

            if action == "invalid_final_answer":
                loaded_messages = self._loaded_messages_from_observations(observations)
                if loaded_messages:
                    return self._answer_with_llm(
                        user_input=user_input,
                        loaded_messages=loaded_messages,
                        llm_events=llm_events,
                    )
                return None

            if action == "expand_package":
                package_name = str(decision.get("package_name") or "")
                if not self._package_exists(package_name):
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
                new_tools = self._tool_payloads_for_package(package_name)
                expanded_tools.extend(new_tools)
                expanded_package_names.add(package_name)
                new_tool_names = [
                    str(tool.get("name"))
                    for tool in new_tools
                    if isinstance(tool.get("name"), str)
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
                        f"Expanded `{package_name}` package with "
                        f"{len(new_tool_names)} tools."
                    ),
                    metadata={"expanded_tools": new_tool_names},
                )
                continue

            if action != "call_tool":
                return None

            tool_name = str(decision.get("tool_name") or "")
            tool_input = (
                decision.get("tool_input")
                if isinstance(decision.get("tool_input"), dict)
                else {}
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

            tool_result = self._execute_tool(
                tool_name=tool_name,
                tool_input=tool_input,
                context=context,
                tool_events=tool_events,
                progress_events=progress_events,
            )
            feedback = self._check_tool_result_with_llm(
                user_input=user_input,
                selected_package=selected_package,
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
                {
                    "tool_name": tool_name,
                    "input": tool_input,
                    "result": tool_result.model_dump(mode="json"),
                    "feedback": feedback,
                }
            )

        loaded_messages = self._loaded_messages_from_observations(observations)
        if loaded_messages:
            return self._answer_with_llm(
                user_input=user_input,
                loaded_messages=loaded_messages,
                llm_events=llm_events,
            )
        return None

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
        selected_package: str,
    ) -> dict[str, Any] | None:
        system_prompt = (
            "You are the Main Agent Brain for Local Knowledge Agent OS. Choose the next "
            "single action for this agent turn. You may call one available tool or answer. "
            f"The initially expanded package is {selected_package}. Use tools when more "
            "local evidence or persistence is needed. For mail questions, normally "
            "call mail.search first, then mail.load_messages for relevant message ids, then "
            "answer from observations. If observations already contain cached mail messages "
            "from the same session and they are relevant, answer from that cache instead of "
            "calling mail.search or mail.load_messages again. Call mail.sync first only when "
            "the user asks to sync, asks for the latest/current mailbox state, or the local "
            "mail store may be stale for the requested answer; after mail.sync, continue with "
            "mail.search and mail.load_messages as needed. The mail package does not persist "
            "matters. After reading mail evidence, expand the matter package before creating "
            "or updating tasks/events. For matter requests, use matter.search or matter.list "
            "to inspect existing matters, matter.create to save one task/event, "
            "matter.create_many to save multiple tasks/events in one controlled batch, "
            "matter.update to change status/fields, and matter.link_source to attach evidence "
            "such as mail message ids. If the currently expanded tools are insufficient, "
            "use operation.type expand_package with package_name set to one registered package; "
            "do not call tools from a package until that package appears in "
            "expanded_package_names. Follow each expanded tool input_schema exactly: include "
            "required fields, respect allowed_values/enums, and do not invent unsupported "
            "field values. For matter writes, status must be one of open, in_progress, "
            "waiting, done, cancelled; priority must be one of low, normal, high, urgent. "
            "Do not use scheduled, todo, medium, or other unsupported values. Before "
            "creating matters from extracted evidence, search existing matters when there "
            "is a realistic chance of duplicates. If matter.search returns similar items, "
            "choose update, skip/no_op, or create only with an explicit reason that the item "
            "is distinct. Use the current_time in context to "
            "resolve relative dates. For direct time questions, use runtime.now or answer from "
            "current_time if it is sufficient. Return only strict JSON using this operation-first "
            "envelope: "
            '{"operation":{"type":"tool_call|expand_package|final_answer|request_confirmation|no_op",'
            '"package_name":null,"tool_name":"mail.search","tool_input":{"query":"...","limit":8},'
            '"final_answer":null,"reason":"...","confidence":"low|medium|high"},'
            '"assistant_message":"short user-visible progress text or final answer"}. '
            "The operation object is the only executable control channel and must come first. "
            "assistant_message is display-only progress text; it never selects tools and never "
            "becomes the final answer. For a final answer, set operation.type to final_answer "
            "and put the answer in operation.final_answer. For a tool call, put all executable "
            "details in operation and only optional progress text in assistant_message. Never "
            "return plain text outside JSON."
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "session_context_window": context_window,
                "route": route,
                "package_catalog": package_catalog,
                "expanded_package_names": expanded_package_names,
                "expanded_tools": expanded_tools,
                "observations": observations,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="decision",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"agent_turn_decision observations={len(observations)}",
            max_output_tokens=self.llm_generation_token_budget,
            llm_events=llm_events,
        )
        if response is None:
            return None
        parsed = self._parse_json_object(response.content)
        if not isinstance(parsed, dict) or not parsed:
            if self._looks_like_tool_operation(response.content):
                repaired = self._repair_malformed_decision_output(
                    raw_output=response.content,
                    user_input=user_input,
                    route=route,
                    expanded_tools=expanded_tools,
                    observations=observations,
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
                return None
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
        return self._normalize_decision_output(parsed, raw_output=response.content)

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
            selected_package=selected_package
            if isinstance(selected_package, str)
            else None,
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
                answer = operation.get("final_answer")
                if not isinstance(answer, str) or not answer.strip():
                    return {
                        "action": "invalid_final_answer",
                        "assistant_message": assistant_message,
                        "operation": operation,
                        "reason": (
                            reason
                            or "operation.final_answer is required for final_answer."
                        ),
                        "_raw_output": raw_output,
                    }
                return {
                    "action": "answer",
                    "answer": answer,
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
                        "final_answer": parsed.get("answer"),
                        "reason": parsed.get("reason"),
                    }
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
            "using the operation-first envelope: {\"operation\":{\"type\":"
            "\"tool_call|expand_package|final_answer|request_confirmation|no_op\","
            "\"package_name\":null,\"tool_name\":null,\"tool_input\":{},"
            "\"final_answer\":null,\"reason\":\"...\","
            "\"confidence\":\"low|medium|high\"},\"assistant_message\":\"...\"}. "
            "Preserve a tool call only when the "
            "malformed output clearly includes the tool name and complete tool input. Do not "
            "invent missing required tool arguments."
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "route": route,
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
        if normalized.get("action") not in {"call_tool", "expand_package"}:
            return None
        normalized["_raw_output"] = raw_output
        normalized["_repair_output"] = response.content
        normalized["reason"] = (
            normalized.get("reason")
            or "Repaired malformed tool-call decision output."
        )
        return normalized

    def _package_exists(self, package_name: str) -> bool:
        return any(
            package.name == package_name
            for package in self.tool_executor.registry.list_packages()
        )

    def _tool_payloads_for_package(self, package_name: str) -> list[dict[str, Any]]:
        return [
            tool.model_dump(mode="json")
            for tool in self.tool_executor.registry.list_tools(package=package_name)
        ]

    def _cached_mail_messages_from_session(
        self,
        *,
        session_id: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        detail = self.session_service.get_session(session_id=session_id)
        cached_messages: list[dict[str, Any]] = []
        seen_message_ids: set[str] = set()
        for session_message in reversed(detail.messages):
            tool_events = session_message.payload.get("tool_events")
            if not isinstance(tool_events, list):
                continue
            for tool_event in reversed(tool_events):
                if not isinstance(tool_event, dict):
                    continue
                if tool_event.get("tool_name") != "mail.load_messages":
                    continue
                result = tool_event.get("result")
                if not isinstance(result, dict):
                    continue
                output = result.get("output")
                if not isinstance(output, dict):
                    continue
                messages = output.get("messages")
                if not isinstance(messages, list):
                    continue
                for message in reversed(messages):
                    if not isinstance(message, dict):
                        continue
                    message_id = str(message.get("message_id") or "")
                    if not message_id or message_id in seen_message_ids:
                        continue
                    cached_message = dict(message)
                    cached_message["_cache"] = {
                        "source": "session_tool_result",
                        "trace_id": session_message.payload.get("trace_id"),
                        "log_path": session_message.payload.get("log_path"),
                    }
                    cached_messages.append(cached_message)
                    seen_message_ids.add(message_id)
                    if len(cached_messages) >= limit:
                        return list(reversed(cached_messages))
        return list(reversed(cached_messages))

    def _cached_mail_observation_from_context_window(
        self,
        context_window: dict[str, Any],
    ) -> dict[str, Any] | None:
        cached_messages = context_window.get("cached_mail_messages")
        if not isinstance(cached_messages, list) or not cached_messages:
            return None
        message_ids = [
            str(message.get("message_id"))
            for message in cached_messages
            if isinstance(message, dict) and message.get("message_id")
        ]
        return {
            "tool_name": "mail.load_messages",
            "input": {
                "message_ids": message_ids,
                "source": "session_cache",
            },
            "result": {
                "invocation_id": "session_cache",
                "tool_name": "mail.load_messages",
                "status": "completed",
                "output": {"messages": cached_messages},
                "error": None,
            },
        }

    def _loaded_messages_from_observations(
        self,
        observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        for observation in reversed(observations):
            if observation.get("tool_name") != "mail.load_messages":
                continue
            result = observation.get("result")
            if not isinstance(result, dict):
                continue
            output = result.get("output")
            if not isinstance(output, dict):
                continue
            messages = output.get("messages")
            if not isinstance(messages, list):
                continue
            return [message for message in messages if isinstance(message, dict)]
        return []

    def _execute_tool(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        tool_events: list[AgentTurnToolEvent],
        progress_events: list[AgentTurnProgressEvent] | None = None,
    ) -> ToolResult:
        selected_at = _now_iso()
        if progress_events is not None:
            self._append_progress(
                progress_events,
                type="tool_started",
                stage="tool_execute",
                tool_name=tool_name,
                status="running",
                message=f"Calling `{tool_name}`.",
                metadata={"input": tool_input},
            )
        self._raise_if_cancel_requested()
        result = self.tool_executor.execute(
            invocation_id=_stable_id("tool_invocation", context.trace_id, tool_name, selected_at),
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
        if progress_events is not None:
            self._append_progress(
                progress_events,
                type="tool_completed",
                stage="tool_execute",
                tool_name=tool_name,
                status=result.status,
                message=self._tool_progress_message(tool_name=tool_name, result=result),
                metadata={"result": result.model_dump(mode="json")},
            )
        return result

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
        if tool_name == "mail.search":
            messages = output.get("messages")
            count = len(messages) if isinstance(messages, list) else 0
            return f"`{tool_name}` completed with {count} matched messages."
        if tool_name == "mail.load_messages":
            messages = output.get("messages")
            count = len(messages) if isinstance(messages, list) else 0
            return f"`{tool_name}` completed with {count} loaded messages."
        if tool_name == "matter.create_many":
            count = output.get("matters_created")
            return f"`{tool_name}` completed with {count or 0} created matters."
        if tool_name == "matter.create":
            matter = output.get("matter")
            title = matter.get("title") if isinstance(matter, dict) else None
            return (
                f"`{tool_name}` completed for `{title}`."
                if title
                else f"`{tool_name}` completed."
            )
        return f"`{tool_name}` completed."

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
            event.tool_name
            for event in tool_events
            if event.result.get("status") == "completed"
        }
        warnings: list[AgentTurnVerificationWarning] = []
        lower_answer = answer.lower()
        if ("日历" in answer or "calendar" in lower_answer) and not any(
            tool_name.startswith("calendar.") for tool_name in successful_tools
        ):
            warnings.append(
                AgentTurnVerificationWarning(
                    code="unsupported_calendar_claim",
                    message=(
                        "Final answer mentions a calendar action, but no calendar tool "
                        "was executed in this turn."
                    ),
                    evidence={"successful_tools": sorted(successful_tools)},
                )
            )

        matter_claim_markers = [
            "已创建",
            "已写入",
            "加入本地事务",
            "写入本地事务",
            "创建事务",
        ]
        matter_write_tools = {"matter.create", "matter.create_many", "mail.persist_matters"}
        if any(marker in answer for marker in matter_claim_markers) and not (
            successful_tools & matter_write_tools
        ):
            warnings.append(
                AgentTurnVerificationWarning(
                    code="unsupported_matter_write_claim",
                    message=(
                        "Final answer claims a local matter was written, but no matter "
                        "write tool completed in this turn."
                    ),
                    evidence={"successful_tools": sorted(successful_tools)},
                )
            )
        return warnings

    def _local_tool_feedback(
        self,
        *,
        tool_name: str,
        result: ToolResult,
    ) -> dict[str, Any]:
        domain_summary = self._tool_domain_summary(tool_name=tool_name, result=result)
        if result.status == "completed":
            feedback = {
                "source": "local",
                "status": "accepted",
                "message": f"{tool_name} executed successfully.",
            }
            if domain_summary:
                feedback["domain_summary"] = domain_summary
            return feedback
        feedback = {
            "source": "local",
            "status": "failed",
            "message": f"{tool_name} execution failed.",
            "error": result.error,
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
        if not tool_name.startswith("matter."):
            return {}
        output = result.output
        matters = self._extract_matter_records_from_tool_output(output)
        if not matters:
            summary: dict[str, Any] = {"tool_family": "matter", "matter_count": 0}
            if result.status == "rejected" and output.get("validation_errors"):
                summary["validation_errors"] = output["validation_errors"]
            return summary
        status_counts: dict[str, int] = {}
        priority_counts: dict[str, int] = {}
        due_count = 0
        for matter in matters:
            status = str(matter.get("status") or "unknown")
            priority = str(matter.get("priority") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            priority_counts[priority] = priority_counts.get(priority, 0) + 1
            if matter.get("due_at"):
                due_count += 1
        return {
            "tool_family": "matter",
            "matter_count": len(matters),
            "matter_status_counts": status_counts,
            "matter_priority_counts": priority_counts,
            "open_count": status_counts.get("open", 0),
            "in_progress_count": status_counts.get("in_progress", 0),
            "done_count": status_counts.get("done", 0),
            "due_count": due_count,
        }

    def _extract_matter_records_from_tool_output(
        self,
        output: dict[str, Any],
    ) -> list[dict[str, Any]]:
        matter = output.get("matter")
        if isinstance(matter, dict):
            return [matter]
        matters = output.get("matters")
        if isinstance(matters, list):
            return [item for item in matters if isinstance(item, dict)]
        return []

    def _check_tool_result_with_llm(
        self,
        *,
        user_input: str,
        selected_package: str,
        decision: dict[str, Any],
        tool_result: ToolResult,
        llm_events: list[AgentTurnLLMEvent],
    ) -> dict[str, Any]:
        local_feedback = self._local_tool_feedback(
            tool_name=tool_result.tool_name,
            result=tool_result,
        )
        if self.llm_client is None:
            return local_feedback

        system_prompt = (
            "You are the Tool Result Checker for Local Knowledge Agent OS. Check whether "
            "the just-executed tool result is a valid observation for the prior tool-call "
            "decision. Do not make a final user answer. Do not claim success when "
            "ToolResult.status is failed. Distinguish tool execution status from domain "
            "record status: ToolResult.status=completed means the tool ran, not that a "
            "matter/task is done. If tool_feedback.domain_summary is present, use it as "
            "the authoritative structured summary for business records. Return only strict JSON: {\"status\":"
            "\"accepted|needs_retry|failed\",\"message\":\"...\",\"remaining_work\":\"...\"}."
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "selected_package": selected_package,
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
            parsed.get("remaining_work")
            if isinstance(parsed.get("remaining_work"), str)
            else None
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
        loaded_messages: list[dict[str, Any]],
        llm_events: list[AgentTurnLLMEvent],
    ) -> str | None:
        if self.llm_client is None:
            return None
        system_prompt = (
            "You are the Main Agent Brain for Local Knowledge Agent OS. Use the loaded "
            "local mail messages as observations. Answer the user directly in Chinese. "
            "Mention uncertainty when the mail evidence is incomplete."
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "loaded_mail_messages": loaded_messages,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self._complete_text_with_retry(
            stage="answer",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary=f"agent_turn_answer messages={len(loaded_messages)}",
            max_output_tokens=None,
            llm_events=llm_events,
        )
        if response is None:
            return None
        return response.content

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
            "current user turn using only the provided session context window when it "
            "is sufficient. Do not invent unavailable local facts. If the context is "
            "insufficient and no tool package was selected, explain what information is "
            "missing. Return only strict JSON: {\"answer\":\"...\"}"
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
                "route": route,
                "session_context_window": context_window,
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
        parsed = self._parse_json_object(response.content)
        if isinstance(parsed, dict) and isinstance(parsed.get("answer"), str):
            return parsed["answer"].strip() or None
        return response.content.strip() or None

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
            "transcripts. Return only strict JSON: {\"summary\":\"...\"}"
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
    ) -> LLMResponse | None:
        if self.llm_client is None:
            return None

        provider = type(self.llm_client).__name__
        for attempt in range(1, self.llm_max_attempts + 1):
            started_at = llm_audit_now_iso()
            perf_start = time.perf_counter()
            llm_call_id = stable_llm_call_id(
                _turn_run_id.get(),
                stage,
                str(attempt),
                started_at,
            )
            self._append_run_event(
                type="llm_started",
                message=f"LLM call started for `{stage}`.",
                stage=stage,
                payload={
                    "llm_call_id": llm_call_id,
                    "provider": provider,
                    "attempt": attempt,
                },
            )
            try:
                response = self._complete_text_once(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    prompt_summary=prompt_summary,
                    max_output_tokens=max_output_tokens,
                    stage=stage,
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
                    message=f"LLM call rate limited for `{stage}`.",
                    stage=stage,
                    payload={
                        "llm_call_id": llm_call_id,
                        "provider": provider,
                        "attempt": attempt,
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
                    message=f"LLM call failed for `{stage}`.",
                    stage=stage,
                    payload={
                        "llm_call_id": llm_call_id,
                        "provider": provider,
                        "attempt": attempt,
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
                message=f"LLM call completed for `{stage}`.",
                stage=stage,
                payload={
                    "llm_call_id": llm_call_id,
                    "provider": response.provider,
                    "attempt": attempt,
                    "status": response.status,
                    "content_length": len(response.content),
                    "provider_request_id": response.provider_request_id,
                    "finish_reason": response.finish_reason,
                    "audit_record": llm_event.audit_record,
                },
            )
            return response
        return None

    def _complete_text_once(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        max_output_tokens: int | None,
        stage: str,
    ) -> LLMResponse:
        if self.llm_client is None:
            raise LLMClientError("No LLM client is configured.")
        try:
            result = self.llm_client.complete_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_summary=prompt_summary,
                temperature=0.0,
                max_output_tokens=max_output_tokens,
                client_name=_turn_llm_client_name.get(),
                model=_turn_llm_model.get(),
                response_mode=_turn_llm_response_mode.get(),
                require_json=stage not in {"answer"},
                metadata={"stage": stage},
            )
        except TypeError:
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
        record_metadata.update(prompt_metadata(system_prompt=system_prompt, user_prompt=user_prompt))
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
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delta = retry_at - datetime.now(timezone.utc)
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
        run_manager.append_event(
            run_id,
            "run_completed",
            "Agent run completed.",
            stage="run",
            payload={
                "answer_length": len(result.answer),
                "selected_package": result.selected_package,
                "tool_event_count": len(result.tool_events),
                "llm_event_count": len(result.llm_events),
                "log_path": result.log_path,
            },
        )
        run_manager.complete_run(
            run_id,
            result_snapshot={
                "session_id": result.session_id,
                "trace_id": result.trace_id,
                "answer": result.answer,
                "selected_package": result.selected_package,
            },
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
        run_manager.fail_run(run_id, error_type=error_type, error=error)

    def _mark_current_run_cancelled(self, reason: str) -> None:
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return
        run_manager.append_event(
            run_id,
            "run_cancelled",
            reason or "Agent run cancelled.",
            stage="run",
            payload={"reason": reason},
        )
        run_manager.mark_cancelled(run_id, reason=reason)

    def _raise_if_cancel_requested(self) -> None:
        run_manager = _turn_run_manager.get()
        run_id = _turn_run_id.get()
        if run_manager is None or run_id is None:
            return
        if run_manager.is_cancel_requested(run_id):
            reason = run_manager.cancel_reason(run_id) or "Run cancelled."
            raise AgentRunCancelled(reason)

    def _answer_locally(self, *, user_input: str, loaded_messages: list[dict[str, Any]]) -> str:
        lines = [
            "我已调用本地 mail tools 检索并加载相关邮件。当前未配置可用 LLM，下面是本地摘要：",
            f"用户问题：{user_input}",
        ]
        for index, message in enumerate(loaded_messages, start=1):
            body = str(message.get("body_text") or "").strip().replace("\r", "")
            snippet = body[:300] + ("..." if len(body) > 300 else "")
            lines.extend(
                [
                    "",
                    f"{index}. {message.get('subject') or '(no subject)'}",
                    f"   from: {message.get('sender') or ''}",
                    f"   received_at: {message.get('received_at') or ''}",
                    f"   snippet: {snippet}",
                ]
            )
        return "\n".join(lines)

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
            f"- selected_package: `{result.selected_package}`",
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
            self._json_block(
                [event.model_dump(mode="json") for event in result.decision_events]
            ),
            "",
            "## Tool Events",
            "",
            self._json_block([event.model_dump(mode="json") for event in result.tool_events]),
            "",
            "## Progress Events",
            "",
            self._json_block(
                [event.model_dump(mode="json") for event in result.progress_events]
            ),
            "",
            "## Verification Warnings",
            "",
            self._json_block(
                [
                    warning.model_dump(mode="json")
                    for warning in result.verification_warnings
                ]
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
        return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"

    def _text_block(self, value: str) -> str:
        return "```text\n" + value + "\n```"
