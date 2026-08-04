"""Minimal general agent turn loop with package-aware tool calling."""

from __future__ import annotations

import json
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.llm import (
    LLMAuthenticationError,
    LLMClientError,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseParseError,
    LLMTimeoutError,
    TextLLMClient,
)
from app.core.sessions import SessionService
from app.core.tools import ToolContext, ToolExecutor


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class AgentTurnToolEvent(BaseModel):
    tool_name: str
    selected_at: str
    completed_at: str
    input: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class AgentTurnLLMEvent(BaseModel):
    stage: str
    provider: str
    status: str
    system_prompt: str
    user_prompt: str
    output: str
    attempt: int = 1
    error_type: str | None = None
    status_code: int | None = None
    retry_after: str | None = None
    error: str | None = None


class AgentTurnResult(BaseModel):
    session_id: str
    trace_id: str
    answer: str
    selected_package: str | None = None
    package_catalog: list[dict[str, Any]] = Field(default_factory=list)
    expanded_tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_events: list[AgentTurnToolEvent] = Field(default_factory=list)
    llm_events: list[AgentTurnLLMEvent] = Field(default_factory=list)
    log_path: str | None = None


class AgentTurnLoop:
    """Small first Main Agent Brain slice for routing to packages and calling tools."""

    def __init__(
        self,
        *,
        session_service: SessionService,
        tool_executor: ToolExecutor,
        llm_client: TextLLMClient | None,
        log_dir: Path,
    ) -> None:
        self.session_service = session_service
        self.tool_executor = tool_executor
        self.llm_client = llm_client
        self.log_dir = log_dir
        self.llm_max_attempts = 2
        self.default_rate_limit_wait_seconds = 1.0

    def run(
        self,
        *,
        session_id: str | None,
        user_input: str,
    ) -> AgentTurnResult:
        session = self.session_service.ensure_session(
            session_id=session_id,
            title=user_input.strip()[:60] or "Agent Session",
            metadata={"entrypoint": "agent.turn"},
        )
        trace_id = _stable_id("agent_turn", session.session_id, user_input, _now_iso())
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

        package_catalog = [
            package.model_dump(mode="json")
            for package in self.tool_executor.registry.list_packages()
        ]
        llm_events: list[AgentTurnLLMEvent] = []
        route = self._route(user_input=user_input, package_catalog=package_catalog, llm_events=llm_events)
        selected_package = route.get("selected_package")
        tool_events: list[AgentTurnToolEvent] = []
        expanded_tools: list[dict[str, Any]] = []
        answer = ""

        if selected_package == "mail":
            expanded_tools = [
                tool.model_dump(mode="json")
                for tool in self.tool_executor.registry.list_tools(package="mail")
            ]
            answer = self._run_mail_tools(
                user_input=user_input,
                route=route,
                context=context,
                tool_events=tool_events,
                llm_events=llm_events,
            )
        else:
            answer = (
                "我还没有为这个请求选择到可执行工具。当前最小 Agent turn 只支持在需要"
                "本地邮件上下文时展开 mail package。"
            )

        result = AgentTurnResult(
            session_id=session.session_id,
            trace_id=trace_id,
            answer=answer,
            selected_package=selected_package if isinstance(selected_package, str) else None,
            package_catalog=package_catalog,
            expanded_tools=expanded_tools,
            tool_events=tool_events,
            llm_events=llm_events,
        )
        log_path = self._write_log(result=result, user_input=user_input)
        result = result.model_copy(update={"log_path": str(log_path)})
        self.session_service.append_message(
            session_id=session.session_id,
            role="agent",
            content=answer,
            payload={
                "trace_id": trace_id,
                "selected_package": result.selected_package,
                "log_path": result.log_path,
                "tool_events": [event.model_dump(mode="json") for event in tool_events],
            },
        )
        return result

    def _route(
        self,
        *,
        user_input: str,
        package_catalog: list[dict[str, Any]],
        llm_events: list[AgentTurnLLMEvent],
    ) -> dict[str, Any]:
        if self.llm_client is None:
            return self._route_locally(user_input)

        system_prompt = (
            "You are the Main Agent Brain for Local Knowledge Agent OS. Choose at most one "
            "tool package for the current turn. Do not choose concrete tools yet. Return only "
            'strict JSON: {"selected_package":"mail|null","reason":"...",'
            '"search_query":"..."}'
        )
        user_prompt = json.dumps(
            {
                "user_input": user_input,
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
            max_output_tokens=400,
            llm_events=llm_events,
        )
        if response is None:
            return self._route_locally(user_input)

        parsed = self._parse_json_object(response.content)
        if not isinstance(parsed, dict):
            return self._route_locally(user_input)
        if parsed.get("selected_package") == "mail":
            return parsed
        return {"selected_package": None, "reason": parsed.get("reason") or "No package selected."}

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
        if any(marker in lower for marker in mail_markers):
            return {
                "selected_package": "mail",
                "reason": "Local routing matched mail-related terms.",
                "search_query": self._local_mail_search_query(user_input),
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

    def _run_mail_tools(
        self,
        *,
        user_input: str,
        route: dict[str, Any],
        context: ToolContext,
        tool_events: list[AgentTurnToolEvent],
        llm_events: list[AgentTurnLLMEvent],
    ) -> str:
        query = str(route.get("search_query") or user_input)
        search_result = self._execute_tool(
            tool_name="mail.search",
            tool_input={"query": query, "limit": 8},
            context=context,
            tool_events=tool_events,
        )
        messages = search_result.output.get("messages", [])
        message_ids = [
            str(message["message_id"])
            for message in messages
            if isinstance(message, dict) and message.get("message_id")
        ]
        if not message_ids:
            return f"我在本地邮件中没有找到与 `{query}` 相关的结果。"

        load_result = self._execute_tool(
            tool_name="mail.load_messages",
            tool_input={"message_ids": message_ids[:5]},
            context=context,
            tool_events=tool_events,
        )
        loaded_messages = [
            message
            for message in load_result.output.get("messages", [])
            if isinstance(message, dict)
        ]
        if self.llm_client is not None:
            answer = self._answer_with_llm(
                user_input=user_input,
                loaded_messages=loaded_messages,
                llm_events=llm_events,
            )
            if answer:
                return answer
        return self._answer_locally(user_input=user_input, loaded_messages=loaded_messages)

    def _execute_tool(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        tool_events: list[AgentTurnToolEvent],
    ) -> Any:
        selected_at = _now_iso()
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
            )
        )
        return result

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
            try:
                response = self.llm_client.complete_text(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    prompt_summary=prompt_summary,
                    temperature=0.0,
                    max_output_tokens=max_output_tokens,
                )
            except LLMRateLimitError as exc:
                llm_events.append(
                    self._llm_error_event(
                        stage=stage,
                        provider=provider,
                        attempt=attempt,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        exc=exc,
                    )
                )
                if attempt >= self.llm_max_attempts:
                    return None
                time.sleep(self._retry_after_seconds(exc.retry_after))
                continue
            except LLMClientError as exc:
                llm_events.append(
                    self._llm_error_event(
                        stage=stage,
                        provider=provider,
                        attempt=attempt,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        exc=exc,
                    )
                )
                return None

            llm_events.append(
                AgentTurnLLMEvent(
                    stage=stage,
                    provider=response.provider,
                    status=response.status,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    output=response.content,
                    attempt=attempt,
                )
            )
            return response
        return None

    def _llm_error_event(
        self,
        *,
        stage: str,
        provider: str,
        attempt: int,
        system_prompt: str,
        user_prompt: str,
        exc: LLMClientError,
    ) -> AgentTurnLLMEvent:
        status_code = exc.status_code if isinstance(exc, LLMProviderHTTPError) else None
        retry_after = exc.retry_after if isinstance(exc, LLMProviderHTTPError) else None
        return AgentTurnLLMEvent(
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
            "## Expanded Tools",
            "",
            self._json_block(result.expanded_tools),
            "",
            "## Tool Events",
            "",
            self._json_block([event.model_dump(mode="json") for event in result.tool_events]),
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
