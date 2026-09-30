"""Agent turn orchestration boundary."""

from __future__ import annotations

from typing import Protocol

from app.core.agent_runs import AgentRunRecord
from app.core.agent_turn import AgentTurnResult
from app.core.llm import LLMResponseMode
from app.core.safety import SafetyReviewMode


class AgentTurnRunner(Protocol):
    """Runtime-facing contract shared by legacy and graph orchestrators."""

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
    ) -> AgentTurnResult: ...

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
    ) -> AgentTurnResult: ...

    def create_run_for_turn(
        self,
        *,
        session_id: str | None,
        user_input: str,
        parent_run_id: str | None = None,
    ) -> AgentRunRecord: ...
