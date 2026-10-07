"""Platform-neutral capture v2 and source adapter contracts.

Provider IDs must be unique within (platform, account_id). Adapters whose native
IDs are chat-scoped must namespace messages AND reply anchors with the chat ID.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CaptureBackpressure(RuntimeError):
    """Provider-neutral signal to stop capture until durable storage has room."""


class ConversationRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    platform: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=256)
    conversation_type: Literal["private", "group", "channel"]
    conversation_id: str = Field(min_length=1, max_length=512)

    @property
    def key(self) -> str:
        raw = json.dumps(
            self.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return "message_conversation_" + hashlib.sha256(raw.encode()).hexdigest()


class CapturePolicy(ConversationRef):
    record_enabled: bool = False
    media_enabled: bool = False
    revision: int = Field(ge=1)
    capture_epoch: int = Field(ge=1)
    minimum_import_version: Literal[1, 2] = 1

    @property
    def conversation(self) -> ConversationRef:
        return ConversationRef(
            **{name: getattr(self, name) for name in ConversationRef.model_fields}
        )

    @property
    def key(self) -> str:
        return self.conversation.key


class Mention(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["user", "all"]
    user_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def valid_target(self):
        if (self.kind == "user") != (self.user_id is not None):
            raise ValueError("invalid_mention_target")
        return self


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["text", "mention", "reply", "unsupported"]
    text: str | None = Field(default=None, max_length=16384)
    mention: Mention | None = None
    message_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def valid_payload(self):
        expected = {"text": "text", "mention": "mention", "reply": "message_id"}.get(self.kind)
        present = {
            name for name in ("text", "mention", "message_id") if getattr(self, name) is not None
        }
        if present != ({expected} if expected else set()):
            raise ValueError("invalid_content_part")
        return self


class MetadataCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mentions: Literal["supported", "not_provided", "unknown"] = "unknown"
    reply: Literal["supported", "not_provided", "unknown"] = "unknown"
    thread: Literal["supported", "not_provided", "unknown"] = "unknown"
    content_parts: Literal["supported", "not_provided", "unknown"] = "unknown"


class AttachmentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ordinal: int = Field(ge=0, le=1023)
    kind: Literal["image", "video"]
    file_name: str | None = Field(default=None, max_length=512)


class MessageEnvelope(ConversationRef):
    message_id: str = Field(min_length=1, max_length=512)
    sender_id: str | None = Field(default=None, max_length=512)
    sender_name: str | None = Field(default=None, max_length=512)
    text: str = Field(default="", max_length=16384)
    sent_at: int | None = Field(default=None, ge=1, le=253402300799)
    received_at: int = Field(ge=0, le=253402300799)
    content_kind: Literal["text", "unsupported"] = "text"
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)
    capture_epoch: int = Field(ge=1)
    adapter_id: str = Field(min_length=1, max_length=120)
    adapter_version: str = Field(min_length=1, max_length=80)
    metadata_capabilities: MetadataCapabilities
    mentions: list[Mention] = Field(default_factory=list, max_length=100)
    reply_to_message_id: str | None = Field(default=None, max_length=512)
    thread_id: str | None = Field(default=None, max_length=512)
    content_parts: list[ContentPart] = Field(default_factory=list, max_length=100)

    @property
    def conversation(self) -> ConversationRef:
        return ConversationRef(
            **{name: getattr(self, name) for name in ConversationRef.model_fields}
        )

    @property
    def key(self) -> str:
        return self.conversation.key

    @property
    def internal_id(self) -> str:
        raw = json.dumps(
            {
                "platform": self.platform,
                "account_id": self.account_id,
                "message_id": self.message_id,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "message_" + hashlib.sha256(raw.encode()).hexdigest()

    @model_validator(mode="after")
    def valid_metadata(self):
        for field, capability in (
            ("mentions", "mentions"),
            ("reply_to_message_id", "reply"),
            ("thread_id", "thread"),
            ("content_parts", "content_parts"),
        ):
            if (
                getattr(self, field)
                and getattr(self.metadata_capabilities, capability) != "supported"
            ):
                raise ValueError("unsupported_native_metadata")
        if len({ref.ordinal for ref in self.attachments}) != len(self.attachments):
            raise ValueError("duplicate_attachment_ordinal")
        if len(self.model_dump_json().encode()) > 131072:
            raise ValueError("message_metadata_too_large")
        return self


@dataclass(frozen=True)
class SourceEvent:
    conversation: ConversationRef
    payload: Any  # Provider-private object, never persisted or returned by MCP.
    received_at: int


@dataclass(frozen=True)
class DownloadedMedia:
    mime_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


class MessageAdapter(Protocol):
    """Future QQ migration implements this interface without changing MCP tools.

    Account authentication is performed outside model-visible tools. capture()
    awaits the consumer so durable-queue backpressure can propagate to providers.
    No send/mark-read/contact mutation method is part of this read-only contract.
    """

    platform: str
    account_id: str

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def capture(self, consume: Callable[[SourceEvent], Awaitable[None]]) -> None: ...
    def normalize(self, event: SourceEvent, policy: CapturePolicy) -> MessageEnvelope: ...
    def backfill(self, conversation: ConversationRef, limit: int) -> AsyncIterator[SourceEvent]: ...
    async def download_attachment(
        self, message: MessageEnvelope, ordinal: int, target: Path, max_bytes: int
    ) -> DownloadedMedia: ...
