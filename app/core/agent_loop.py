"""Agent loops that coordinate package tools, LLM reasoning, and feedback."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from app.core.llm import LLMClientError, TextLLMClient
from app.core.mail import MailMatterDraft, MailMessageRecord, MailProcessResult, MailService
from app.core.tools import ToolContext, ToolExecutor


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class MailProcessingAgentLoop:
    """Fixed first mail loop after the agent has routed a request to the mail package."""

    def __init__(
        self,
        *,
        mail_service: MailService,
        tool_executor: ToolExecutor,
        llm_client: TextLLMClient | None,
    ) -> None:
        self.mail_service = mail_service
        self.tool_executor = tool_executor
        self.llm_client = llm_client

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

        search_result = self.tool_executor.execute(
            invocation_id=_stable_id("tool_invocation", run_id, "mail.search"),
            tool_name="mail.search",
            tool_input={"query": query or "", "limit": limit},
            context=context,
        )
        message_ids = [
            str(message["message_id"])
            for message in search_result.output.get("messages", [])
            if isinstance(message, dict) and message.get("message_id")
        ]

        load_result = self.tool_executor.execute(
            invocation_id=_stable_id("tool_invocation", run_id, "mail.load_messages"),
            tool_name="mail.load_messages",
            tool_input={"message_ids": message_ids},
            context=context,
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
                drafts = self._draft_matters_with_llm(query=query, messages=messages)
                provider = "agent_llm"
            except LLMClientError:
                provider = "agent_local_heuristic_after_llm_error"

        persist_result = self.tool_executor.execute(
            invocation_id=_stable_id("tool_invocation", run_id, "mail.persist_matters"),
            tool_name="mail.persist_matters",
            tool_input={
                "drafts": [draft.model_dump(mode="json") for draft in drafts],
                "provider": provider,
                "link_reason": "Agent mail processing run.",
            },
            context=context,
        )
        matters_created = int(persist_result.output.get("matters_created") or 0)

        return self.mail_service.record_processing_run(
            run_id=run_id,
            query=query,
            status="completed",
            processed_messages=len(messages),
            matters_created=matters_created,
            provider=provider,
        )

    def _draft_matters_with_llm(
        self,
        *,
        query: str | None,
        messages: list[MailMessageRecord],
    ) -> list[MailMatterDraft]:
        if self.llm_client is None:
            return self.mail_service.draft_matters_locally(messages)

        response = self.llm_client.complete_text(
            system_prompt=(
                "You are the agent loop for Local Knowledge Agent OS. The mail package "
                "has already loaded complete email bodies. Extract actionable matters. "
                "Return only strict JSON with this shape: "
                '{"matters":[{"title":"...","summary":"...","status":"open",'
                '"priority":"low|normal|high","source_message_ids":["mail_msg_..."]}]}. '
                "Do not omit important deadlines, missing documents, or sender requests."
            ),
            user_prompt=self._mail_llm_prompt(query=query, messages=messages),
            prompt_summary=f"mail_agent_loop query={query or ''} messages={len(messages)}",
            temperature=0.0,
            max_output_tokens=None,
        )
        drafts = self._parse_llm_matter_drafts(response.content)
        if not drafts:
            raise LLMClientError("LLM response did not contain any mail matters.")
        return self._normalize_source_message_ids(drafts=drafts, messages=messages)

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
