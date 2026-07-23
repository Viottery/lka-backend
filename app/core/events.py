"""Runtime event records used by the debug execution loop."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventRecord(BaseModel):
    event_id: str
    event_type: str
    session_id: str
    context_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "completed"
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
