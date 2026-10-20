"""Shared LLM request, response, and stream event models."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

IMAGE_INPUT_TOKEN_RESERVATION = 32768


class LLMResponseMode(str, Enum):
    TEXT = "text"
    JSON = "json"
    STREAM = "stream"


class LLMReasoningEffort(str, Enum):
    """Provider-neutral reasoning effort hint for models that support it."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LLMTextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class LLMImageURL(BaseModel):
    url: str = Field(repr=False)
    detail: Literal["auto", "low", "high"] = "high"


class LLMImageBlock(BaseModel):
    type: Literal["image_url"] = "image_url"
    image_url: LLMImageURL


class LLMAudioInput(BaseModel):
    data: str = Field(repr=False, min_length=1)
    format: Literal["wav", "mp3"]


class LLMAudioBlock(BaseModel):
    type: Literal["input_audio"] = "input_audio"
    input_audio: LLMAudioInput
    duration_ms: int = Field(gt=0, exclude=True)

    @property
    def token_reservation(self) -> int:
        # Conservative audio reservation; encoded bytes are never text tokens.
        return ((self.duration_ms + 999) // 1000) * 40 + 1024


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[LLMTextBlock | LLMImageBlock | LLMAudioBlock]

    @model_validator(mode="after")
    def image_role(self):
        if self.role != "user" and self.has_images:
            raise ValueError("Image content is supported only in user messages.")
        if self.role != "user" and self.has_audio:
            raise ValueError("Audio content is supported only in user messages.")
        return self

    @property
    def has_images(self) -> bool:
        return isinstance(self.content, list) and any(isinstance(block, LLMImageBlock) for block in self.content)

    @property
    def has_audio(self) -> bool:
        return isinstance(self.content, list) and any(isinstance(block, LLMAudioBlock) for block in self.content)


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
    reasoning_effort: LLMReasoningEffort | None = None
    thinking_enabled: bool | None = None
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
