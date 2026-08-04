"""Agent loops that coordinate package tools, LLM reasoning, and feedback."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from app.core.agent_logging import AgentRunLogger
from app.core.llm import (
    LLMAuthenticationError,
    LLMClientError,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMTimeoutError,
    TextLLMClient,
)
from app.core.mail import MailMatterDraft, MailMessageRecord, MailProcessResult, MailService
from app.core.tools import ToolContext, ToolExecutor, ToolResult


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class MailAgentLLMError(LLMClientError):
    def __init__(self, message: str, llm_event: dict[str, Any]) -> None:
        super().__init__(message)
        self.llm_event = llm_event


class MailProcessingAgentLoop:
    """Fixed first mail loop after the agent has routed a request to the mail package."""

    def __init__(
        self,
        *,
        mail_service: MailService,
        tool_executor: ToolExecutor,
        llm_client: TextLLMClient | None,
        run_logger: AgentRunLogger,
        rate_limit_max_retries: int = 1,
        rate_limit_default_delay_seconds: float = 5.0,
        rate_limit_max_delay_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.mail_service = mail_service
        self.tool_executor = tool_executor
        self.llm_client = llm_client
        self.run_logger = run_logger
        self.rate_limit_max_retries = rate_limit_max_retries
        self.rate_limit_default_delay_seconds = rate_limit_default_delay_seconds
        self.rate_limit_max_delay_seconds = rate_limit_max_delay_seconds
        self.sleep = sleep

    def run(
        self,
        *,
        session_id: str,
        query: str | None,
        limit: int,
    ) -> MailProcessResult:
        run_id = _stable_id(
            "mail_run",
            session_id,
            query or "",
            str(limit),
            datetime.now(timezone.utc).isoformat(),
        )
        context = ToolContext(session_id=session_id, trace_id=run_id, context_id=run_id)

        tool_events: list[dict[str, Any]] = []
        llm_event: dict[str, Any] | None = None
        package_catalog = [
            package.model_dump(mode="json")
            for package in self.tool_executor.registry.list_packages()
        ]
        user_input = f"Process local mail with query: {query or '(latest mail)'}"

        search_result = self._execute_tool_with_log(
            run_id=run_id,
            tool_name="mail.search",
            tool_input={"query": query or "", "limit": limit},
            context=context,
            tool_events=tool_events,
        )
        message_ids = [
            str(message["message_id"])
            for message in search_result.output.get("messages", [])
            if isinstance(message, dict) and message.get("message_id")
        ]

        load_result = self._execute_tool_with_log(
            run_id=run_id,
            tool_name="mail.load_messages",
            tool_input={"message_ids": message_ids},
            context=context,
            tool_events=tool_events,
        )
        messages = [
            MailMessageRecord.model_validate(message)
            for message in load_result.output.get("messages", [])
            if isinstance(message, dict)
        ]

        provider = "agent_local_heuristic"
        drafts = self.mail_service.draft_matters_locally(messages)
        if self.llm_client is not None and messages:
            try:
                drafts, llm_event = self._draft_matters_with_llm_with_retry(
                    query=query,
                    messages=messages,
                )
                provider = "agent_llm"
            except MailAgentLLMError as exc:
                llm_event = exc.llm_event
                provider = self._fallback_provider_for_llm_event(llm_event)
            except LLMClientError as exc:
                llm_event = {
                    "provider": type(self.llm_client).__name__,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "started_at": None,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "system_prompt": "",
                    "user_prompt": "",
                    "output": str(exc),
                }
                provider = self._fallback_provider_for_llm_event(llm_event)

        persist_result = self._execute_tool_with_log(
            run_id=run_id,
            tool_name="mail.persist_matters",
            tool_input={
                "drafts": [draft.model_dump(mode="json") for draft in drafts],
                "provider": provider,
                "link_reason": "Agent mail processing run.",
            },
            context=context,
            tool_events=tool_events,
        )
        matters_created = int(persist_result.output.get("matters_created") or 0)

        result = self.mail_service.record_processing_run(
            run_id=run_id,
            session_id=session_id,
            query=query,
            status="completed",
            processed_messages=len(messages),
            matters_created=matters_created,
            provider=provider,
        )
        log_path = self.run_logger.write_mail_process_log(
            run_id=run_id,
            session_id=session_id,
            user_input=user_input,
            package_catalog=package_catalog,
            expanded_package="mail",
            tool_events=tool_events,
            llm_event=llm_event,
            final_result=result.model_dump(mode="json"),
        )
        return result.model_copy(update={"log_path": str(log_path)})

    def _draft_matters_with_llm_with_retry(
        self,
        *,
        query: str | None,
        messages: list[MailMessageRecord],
    ) -> tuple[list[MailMatterDraft], dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        retry_count = 0
        waited_seconds = 0.0
        max_attempts = max(1, self.rate_limit_max_retries + 1)

        for attempt in range(1, max_attempts + 1):
            try:
                drafts, llm_event = self._draft_matters_with_llm(
                    query=query,
                    messages=messages,
                )
                llm_event["attempt"] = attempt
                llm_event["retry_count"] = retry_count
                llm_event["waited_seconds"] = waited_seconds
                if attempts:
                    llm_event["attempts"] = [*attempts, self._copy_llm_attempt(llm_event)]
                return drafts, llm_event
            except MailAgentLLMError as exc:
                llm_event = exc.llm_event
                llm_event["attempt"] = attempt
                attempts.append(self._copy_llm_attempt(llm_event))
                if llm_event.get("status") != "rate_limited" or attempt >= max_attempts:
                    llm_event["retry_count"] = retry_count
                    llm_event["waited_seconds"] = waited_seconds
                    llm_event["attempts"] = attempts
                    raise MailAgentLLMError(str(exc), llm_event) from exc

                delay = self._rate_limit_delay_seconds(llm_event.get("retry_after"))
                llm_event["retry_scheduled"] = True
                llm_event["retry_delay_seconds"] = delay
                attempts[-1] = self._copy_llm_attempt(llm_event)
                retry_count += 1
                waited_seconds += delay
                self.sleep(delay)

        raise MailAgentLLMError(
            "LLM retry loop exited without a result.",
            {
                "provider": type(self.llm_client).__name__ if self.llm_client else "none",
                "status": "failed",
                "error_type": "RetryLoopExited",
                "started_at": None,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "system_prompt": "",
                "user_prompt": "",
                "output": "LLM retry loop exited without a result.",
                "fallback": "local_heuristic",
                "retry_count": retry_count,
                "waited_seconds": waited_seconds,
                "attempts": attempts,
            },
        )

    def _copy_llm_attempt(self, llm_event: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in llm_event.items() if key != "attempts"}

    def _rate_limit_delay_seconds(self, retry_after: object) -> float:
        if retry_after is None:
            return self.rate_limit_default_delay_seconds
        try:
            retry_after_seconds = float(str(retry_after).strip())
        except ValueError:
            return self.rate_limit_default_delay_seconds
        if retry_after_seconds < 0:
            return self.rate_limit_default_delay_seconds
        return min(retry_after_seconds, self.rate_limit_max_delay_seconds)

    def _fallback_provider_for_llm_event(self, llm_event: dict[str, Any]) -> str:
        status = llm_event.get("status")
        if status == "rate_limited":
            return "agent_local_heuristic_after_rate_limit"
        if status == "auth_failed":
            return "agent_local_heuristic_after_auth_error"
        if status == "network_failed":
            return "agent_local_heuristic_after_network_error"
        if status == "timeout":
            return "agent_local_heuristic_after_timeout"
        if status == "http_failed":
            return "agent_local_heuristic_after_provider_http_error"
        if status in {"response_parse_failed", "parse_failed"}:
            return "agent_local_heuristic_after_parse_error"
        return "agent_local_heuristic_after_llm_error"

    def _execute_tool_with_log(
        self,
        *,
        run_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        tool_events: list[dict[str, Any]],
    ) -> ToolResult:
        selected_at = datetime.now(timezone.utc).isoformat()
        result = self.tool_executor.execute(
            invocation_id=_stable_id("tool_invocation", run_id, tool_name),
            tool_name=tool_name,
            tool_input=tool_input,
            context=context,
        )
        tool_events.append(
            {
                "tool_name": tool_name,
                "selected_at": selected_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "input": tool_input,
                "result": result.model_dump(mode="json"),
            }
        )
        return result

    def _draft_matters_with_llm(
        self,
        *,
        query: str | None,
        messages: list[MailMessageRecord],
    ) -> tuple[list[MailMatterDraft], dict[str, Any]]:
        if self.llm_client is None:
            return self.mail_service.draft_matters_locally(messages), {
                "provider": "none",
                "status": "skipped",
                "started_at": None,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "system_prompt": "",
                "user_prompt": "",
                "output": "No LLM client was configured.",
            }

        system_prompt = (
            "You are the agent loop for Local Knowledge Agent OS. The mail package "
            "has already loaded complete email bodies. Extract actionable matters. "
            "Return only strict JSON with this shape: "
            '{"matters":[{"title":"...","summary":"...","status":"open",'
            '"priority":"low|normal|high","source_message_ids":["mail_msg_..."]}]}. '
            "Do not omit important deadlines, missing documents, or sender requests."
        )
        user_prompt = self._mail_llm_prompt(query=query, messages=messages)
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            response = self.llm_client.complete_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_summary=f"mail_agent_loop query={query or ''} messages={len(messages)}",
                temperature=0.0,
                max_output_tokens=None,
            )
        except LLMRateLimitError as exc:
            raise MailAgentLLMError(
                str(exc),
                {
                    "provider": type(self.llm_client).__name__,
                    "status": "rate_limited",
                    "error_type": type(exc).__name__,
                    "status_code": exc.status_code,
                    "retry_after": exc.retry_after,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "output": str(exc),
                    "fallback": "local_heuristic",
                },
            ) from exc
        except LLMAuthenticationError as exc:
            raise self._mail_agent_llm_error(
                exc=exc,
                status="auth_failed",
                started_at=started_at,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                status_code=exc.status_code,
                retry_after=exc.retry_after,
            ) from exc
        except LLMTimeoutError as exc:
            raise self._mail_agent_llm_error(
                exc=exc,
                status="timeout",
                started_at=started_at,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            ) from exc
        except LLMNetworkError as exc:
            raise self._mail_agent_llm_error(
                exc=exc,
                status="network_failed",
                started_at=started_at,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            ) from exc
        except LLMProviderHTTPError as exc:
            raise self._mail_agent_llm_error(
                exc=exc,
                status="http_failed",
                started_at=started_at,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                status_code=exc.status_code,
                retry_after=exc.retry_after,
            ) from exc
        except LLMResponseParseError as exc:
            raise self._mail_agent_llm_error(
                exc=exc,
                status="response_parse_failed",
                started_at=started_at,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            ) from exc
        except LLMClientError as exc:
            raise MailAgentLLMError(
                str(exc),
                {
                    "provider": type(self.llm_client).__name__,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "output": str(exc),
                },
            ) from exc
        llm_event = {
            "provider": response.provider,
            "status": response.status,
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "output": response.content,
        }
        drafts = self._parse_llm_matter_drafts(response.content)
        if not drafts:
            llm_event["status"] = "parse_failed"
            llm_event["error_type"] = "LLMOutputParseError"
            llm_event["fallback"] = "local_heuristic"
            raise MailAgentLLMError(
                "LLM response did not contain any mail matters.",
                llm_event,
            )
        return self._normalize_source_message_ids(drafts=drafts, messages=messages), llm_event

    def _mail_agent_llm_error(
        self,
        *,
        exc: LLMClientError,
        status: str,
        started_at: str,
        system_prompt: str,
        user_prompt: str,
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> MailAgentLLMError:
        return MailAgentLLMError(
            str(exc),
            {
                "provider": type(self.llm_client).__name__,
                "status": status,
                "error_type": type(exc).__name__,
                "status_code": status_code,
                "retry_after": retry_after,
                "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "output": str(exc),
                "fallback": "local_heuristic",
            },
        )

    def _normalize_source_message_ids(
        self,
        *,
        drafts: list[MailMatterDraft],
        messages: list[MailMessageRecord],
    ) -> list[MailMatterDraft]:
        valid_message_ids = {message.message_id for message in messages}
        normalized_drafts: list[MailMatterDraft] = []
        for index, draft in enumerate(drafts):
            source_message_ids = [
                message_id
                for message_id in draft.source_message_ids
                if message_id in valid_message_ids
            ]
            if not source_message_ids and index < len(messages):
                source_message_ids = [messages[index].message_id]
            normalized_drafts.append(
                draft.model_copy(update={"source_message_ids": source_message_ids})
            )
        return normalized_drafts

    def _mail_llm_prompt(self, *, query: str | None, messages: list[MailMessageRecord]) -> str:
        sections = [
            f"User mail-processing request: {query or '(latest mail)'}",
            "Use every complete email body below. Do not rely on search snippets.",
        ]
        for index, message in enumerate(messages, start=1):
            attachments = [
                {
                    "external_id": attachment.external_id,
                    "name": attachment.name,
                    "content_type": attachment.content_type,
                    "size": attachment.size,
                }
                for attachment in message.attachments
            ]
            sections.append(
                "\n".join(
                    [
                        f"EMAIL {index}",
                        f"message_id: {message.message_id}",
                        f"folder: {message.folder}",
                        f"subject: {message.subject}",
                        f"sender: {message.sender}",
                        f"to: {', '.join(message.recipients)}",
                        f"cc: {', '.join(message.cc)}",
                        f"received_at: {message.received_at or ''}",
                        f"attachments: {json.dumps(attachments, ensure_ascii=False)}",
                        "body_text:",
                        message.body_text,
                        "END_EMAIL",
                    ]
                )
            )
        return "\n\n".join(sections)

    def _parse_llm_matter_drafts(self, content: str) -> list[MailMatterDraft]:
        payload = self._parse_json_object(content)
        matters = payload.get("matters") if isinstance(payload, dict) else None
        if not isinstance(matters, list):
            return []

        drafts: list[MailMatterDraft] = []
        for item in matters:
            if not isinstance(item, dict):
                continue
            try:
                drafts.append(MailMatterDraft.model_validate(item))
            except ValueError:
                continue
        return drafts

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
