"""Deterministic LLM used by runtime evals.

This client follows the current Agent Harness prompts and returns strict
operation-first JSON for route/decision stages. It is intentionally simple:
the benchmark measures harness/tool/log contracts deterministically, while
HTTP/SSE subjects can be used against a real configured provider.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.llm import LLMResponse, LLMResponseMode


class EvalScriptedLLM:
    """Scripted, prompt-stage-aware LLM for reproducible benchmark runs."""

    name = "eval_scripted"
    provider_name = "eval_scripted"
    default_model = "eval-scripted-v1"
    available_models = ["eval-scripted-v1"]
    supports_stream = False
    supports_json_mode = True

    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.calls: list[dict[str, Any]] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        client_name: str | None = None,
        model: str | None = None,
        response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        require_json: bool = False,
        metadata: dict | None = None,
    ) -> LLMResponse:
        payload = _json_object(user_prompt)
        stage = _stage_from_prompt(prompt_summary)
        content = self._content_for(stage=stage, payload=payload)
        self.calls.append(
            {
                "stage": stage,
                "prompt_summary": prompt_summary,
                "client_name": client_name,
                "model": model,
                "response_mode": response_mode.value
                if isinstance(response_mode, LLMResponseMode)
                else str(response_mode),
                "content": content,
            }
        )
        return LLMResponse(
            provider=self.provider_name,
            client_name=self.name,
            model=model or self.default_model,
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
            response_mode=response_mode,
            usage={
                "prompt_tokens": max(1, len(system_prompt + user_prompt) // 4),
                "completion_tokens": max(1, len(content) // 4),
                "total_tokens": max(1, len(system_prompt + user_prompt + content) // 4),
            },
            finish_reason="stop",
            metadata={"eval_stage": stage},
        )

    def _content_for(self, *, stage: str, payload: dict[str, Any]) -> str:
        if stage == "route":
            return json.dumps(self._route(payload), ensure_ascii=False)
        if stage == "decision":
            return json.dumps(self._decision(payload), ensure_ascii=False)
        if stage == "tool_result_check":
            return json.dumps(
                {
                    "status": "accepted",
                    "message": "Eval scripted checker accepted the observation.",
                    "remaining_work": "",
                },
                ensure_ascii=False,
            )
        if stage in {"answer", "context_answer"}:
            return self._answer(payload)
        if stage == "decision_repair":
            return json.dumps(
                {
                    "operation": {
                        "type": "final_answer",
                        "package_name": None,
                        "tool_name": None,
                        "tool_input": {},
                        "final_answer": None,
                        "reason": "Eval repair fallback.",
                        "confidence": "low",
                    },
                    "assistant_message": "进入最终回答阶段。",
                },
                ensure_ascii=False,
            )
        if stage == "context_summarize":
            return "Eval context summary."
        return "Eval scripted response."

    def _route(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected = self.case.get("expect") if isinstance(self.case.get("expect"), dict) else {}
        selected = expected.get("selected_package")
        if not isinstance(selected, str):
            selected = self.case.get("selected_package")
        if not isinstance(selected, str):
            selected = "mail"
        if selected == "none":
            selected = None
        return {
            "selected_package": selected,
            "reason": "Eval scripted route.",
            "search_query": self._search_query(payload),
        }

    def _decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        observations = payload.get("observations")
        observations = observations if isinstance(observations, list) else []
        user_input = str(payload.get("user_input") or "")
        expected = self.case.get("expect") if isinstance(self.case.get("expect"), dict) else {}
        workflow = str(self.case.get("workflow") or expected.get("workflow") or "mail_qa")

        if isinstance(self.case.get("tool_plan"), list):
            return self._tool_plan_decision(observations=observations)
        if workflow == "mail_to_matter":
            return self._mail_to_matter_decision(observations=observations)

        if observations and _has_cached_loaded_mail(observations):
            return _final_answer_decision("Cached mail evidence is sufficient.")
        if not observations:
            return _tool_call_decision(
                tool_name="mail.search",
                tool_input={
                    "query": self._search_query(payload) or user_input,
                    "limit": int(self.case.get("search_limit") or 10),
                },
                message="搜索本地邮件。",
            )
        if _has_tool_observation(observations, "mail.search") and not _has_tool_observation(
            observations, "mail.load_messages"
        ):
            message_ids = _message_ids_from_search(observations)
            return _tool_call_decision(
                tool_name="mail.load_messages",
                tool_input={"message_ids": message_ids},
                message="加载相关邮件全文。",
            )
        return _final_answer_decision("Loaded mail evidence is sufficient.")

    def _mail_to_matter_decision(self, *, observations: list[dict[str, Any]]) -> dict[str, Any]:
        if not observations:
            return _tool_call_decision(
                tool_name="mail.search",
                tool_input={
                    "query": self._case_search_query(),
                    "limit": int(self.case.get("search_limit") or 10),
                },
                message="搜索可转成事务的邮件。",
            )
        if _has_tool_observation(observations, "mail.search") and not _has_tool_observation(
            observations, "mail.load_messages"
        ):
            return _tool_call_decision(
                tool_name="mail.load_messages",
                tool_input={"message_ids": _message_ids_from_search(observations)},
                message="读取邮件证据。",
            )
        if "matter" not in _expanded_package_names_from_case(self.case) and not _has_expand_observation(
            observations, "matter"
        ):
            return {
                "operation": {
                    "type": "expand_package",
                    "package_name": "matter",
                    "tool_name": None,
                    "tool_input": {},
                    "final_answer": None,
                    "reason": "Need matter persistence tools.",
                    "confidence": "high",
                },
                "assistant_message": "展开事务工具。",
            }
        if not _has_tool_observation(observations, "matter.create"):
            matter_payload = self.case.get("matter")
            if not isinstance(matter_payload, dict):
                matter_payload = {
                    "title": "Eval extracted matter",
                    "summary": "Matter extracted from eval mail evidence.",
                    "status": "open",
                    "priority": "normal",
                    "tags": ["eval"],
                    "source_links": _source_links_from_loaded_mail(observations),
                    "metadata": {"source": "eval"},
                }
            else:
                matter_payload = dict(matter_payload)
                if not matter_payload.get("source_links"):
                    matter_payload["source_links"] = _source_links_from_loaded_mail(observations)
            return _tool_call_decision(
                tool_name="matter.create",
                tool_input=matter_payload,
                message="创建本地事务。",
            )
        return _final_answer_decision("Matter has been created from evidence.")

    def _tool_plan_decision(self, *, observations: list[dict[str, Any]]) -> dict[str, Any]:
        tool_plan = self.case.get("tool_plan")
        tool_plan = tool_plan if isinstance(tool_plan, list) else []
        completed_tool_count = sum(
            1
            for observation in observations
            if isinstance(observation, dict) and isinstance(observation.get("tool_name"), str)
        )
        if completed_tool_count >= len(tool_plan):
            return _final_answer_decision("Eval tool plan completed.")
        step = tool_plan[completed_tool_count]
        if not isinstance(step, dict):
            return _final_answer_decision("Eval tool plan step was invalid.")
        tool_name = str(step.get("tool_name") or "")
        tool_input = step.get("tool_input") if isinstance(step.get("tool_input"), dict) else {}
        return _tool_call_decision(
            tool_name=tool_name,
            tool_input=_resolve_placeholders(tool_input, observations, self.case),
            message=str(step.get("message") or f"调用 {tool_name}。"),
        )

    def _answer(self, payload: dict[str, Any]) -> str:
        expected_answer = self.case.get("scripted_answer")
        if isinstance(expected_answer, str) and expected_answer.strip():
            return expected_answer

        observations = payload.get("observations")
        observations = observations if isinstance(observations, list) else []
        loaded_messages = _loaded_messages(observations)
        if loaded_messages:
            facts = []
            for message in loaded_messages:
                subject = message.get("subject") or "无主题"
                body = " ".join(str(message.get("body_text") or "").split())
                facts.append(f"{subject}: {body[:260]}")
            return "根据本地邮件证据：" + "；".join(facts)
        if _has_tool_observation(observations, "matter.create"):
            return "已根据邮件证据创建本地事务。"
        return "当前上下文足够回答该问题。"

    def _search_query(self, payload: dict[str, Any]) -> str:
        query = self._case_search_query()
        if query:
            return query
        user_input = payload.get("user_input")
        return str(user_input or "")

    def _case_search_query(self) -> str:
        query = self.case.get("search_query")
        return str(query) if isinstance(query, str) else ""


def _stage_from_prompt(prompt_summary: str) -> str:
    if prompt_summary.startswith("agent_turn_route"):
        return "route"
    if prompt_summary.startswith("agent_turn_decision_repair"):
        return "decision_repair"
    if prompt_summary.startswith("agent_turn_decision"):
        return "decision"
    if prompt_summary.startswith("tool_result_check"):
        return "tool_result_check"
    if prompt_summary.startswith("agent_turn_answer"):
        return "answer"
    if prompt_summary.startswith("agent_turn_context_answer"):
        return "context_answer"
    if prompt_summary.startswith("agent_turn_context_summarize"):
        return "context_summarize"
    return prompt_summary.split()[0] if prompt_summary else "unknown"


def _json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _tool_call_decision(*, tool_name: str, tool_input: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "operation": {
            "type": "tool_call",
            "package_name": tool_name.split(".", 1)[0],
            "tool_name": tool_name,
            "tool_input": tool_input,
            "final_answer": None,
            "reason": f"Call {tool_name}.",
            "confidence": "high",
        },
        "assistant_message": message,
    }


def _final_answer_decision(reason: str) -> dict[str, Any]:
    return {
        "operation": {
            "type": "final_answer",
            "package_name": None,
            "tool_name": None,
            "tool_input": {},
            "final_answer": None,
            "reason": reason,
            "confidence": "high",
        },
        "assistant_message": "证据已足够，进入最终回答。",
    }


def _has_tool_observation(observations: list[dict[str, Any]], tool_name: str) -> bool:
    return any(observation.get("tool_name") == tool_name for observation in observations)


def _has_expand_observation(observations: list[dict[str, Any]], package_name: str) -> bool:
    return any(
        observation.get("action") == "expand_package"
        and observation.get("package_name") == package_name
        for observation in observations
    )


def _has_cached_loaded_mail(observations: list[dict[str, Any]]) -> bool:
    return any(
        observation.get("tool_name") == "mail.load_messages"
        and isinstance(observation.get("_cache"), dict)
        for observation in observations
    )


def _message_ids_from_search(observations: list[dict[str, Any]]) -> list[str]:
    for observation in reversed(observations):
        if observation.get("tool_name") != "mail.search":
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
        return [
            str(message.get("message_id"))
            for message in messages
            if isinstance(message, dict) and message.get("message_id")
        ]
    return []


def _loaded_messages(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = []
    for observation in observations:
        if observation.get("tool_name") != "mail.load_messages":
            continue
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        output = result.get("output")
        if not isinstance(output, dict):
            continue
        messages = output.get("messages")
        if isinstance(messages, list):
            loaded.extend(message for message in messages if isinstance(message, dict))
    return loaded


def _source_links_from_loaded_mail(observations: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "source_type": "mail_message",
            "source_id": str(message.get("message_id")),
            "reason": "Loaded by eval mail evidence.",
        }
        for message in _loaded_messages(observations)
        if message.get("message_id")
    ]


def _expanded_package_names_from_case(case: dict[str, Any]) -> set[str]:
    value = case.get("expanded_package_names")
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value}


def _resolve_placeholders(value: Any, observations: list[dict[str, Any]], case: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_placeholders(item, observations, case)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_placeholders(item, observations, case) for item in value]
    if value == "$matter_id_from_create":
        matter_id = _matter_id_from_create(observations)
        return matter_id or ""
    if isinstance(value, str) and value == "$workspace_root":
        filesystem = _fixture_filesystem(case)
        return str(filesystem.get("workspace_root") or "")
    if isinstance(value, str) and value.startswith("$file:"):
        relative_path = value.split(":", 1)[1]
        filesystem = _fixture_filesystem(case)
        files = filesystem.get("files")
        item = files.get(relative_path) if isinstance(files, dict) else None
        return str(item.get("path") or "") if isinstance(item, dict) else ""
    if value == "$sha256_from_read_file":
        sha256 = _sha256_from_read_file(observations)
        return sha256 or ""
    if value == "$bash_session_id_from_last_run":
        session_id = _bash_session_id_from_last_run(observations)
        return session_id or ""
    if value == "$bash_next_offset_from_last_read":
        offset = _bash_next_offset_from_last_read(observations)
        return offset if offset is not None else 0
    return value


def _matter_id_from_create(observations: list[dict[str, Any]]) -> str | None:
    for observation in reversed(observations):
        if observation.get("tool_name") != "matter.create":
            continue
        result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        matter = output.get("matter") if isinstance(output, dict) else None
        if isinstance(matter, dict) and matter.get("matter_id"):
            return str(matter["matter_id"])
    return None


def _fixture_filesystem(case: dict[str, Any]) -> dict[str, Any]:
    fixture_index = case.get("_eval_fixture_index")
    if not isinstance(fixture_index, dict):
        return {}
    filesystem = fixture_index.get("filesystem")
    return filesystem if isinstance(filesystem, dict) else {}


def _sha256_from_read_file(observations: list[dict[str, Any]]) -> str | None:
    for observation in reversed(observations):
        if observation.get("tool_name") != "filesystem.read_file":
            continue
        result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        if isinstance(output, dict) and isinstance(output.get("sha256"), str):
            return output["sha256"]
    return None


def _bash_session_id_from_last_run(observations: list[dict[str, Any]]) -> str | None:
    for observation in reversed(observations):
        if observation.get("tool_name") != "bash.run":
            continue
        result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        if isinstance(output, dict) and isinstance(output.get("session_id"), str):
            return output["session_id"]
    return None


def _bash_next_offset_from_last_read(observations: list[dict[str, Any]]) -> int | None:
    for observation in reversed(observations):
        if observation.get("tool_name") != "bash.read_session":
            continue
        result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        if isinstance(output, dict) and isinstance(output.get("next_offset"), int):
            return int(output["next_offset"])
    return None
