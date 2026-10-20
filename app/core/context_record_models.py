"""Versioned, source-grounded conversation state; not user guidance or memory."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seq: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=4000)


class SourcePassageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    passage_id: str = Field(pattern=r"^p_[0-9]+_[0-9]+_[0-9]+_[0-9a-f]{16}$", max_length=150)


class ContextRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["fact", "preference", "task", "decision", "progress", "correction", "episode"]
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


class ProposedContextChange(ContextChange):
    value: ProposedContextRecordInput | None = None
    archive_sources: list[SourcePassageRef] = Field(default_factory=list, max_length=12)


class ProposedContextDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    changes: list[ProposedContextChange] = Field(default_factory=list, max_length=128)
