"""Deterministic synthetic LLM fixture for old/new normal-use comparison.

Only fixed synthetic prompts/data are accepted. It deliberately raises on an
unknown stage so a fixture gap is visible instead of being hidden as a fake answer.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from typing import Any


class UnsupportedFixtureStage(RuntimeError):
    pass


class SyntheticNormalUseClient:
    """Sync `complete_text` adapter compatible with foreground and worker wrappers."""

    def __init__(self, *, foreground_delay: float = 0.45, background_delay: float = 0.1):
        if not 0 <= foreground_delay <= 2 or not 0 <= background_delay <= 2:
            raise ValueError("fixture delays must be between 0 and 2 seconds")
        self.foreground_delay = foreground_delay
        self.background_delay = background_delay
        self.stage_counts: Counter[str] = Counter()
        self.request_log: list[dict[str, Any]] = []
        self.manager_task_mode = False
        self._manager_decisions = 0

    @staticmethod
    def _response(content: str, request=None):
        from app.core.llm.models import LLMResponse

        request = request or {}
        return LLMResponse(
            provider="synthetic_normal_use", client_name="synthetic", model="fixture-v1",
            status="completed", content=content, partial=False, finish_reason="stop",
            response_mode=request.get("response_mode", "text"),
            prompt_summary=request.get("prompt_summary", "synthetic fixture"),
            metadata={}, usage={"prompt_tokens": 80, "completion_tokens": 35, "total_tokens": 115},
        )

    @staticmethod
    def _passage_by_role(payload: dict, role: str) -> dict:
        candidates = [row for row in payload.get("messages", []) if row.get("role") == role]
        if not candidates:
            raise ValueError(f"fixture prompt lacks a {role} source passage")
        row = max(candidates, key=lambda item: item.get("seq", 0))
        passages = row.get("passages") or []
        if not passages or not passages[0].get("passage_id"):
            raise ValueError(f"fixture prompt lacks a {role} passage ID")
        return {"passage_id": passages[0]["passage_id"]}

    def _memory_organize(self, prompt: str) -> str:
        payload = json.loads(prompt)
        current_id = payload["current_user_message_id"]
        current = next((row for row in payload["messages"]
                        if row.get("message_id") == current_id and row.get("role") == "user"), None)
        if current is None:
            raise ValueError("fixture current user message is absent")
        claim = current["content"].strip()
        if "希望" not in claim:
            return json.dumps({"candidates": []}, ensure_ascii=False)
        existing = next((row for row in payload.get("existing_memories", [])
                         if row.get("scope") == "global" and row.get("content", "").casefold() == claim.casefold()), None)
        item = {
            "claim": claim,
            "kind": "preference",
            "scope": "global",
            "durable": True,
            "grounding": "direct",
            "confidence": 0.96,
            "sources": [{"message_id": current_id, "quote": current["content"]}],
            "relation": "equivalent" if existing else "new",
            "target_memory_id": existing["memory_id"] if existing else None,
            "expires_at": None,
        }
        return json.dumps({"candidates": [item]}, ensure_ascii=False)

    def _intent_delta(self, prompt: str) -> str:
        payload = json.loads(prompt)
        user_source = self._passage_by_role(payload, "user")
        agent_source = self._passage_by_role(payload, "agent")
        existing = {item["kind"]: item for item in payload.get("existing_records", [])
                    if item.get("kind") in {"intent", "dialogue"}}
        user_rows = [row for row in payload.get("messages", []) if row.get("role") == "user"]
        latest_user_text = " ".join(p.get("content", "") for p in user_rows[-1].get("passages", []))
        intent_content = (
            "用户在整理技术答复方式，倾向先看结论，再看关键依据。"
            if "结论" in latest_user_text or "依据" in latest_user_text
            else "用户在讨论技术答复，希望说明清晰并保留必要推理。"
        )
        values = [
            ("intent", intent_content, "user_statement", user_source, 3),
            ("dialogue", "本轮已答复，当前没有未回应的追问。", "assistant_report", agent_source, 2),
        ]
        changes = []
        for kind, content, evidence, source, priority in values:
            old = existing.get(kind)
            change = {
                "op": "update" if old else "add",
                "reason": "更新本轮对话方向与回应状态。",
                "value": {
                    "kind": kind, "subject": kind, "topic": "session", "content": content,
                    "evidence": evidence, "sources": [source], "priority": priority,
                    "validity": "current", "related_ids": [],
                },
            }
            if old:
                change["record_id"] = old["record_id"]
                change["expected_revision"] = old["revision"]
            changes.append(change)
        return json.dumps({"changes": changes}, ensure_ascii=False)

    @staticmethod
    def _consolidate(prompt: str) -> str:
        payload = json.loads(prompt)
        memories = payload.get("memories", [])
        if len(memories) < 2:
            return json.dumps({"merges": []}, ensure_ascii=False)
        selected = memories[:2]
        first, second = selected
        content = (
            "回答技术问题时先给结论，再用简洁的关键依据说明，避免冗长背景。"
        )
        merge = {
            "target_memory_id": first["memory_id"],
            "memory_ids": [first["memory_id"], second["memory_id"]],
            "content": content,
            "confidence": 0.96,
            "evidence": [
                {"memory_id": first["memory_id"], "quote": first["content"]},
                {"memory_id": second["memory_id"], "quote": second["content"]},
            ],
        }
        return json.dumps({"merges": [merge]}, ensure_ascii=False)

    def complete_text(self, **request):
        metadata = request.get("metadata") or {}
        stage = metadata.get("stage") or request.get("prompt_summary")
        if not isinstance(stage, str) or not stage:
            raise UnsupportedFixtureStage("request has no stage or prompt_summary")
        self.stage_counts[stage] += 1
        self.request_log.append({"stage": stage, "prompt_summary": request.get("prompt_summary")})
        if stage in {"decision", "agent_decision"}:
            delay = self.foreground_delay
            if self.manager_task_mode and self._manager_decisions == 0:
                operation = {"type": "expand_package", "package_name": "management"}
            elif self.manager_task_mode and self._manager_decisions == 1:
                operation = {"type": "tool_call", "tool_name": "management.start_task", "tool_input": {
                    "command_id": "synthetic-benchmark-task", "purpose": "Return a short result for the isolated workload benchmark.",
                    "title": "Synthetic benchmark task",
                }}
            elif self.manager_task_mode and self._manager_decisions == 2:
                operation = {"type": "final_answer", "reason": "The synthetic task was durably queued."}
            else:
                operation = {"type": "final_answer", "reason": "可直接回答这条合成的常规偏好问题。"}
            if self.manager_task_mode:
                self._manager_decisions += 1
            result = json.dumps({"operation": operation}, ensure_ascii=False)
        elif stage == "route":
            delay = self.foreground_delay
            result = json.dumps({
                "selected_package": None,
                "reason": "本轮可依据用户的合成偏好直接答复，无需调用工具。",
            }, ensure_ascii=False)
        elif stage in {"answer", "context_answer"}:
            delay = self.foreground_delay
            result = "技术问题先给结论，再说明关键依据；细节按需展开。"
        elif stage == "answer_review":
            delay = self.foreground_delay
            result = json.dumps({
                "faithful": True, "hallucination_free": True, "request_aligned": True,
                "decision": "pass", "issues": [], "revision_instructions": "", "evidence_gap": "",
            }, ensure_ascii=False)
        elif stage == "background_memory_organize":
            delay = self.background_delay
            result = self._memory_organize(request["user_prompt"])
        elif stage in {"background_conversation_intent", "background_conversation_intent_review"}:
            delay = self.background_delay
            result = self._intent_delta(request["user_prompt"])
        elif stage == "background_memory_consolidate":
            delay = self.background_delay
            result = self._consolidate(request["user_prompt"])
        else:
            raise UnsupportedFixtureStage(f"unsupported synthetic fixture stage: {stage}")
        if delay:
            time.sleep(delay)
        return self._response(result, request)

    def enable_manager_task(self):
        self.manager_task_mode = True
        self._manager_decisions = 0

    def disable_manager_task(self):
        self.manager_task_mode = False

    async def acomplete_text(self, **request):
        """Async adapter that keeps synthetic model latency off the event loop."""
        return await asyncio.to_thread(self.complete_text, **request)


def install_fixture(runtime, *, foreground_delay: float = 0.45, background_delay: float = 0.1):
    """Install on both foreground and memory coordinator clients; return the fixture."""
    client = SyntheticNormalUseClient(
        foreground_delay=foreground_delay, background_delay=background_delay,
    )
    runtime.agent_turn_loop.llm_client = client
    runtime.memory_background.llm_client = client
    runtime.agent_llm_client = client
    return client
