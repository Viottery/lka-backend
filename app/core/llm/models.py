"""Shared LLM request, response, and stream event models."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class LLMResponseMode(str, Enum):
    TEXT = "text"
    JSON = "json"
    STREAM = "stream"


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class LLMToolDefinition(BaseModel):
    """Provider-neutral function definition for a single LLM request."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    strict: bool = False


class LLMToolCall(BaseModel):
    """A complete provider-native function call."""

    id: str | None = None
    name: str
    arguments: dict[str, Any] | None = None
    raw_arguments: str = ""


class LLMRequest(BaseModel):
    messages: list[LLMMessage]
    prompt_summary: str
    response_mode: LLMResponseMode = LLMResponseMode.TEXT
    client_name: str | None = None
    model: str | None = None
    temperature: float = 0.0
    max_output_tokens: int | None = None
    require_json: bool = False
    tools: list[LLMToolDefinition] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    provider: str
    status: str
    content: str
    prompt_summary: str
    client_name: str | None = None
    model: str | None = None
    response_mode: LLMResponseMode = LLMResponseMode.TEXT
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str | None = None
    provider_request_id: str | None = None
    partial: bool = False
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMStreamEvent(BaseModel):
    event_type: Literal["llm_started", "llm_delta", "llm_completed", "llm_failed"]
    stage: str
    client_name: str
    provider: str
    model: str
    delta: str = ""
    content_snapshot: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
