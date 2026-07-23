"""Trace recording for debug runtime runs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.events import EventRecord, utc_now


class TraceRecord(BaseModel):
    trace_id: str
    session_id: str
    context_id: str | None = None
    status: str
    events: list[EventRecord] = Field(default_factory=list)
    verification_clues: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class TraceRecorder:
    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        self._conn_factory = conn_factory

    def record_event(self, event: EventRecord) -> None:
        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT INTO runtime_events(
                    event_id, event_type, session_id, context_id, payload, status, error, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    event_type=excluded.event_type,
                    session_id=excluded.session_id,
                    context_id=excluded.context_id,
                    payload=excluded.payload,
                    status=excluded.status,
                    error=excluded.error,
                    created_at=excluded.created_at
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.session_id,
                    event.context_id,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    event.status,
                    event.error,
                    event.created_at.isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def record_trace(self, trace: TraceRecord) -> None:
        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT INTO traces(
                    trace_id, session_id, context_id, status, events_payload,
                    verification_clues, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    context_id=excluded.context_id,
                    status=excluded.status,
                    events_payload=excluded.events_payload,
                    verification_clues=excluded.verification_clues,
                    created_at=excluded.created_at
                """,
                (
                    trace.trace_id,
                    trace.session_id,
                    trace.context_id,
                    trace.status,
                    json.dumps(
                        [event.model_dump(mode="json") for event in trace.events],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(trace.verification_clues, ensure_ascii=False, sort_keys=True),
                    trace.created_at.isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def persist_debug_run(self, trace: TraceRecord) -> TraceRecord:
        for event in trace.events:
            self.record_event(event)
        self.record_trace(trace)
        return trace
