"""Durable local storage for Agent run control state and large execution artifacts."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.storage.db import connect, init_db


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_load(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("Stored Agent payload must be a JSON object.")
    return parsed


class SqliteAgentRunStore:
    """Project-owned durable store; graph checkpoints retain only references to artifacts."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        init_db(db_path)

    def save_run(self, record: dict[str, Any], *, updated_at: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_runs(
                    run_id, session_id, status, record_payload, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    status = excluded.status,
                    record_payload = excluded.record_payload,
                    updated_at = excluded.updated_at
                """,
                (
                    record["run_id"],
                    record["session_id"],
                    record["status"],
                    _json_dump(record),
                    record["created_at"],
                    updated_at,
                ),
            )

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT record_payload FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _json_load(row["record_payload"]) if row is not None else None

    def save_event(self, event: dict[str, Any]) -> None:
        self.save_events([event])

    def save_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO agent_run_events(run_id, sequence, event_payload, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, sequence) DO UPDATE SET event_payload = excluded.event_payload
                """,
                [
                    (
                        event["run_id"],
                        event["sequence"],
                        _json_dump(event),
                        event["created_at"],
                    )
                    for event in events
                ],
            )

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT event_payload FROM agent_run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [_json_load(row["event_payload"]) for row in rows]

    def save_review(self, review: dict[str, Any], *, updated_at: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_safety_reviews(
                    review_id, run_id, status, review_payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                    status = excluded.status,
                    review_payload = excluded.review_payload,
                    updated_at = excluded.updated_at
                """,
                (
                    review["review_id"],
                    review["run_id"],
                    review["status"],
                    _json_dump(review),
                    review["created_at"],
                    updated_at,
                ),
            )

    def load_review(self, review_id: str) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT review_payload FROM agent_safety_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        return _json_load(row["review_payload"]) if row is not None else None

    def list_reviews(self, run_id: str) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                (
                    "SELECT review_payload FROM agent_safety_reviews "
                    "WHERE run_id = ? ORDER BY created_at"
                ),
                (run_id,),
            ).fetchall()
        return [_json_load(row["review_payload"]) for row in rows]

    def put_artifact(
        self,
        *,
        artifact_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        summary: str,
        created_at: str,
    ) -> dict[str, Any]:
        serialized = _json_dump(payload)
        content_hash = sha256(serialized.encode("utf-8")).hexdigest()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_run_artifacts(
                    artifact_id, run_id, kind, content_hash, summary, payload,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    summary = excluded.summary,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact_id,
                    run_id,
                    kind,
                    content_hash,
                    summary,
                    serialized,
                    created_at,
                    created_at,
                ),
            )
        return {
            "artifact_id": artifact_id,
            "kind": kind,
            "content_hash": content_hash,
            "summary": summary,
        }

    def load_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM agent_run_artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return _json_load(row["payload"]) if row is not None else None

    def claim_tool_invocation(
        self,
        *,
        invocation_id: str,
        run_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        claimed_at: str,
    ) -> dict[str, Any]:
        """Claim one tool effect, or return its completed/uncertain prior state."""

        input_hash = sha256(_json_dump(tool_input).encode("utf-8")).hexdigest()
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT tool_name, input_hash, status, result_payload
                FROM agent_tool_invocations WHERE invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO agent_tool_invocations(
                        invocation_id, run_id, tool_name, input_hash, status, claimed_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'executing', ?, ?)
                    """,
                    (invocation_id, run_id, tool_name, input_hash, claimed_at, claimed_at),
                )
                return {"status": "claimed"}
        if row["tool_name"] != tool_name or row["input_hash"] != input_hash:
            return {"status": "conflict"}
        if row["status"] == "completed" and row["result_payload"] is not None:
            return {"status": "completed", "result": _json_load(row["result_payload"])}
        return {"status": "uncertain"}

    def complete_tool_invocation(
        self,
        *,
        invocation_id: str,
        result: dict[str, Any],
        completed_at: str,
    ) -> None:
        with connect(self.db_path) as conn:
            updated = conn.execute(
                """
                UPDATE agent_tool_invocations
                SET status = 'completed', result_payload = ?, updated_at = ?
                WHERE invocation_id = ? AND status = 'executing'
                """,
                (_json_dump(result), completed_at, invocation_id),
            )
        if updated.rowcount != 1:
            raise RuntimeError(f"Tool invocation is not claimable for completion: {invocation_id}")
