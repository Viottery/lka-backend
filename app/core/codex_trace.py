"""Private, append-only journal for Codex app-server protocol events.

The journal is deliberately separate from ``agent_run_events``: native Codex
payloads can contain prompts, source code, command output, and other sensitive
workspace data, so they must not flow through the ordinary run-event API.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.db import connect, init_db

DEFAULT_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


class CodexTraceError(RuntimeError):
    """Base error for durable Codex trace operations."""


class CodexTracePayloadError(CodexTraceError, ValueError):
    """Raised when a payload is not JSON-safe or exceeds the configured limit."""


@dataclass(frozen=True)
class CodexTraceEvent:
    child_run_id: str
    sequence: int
    created_at: str
    method: str
    payload: dict[str, Any]
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    request_id: str | None = None


def _validate_json_value(value: Any, *, path: str = "payload") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodexTracePayloadError(f"{path} contains a non-finite number.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CodexTracePayloadError(f"{path} contains a non-string object key.")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise CodexTracePayloadError(
        f"{path} contains a non-JSON value of type {type(value).__name__}."
    )


def _serialize_payload(payload: dict[str, Any], max_payload_bytes: int) -> str:
    if not isinstance(payload, dict):
        raise CodexTracePayloadError("Codex trace payload must be a JSON object.")
    _validate_json_value(payload)
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CodexTracePayloadError("Codex trace payload cannot be serialized as JSON.") from exc
    if len(serialized.encode("utf-8")) > max_payload_bytes:
        raise CodexTracePayloadError(
            f"Codex trace payload exceeds the {max_payload_bytes}-byte limit; event was not stored."
        )
    return serialized


def _request_id_text(request_id: str | int | None) -> str | None:
    if request_id is None:
        return None
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise TypeError("request_id must be a string, integer, or None.")
    text = str(request_id)
    if not text:
        raise ValueError("request_id must not be empty.")
    return text


class CodexTraceJournal:
    """Append and page through private native Codex protocol events.

    The caller chooses the local SQLite database path. Events are committed
    individually under ``BEGIN IMMEDIATE`` so concurrent writers receive a
    single monotonic sequence per Child Run. No payload is silently truncated.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive.")
        self.db_path = Path(db_path)
        self.max_payload_bytes = max_payload_bytes
        init_db(self.db_path)
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_trace_events (
                    child_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    method TEXT NOT NULL,
                    thread_id TEXT,
                    turn_id TEXT,
                    item_id TEXT,
                    request_id TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(child_run_id, sequence)
                )
                """
            )

    def append(
        self,
        child_run_id: str,
        method: str,
        payload: dict[str, Any],
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        request_id: str | int | None = None,
    ) -> CodexTraceEvent:
        """Durably append one complete protocol event and return its sequence."""
        for field, value in (("child_run_id", child_run_id), ("method", method)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string.")
        for field, value in (
            ("thread_id", thread_id),
            ("turn_id", turn_id),
            ("item_id", item_id),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{field} must be a non-empty string or None.")
        request_text = _request_id_text(request_id)
        payload_json = _serialize_payload(payload, self.max_payload_bytes)
        created_at = datetime.now(UTC).isoformat()

        try:
            with connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                    "FROM codex_trace_events WHERE child_run_id = ?",
                    (child_run_id,),
                ).fetchone()
                sequence = int(row["next_sequence"])
                conn.execute(
                    """
                    INSERT INTO codex_trace_events(
                        child_run_id, sequence, created_at, method, thread_id,
                        turn_id, item_id, request_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child_run_id,
                        sequence,
                        created_at,
                        method,
                        thread_id,
                        turn_id,
                        item_id,
                        request_text,
                        payload_json,
                    ),
                )
        except sqlite3.Error as exc:
            raise CodexTraceError("Could not durably append Codex trace event.") from exc

        return CodexTraceEvent(
            child_run_id=child_run_id,
            sequence=sequence,
            created_at=created_at,
            method=method,
            payload=json.loads(payload_json),
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            request_id=request_text,
        )

    def mark_gap(
        self,
        child_run_id: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> CodexTraceEvent:
        """Record a known interval where native event coverage is incomplete."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A gap reason is required.")
        payload: dict[str, Any] = {"reason": reason, "completeness": "incomplete"}
        if details is not None:
            payload["details"] = details
        return self.append(
            child_run_id,
            "journal/completeness_gap",
            payload,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def list(
        self,
        child_run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> list[CodexTraceEvent]:
        """Read one stable sequence-ordered page without exposing run events."""
        if not isinstance(child_run_id, str) or not child_run_id.strip():
            raise ValueError("child_run_id must be a non-empty string.")
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}.")
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT child_run_id, sequence, created_at, method, thread_id,
                       turn_id, item_id, request_id, payload_json
                FROM codex_trace_events
                WHERE child_run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (child_run_id, after_sequence, limit),
            ).fetchall()
        return [
            CodexTraceEvent(
                child_run_id=row["child_run_id"],
                sequence=int(row["sequence"]),
                created_at=row["created_at"],
                method=row["method"],
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                item_id=row["item_id"],
                request_id=row["request_id"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def has_gap(self, child_run_id: str) -> bool:
        """Return whether this run has any explicit completeness-gap marker."""
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM codex_trace_events
                WHERE child_run_id = ? AND method = 'journal/completeness_gap'
                LIMIT 1
                """,
                (child_run_id,),
            ).fetchone()
        return row is not None
