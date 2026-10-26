"""Versioned, source-grounded conversation state; not user guidance or memory."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


class SourceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seq: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=4000)


class SourcePassageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    passage_id: str = Field(pattern=r"^p_[0-9]+_[0-9]+_[0-9]+_[0-9a-f]{16}$", max_length=150)


class ContextRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["fact", "preference", "task", "decision", "progress", "correction", "episode", "focus", "phase", "intent", "dialogue"]
    subject: str = Field(min_length=1, max_length=200)
    topic: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2000)
    evidence: Literal["user_statement", "assistant_report", "inference"]
    sources: list[SourceQuote | SourcePassageRef] = Field(min_length=1, max_length=12)
    task_status: Literal["proposed", "active", "answered", "completed", "cancelled", "unknown"] | None = None
    validity: Literal["current", "superseded", "archived", "disputed"] = "current"
    event_at: str | None = Field(default=None, max_length=100)
    review_at: str | None = Field(default=None, max_length=100)
    priority: int = Field(default=2, ge=0, le=3)
    related_ids: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def task_state(self):
        if (self.kind == "task") != (self.task_status is not None):
            raise ValueError("Only tasks require task_status")
        return self


class ContextChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op: Literal["add", "update", "archive", "merge"]
    record_id: str | None = Field(default=None, max_length=100)
    expected_revision: int | None = Field(default=None, ge=1)
    value: ContextRecordInput | None = None
    merge_ids: list[str] = Field(default_factory=list, max_length=16)
    merge_revisions: list[int] = Field(default_factory=list, max_length=16)
    reason: str = Field(min_length=1, max_length=500)
    archive_sources: list[SourceQuote | SourcePassageRef] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def operation(self):
        if self.op == "add":
            if self.record_id is not None or self.expected_revision is not None or self.value is None:
                raise ValueError("add requires value and no target")
        elif not self.record_id or self.expected_revision is None:
            raise ValueError("Existing target and revision required")
        if self.op != "archive" and self.value is None:
            raise ValueError("Replacement value required")
        if self.op == "archive" and self.value is not None:
            raise ValueError("archive preserves content; use update to correct")
        if self.op == "archive" and not self.archive_sources:
            raise ValueError("Archive needs new source evidence")
        if self.op != "archive" and self.archive_sources:
            raise ValueError("archive_sources only valid for archive")
        if self.op == "merge":
            if not self.merge_ids or len(self.merge_ids) != len(self.merge_revisions):
                raise ValueError("merge requires source revisions")
        elif self.merge_ids or self.merge_revisions:
            raise ValueError("merge sources only valid for merge")
        return self


class ContextDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    changes: list[ContextChange] = Field(default_factory=list, max_length=128)


class ProposedContextRecordInput(ContextRecordInput):
    """New model protocol; legacy exact quotes remain accepted by the validator."""

    sources: list[SourcePassageRef] = Field(min_length=1, max_length=12)
    kind: Literal["fact", "preference", "task", "decision", "progress", "correction", "episode", "focus", "phase"]


class ProposedContextChange(ContextChange):
    value: ProposedContextRecordInput | None = None
    archive_sources: list[SourcePassageRef] = Field(default_factory=list, max_length=12)


class ProposedContextDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    changes: list[ProposedContextChange] = Field(default_factory=list, max_length=128)


class HandoffRecordInput(ProposedContextRecordInput):
    """Small session-local state, not another store of durable user facts."""

    kind: Literal["focus", "phase", "task", "decision", "progress", "correction"]
    content: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def singleton_identity(self):
        if self.kind in {"focus", "phase"}:
            if self.subject != self.kind or self.topic != "session" or self.validity != "current":
                raise ValueError("focus/phase use subject=kind, topic=session and validity=current")
            if self.kind == "focus" and self.evidence != "user_statement":
                raise PydanticCustomError("handoff_focus_authority", "Current focus must be grounded in the user's request")
        return self


class HandoffChange(ProposedContextChange):
    value: HandoffRecordInput | None = None


class HandoffDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    changes: list[HandoffChange] = Field(default_factory=list, max_length=32)


class IntentRecordInput(ProposedContextRecordInput):
    """Conversation direction and response state, independent of task execution."""

    kind: Literal["intent", "dialogue"]
    content: str = Field(min_length=1, max_length=240)
    evidence: Literal["user_statement", "assistant_report"]
    task_status: None = None
    event_at: None = None
    review_at: None = None
    related_ids: list[str] = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def conversation_slot(self):
        if self.subject != self.kind or self.topic != "session" or self.validity != "current":
            raise ValueError("Intent slots use subject=kind, topic=session, validity=current")
        if self.kind == "intent" and self.evidence != "user_statement":
            raise ValueError("Conversation direction requires user evidence")
        if self.kind == "dialogue" and len(self.content) > 160:
            raise ValueError("Response state is limited to 160 characters")
        if self.event_at or self.review_at or self.related_ids:
            raise ValueError("Intent slots do not store schedules or task relationships")
        return self


class IntentChange(ProposedContextChange):
    op: Literal["add", "update"]
    value: IntentRecordInput


class IntentDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    changes: list[IntentChange] = Field(default_factory=list, max_length=2)


class CitationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    change_index: int = Field(ge=0, le=31)
    sources: list[SourcePassageRef] | None = Field(default=None, min_length=1, max_length=12)
    archive_sources: list[SourcePassageRef] | None = Field(default=None, min_length=1, max_length=12)

    @model_validator(mode="after")
    def exactly_one_source_field(self):
        if (self.sources is None) == (self.archive_sources is None):
            raise ValueError("Choose sources or archive_sources")
        return self


class CitationRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repairs: list[CitationPatch] = Field(min_length=1, max_length=32)
