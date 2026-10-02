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


def validate_child_plan_ownership(parent_record: dict[str, Any], child_plan_id: str) -> None:
    """Validate plan ownership, allowing only a persisted active nested Plan.

    A coordinator child keeps the plan ID of the outer step that owns it. Its
    own nested Plan is persisted in metadata and may create children under that
    distinct plan ID. Arbitrary plan-ID changes are rejected.
    """
    parent_plan_id = parent_record.get("plan_id")
    metadata = parent_record.get("metadata", {})
    raw_plan = metadata.get("multi_agent_plan") if isinstance(metadata, dict) else None
    if parent_plan_id is None and not isinstance(raw_plan, dict):
        # Compatibility for low-level run-manager users without a persisted Plan.
        return
    if parent_plan_id == child_plan_id and not isinstance(raw_plan, dict):
        return
    if not isinstance(raw_plan, dict):
        raise ValueError(  # noqa: TRY004 - persisted ownership state, not an argument type.
            "Parent run is already linked to a different plan."
        )
    try:
        from app.core.multi_agent import Plan, PlanStatus

        plan = Plan.model_validate(raw_plan)
    except (ImportError, ValueError, TypeError):
        raise ValueError("Parent run is already linked to a different plan.") from None
    if (
        plan.parent_run_id != parent_record.get("run_id")
        or plan.plan_id != child_plan_id
        or plan.status not in {
            PlanStatus.VALIDATED,
            PlanStatus.QUEUED,
            PlanStatus.RUNNING,
            PlanStatus.REPLANNING,
        }
    ):
        raise ValueError("Child plan ID does not match the parent's active nested Plan.")


class SqliteAgentRunStore:
    """Project-owned durable store; graph checkpoints retain only references to artifacts."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        init_db(db_path)
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_user_continuations (
                    command_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_user_continuations_run
                ON agent_user_continuations(run_id, created_at)
                """
            )

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

    def save_child_creation(
        self,
        *,
        parent: dict[str, Any],
        child: dict[str, Any],
        events: list[dict[str, Any]],
        updated_at: str,
    ) -> None:
        """Atomically persist a child, its parent link, and lifecycle creation events."""

        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent_row = conn.execute(
                "SELECT record_payload FROM agent_runs WHERE run_id = ?",
                (parent["run_id"],),
            ).fetchone()
            if parent_row is None:
                raise KeyError(f"Parent Agent run not found: {parent['run_id']}")
            stored_parent = _json_load(parent_row["record_payload"])
            if stored_parent.get("status") in {
                "completed", "failed", "cancelled", "timed_out",
                "waiting_user", "waiting_confirmation",
            }:
                raise ValueError("Parent Agent run cannot create a child in its current state.")
            validate_child_plan_ownership(stored_parent, str(child.get("plan_id") or ""))
            if parent.get("plan_id") != (stored_parent.get("plan_id") or child.get("plan_id")):
                raise ValueError("Parent Agent run ownership changed while a child was being created.")
            stored_child_ids = stored_parent.get("child_run_ids", [])
            passed_child_ids = parent.get("child_run_ids", [])
            if (
                not isinstance(stored_child_ids, list)
                or not isinstance(passed_child_ids, list)
                or passed_child_ids != [*stored_child_ids, child.get("run_id")]
            ):
                raise ValueError("Parent Agent run changed while a child was being created.")
            if child.get("parent_run_id") != parent["run_id"]:
                raise ValueError("Child run does not belong to the persisted parent.")
            if conn.execute(
                "SELECT 1 FROM agent_runs WHERE run_id = ?", (child["run_id"],)
            ).fetchone() is not None:
                raise ValueError("Child run ID already exists.")
            parent_sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM agent_run_events WHERE run_id = ?",
                (parent["run_id"],),
            ).fetchone()[0])
            if events[0].get("sequence") != parent_sequence + 1:
                raise ValueError("Parent event sequence changed while a child was being created.")
            for record in (parent, child):
                conn.execute(
                    """
                    INSERT INTO agent_runs(run_id, session_id, status, record_payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        session_id = excluded.session_id, status = excluded.status,
                        record_payload = excluded.record_payload, updated_at = excluded.updated_at
                    """,
                    (
                        record["run_id"], record["session_id"], record["status"],
                        _json_dump(record), record["created_at"], updated_at,
                    ),
                )
            conn.executemany(
                """
                INSERT INTO agent_run_events(run_id, sequence, event_payload, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, sequence) DO UPDATE SET event_payload = excluded.event_payload
                """,
                [
                    (event["run_id"], event["sequence"], _json_dump(event), event["created_at"])
                    for event in events
                ],
            )

    def save_run_control_batch(
        self,
        *,
        previous: list[dict[str, Any]],
        updated: list[dict[str, Any]],
        events: list[dict[str, Any]],
        updated_at: str,
    ) -> None:
        """Commit cancellation/timeout state and its events as one transaction."""

        expected = {record["run_id"]: record for record in previous}
        if len(expected) != len(previous) or {record["run_id"] for record in updated} != set(expected):
            raise ValueError("Run control batch must update each expected run exactly once.")
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in updated:
                run_id = record["run_id"]
                row = conn.execute(
                    "SELECT record_payload FROM agent_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None or _json_load(row["record_payload"]) != expected[run_id]:
                    raise ValueError(f"Agent run changed during control transition: {run_id}")
            next_sequence: dict[str, int] = {}
            for event in events:
                run_id = event["run_id"]
                if run_id not in expected:
                    raise ValueError("Control event references a run outside the transition.")
                if run_id not in next_sequence:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM agent_run_events WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    next_sequence[run_id] = int(row[0]) + 1
                if event["sequence"] != next_sequence[run_id]:
                    raise ValueError("Agent event sequence changed during control transition.")
                next_sequence[run_id] += 1
            conn.executemany(
                """
                UPDATE agent_runs SET status = ?, record_payload = ?, updated_at = ?
                WHERE run_id = ?
                """,
                [
                    (record["status"], _json_dump(record), updated_at, record["run_id"])
                    for record in updated
                ],
            )
            conn.executemany(
                """
                INSERT INTO agent_run_events(run_id, sequence, event_payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (event["run_id"], event["sequence"], _json_dump(self._event_for_storage(event)), event["created_at"])
                    for event in events
                ],
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
                        _json_dump(self._event_for_storage(event)),
                        event["created_at"],
                    )
                    for event in events
                ],
            )

    def save_waiting_user_question(
        self,
        *,
        record: dict[str, Any],
        event: dict[str, Any],
        updated_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Atomically park a run and publish its user-facing question event."""

        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT record_payload FROM agent_runs WHERE run_id = ?",
                (record["run_id"],),
            ).fetchone()
            if row is None:
                raise KeyError(f"Agent run not found: {record['run_id']}")
            current = _json_load(row["record_payload"])
            metadata = current.get("metadata", {})
            existing_question = metadata.get("pending_user_question") if isinstance(metadata, dict) else None
            requested_question = record.get("metadata", {}).get("pending_user_question")
            if current.get("status") == "waiting_user":
                if (
                    isinstance(existing_question, dict)
                    and isinstance(requested_question, dict)
                    and all(
                        existing_question.get(key) == requested_question.get(key)
                        for key in ("question_id", "patch_id", "question")
                    )
                ):
                    existing_row = conn.execute(
                        """
                        SELECT event_payload FROM agent_run_events
                        WHERE run_id = ? AND json_extract(event_payload, '$.type') = 'multi_agent_user_question'
                          AND json_extract(event_payload, '$.payload.question_id') = ?
                        ORDER BY sequence DESC LIMIT 1
                        """,
                        (record["run_id"], existing_question.get("question_id")),
                    ).fetchone()
                    if existing_row is not None:
                        conn.commit()
                        return current, _json_load(existing_row["event_payload"]), True
                raise ValueError("Agent run is already waiting for a different user question.")
            if current.get("status") != "running":
                raise ValueError("Only a running Agent run can ask the user a question.")
            if metadata.get("cancel_requested") is True or metadata.get("completion_claimed") is True:
                raise ValueError("Agent run cannot wait for a user response in its current state.")

            sequence_row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM agent_run_events WHERE run_id = ?",
                (record["run_id"],),
            ).fetchone()
            event = dict(event)
            event["sequence"] = int(sequence_row["next_sequence"])
            event["event_id"] = f"{record['run_id']}_event_{event['sequence']:06d}"
            record = dict(record)
            record["status"] = "waiting_user"
            conn.execute(
                "UPDATE agent_runs SET status = ?, record_payload = ?, updated_at = ? WHERE run_id = ?",
                ("waiting_user", _json_dump(record), updated_at, record["run_id"]),
            )
            conn.execute(
                "INSERT INTO agent_run_events(run_id, sequence, event_payload, created_at) VALUES (?, ?, ?, ?)",
                (record["run_id"], event["sequence"], _json_dump(event), event["created_at"]),
            )
            conn.commit()
            return record, event, False

    def save_waiting_for_child_user(
        self,
        *,
        parent_record: dict[str, Any],
        child_run_ids: list[str],
        event: dict[str, Any],
        updated_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Atomically park a parent on child questions without adding a question."""

        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT record_payload FROM agent_runs WHERE run_id = ?",
                (parent_record["run_id"],),
            ).fetchone()
            if row is None:
                raise KeyError(f"Agent run not found: {parent_record['run_id']}")
            current = _json_load(row["record_payload"])
            metadata = current.get("metadata", {})
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            if (
                metadata.get("pending_user_question")
                and not metadata.get("pending_user_answer_command_id")
            ):
                raise ValueError("A parent with its own pending question cannot wait on child questions.")
            if current.get("status") not in {"running", "waiting_user"}:
                raise ValueError("Parent Agent run cannot wait on child questions in its current state.")
            if metadata.get("cancel_requested") is True or metadata.get("completion_claimed") is True:
                raise ValueError("Parent Agent run cannot wait on child questions in its current state.")

            existing_ids = metadata.get("waiting_child_user_run_ids", [])
            if not isinstance(existing_ids, list):
                existing_ids = []
            combined_ids = list(dict.fromkeys([*existing_ids, *child_run_ids]))
            for child_run_id in child_run_ids:
                child_row = conn.execute(
                    "SELECT record_payload FROM agent_runs WHERE run_id = ?",
                    (child_run_id,),
                ).fetchone()
                if child_row is None:
                    raise ValueError("A referenced child run does not exist.")
                child = _json_load(child_row["record_payload"])
                if child.get("parent_run_id") != parent_record["run_id"] or child.get("status") not in {
                    "waiting_user", "waiting_confirmation"
                }:
                    raise ValueError("Only a waiting child owned by this parent can be referenced.")

            if (
                current.get("status") == "waiting_user"
                and existing_ids == combined_ids
                and not metadata.get("pending_user_question")
                and not metadata.get("pending_user_answer_command_id")
            ):
                conn.commit()
                return current, None
            metadata["waiting_child_user_run_ids"] = combined_ids
            metadata.pop("pending_user_question", None)
            metadata.pop("pending_user_answer_command_id", None)
            current["metadata"] = metadata
            current["status"] = "waiting_user"
            current["waiting_since"] = updated_at
            sequence_row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM agent_run_events WHERE run_id = ?",
                (parent_record["run_id"],),
            ).fetchone()
            event = dict(event)
            event["sequence"] = int(sequence_row["next_sequence"])
            event["event_id"] = f"{parent_record['run_id']}_event_{event['sequence']:06d}"
            event_payload = dict(event.get("payload") or {})
            event_payload["child_run_ids"] = combined_ids
            event["payload"] = event_payload
            conn.execute(
                "UPDATE agent_runs SET status = ?, record_payload = ?, updated_at = ? WHERE run_id = ?",
                ("waiting_user", _json_dump(current), updated_at, parent_record["run_id"]),
            )
            conn.execute(
                "INSERT INTO agent_run_events(run_id, sequence, event_payload, created_at) VALUES (?, ?, ?, ?)",
                (parent_record["run_id"], event["sequence"], _json_dump(event), event["created_at"]),
            )
            conn.commit()
            return current, event

    def resume_after_child_user(
        self,
        *,
        parent_run_id: str,
        event: dict[str, Any],
        updated_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
        """Resume a child-question parent only after all referenced children unblock."""

        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT record_payload FROM agent_runs WHERE run_id = ?", (parent_run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Agent run not found: {parent_run_id}")
            record = _json_load(row["record_payload"])
            metadata = record.get("metadata", {})
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            child_ids = metadata.get("waiting_child_user_run_ids", [])
            if (
                record.get("status") != "waiting_user"
                or not isinstance(child_ids, list)
                or not child_ids
                or metadata.get("pending_user_question")
                or metadata.get("pending_user_answer_command_id")
                or metadata.get("cancel_requested") is True
                or metadata.get("completion_claimed") is True
            ):
                conn.commit()
                return record, None, False
            for child_run_id in child_ids:
                child_row = conn.execute(
                    "SELECT status, record_payload FROM agent_runs WHERE run_id = ?",
                    (child_run_id,),
                ).fetchone()
                if child_row is None:
                    conn.commit()
                    return record, None, False
                child = _json_load(child_row["record_payload"])
                if child.get("parent_run_id") != parent_run_id or child_row["status"] not in {
                    "completed", "failed", "cancelled", "timed_out"
                }:
                    conn.commit()
                    return record, None, False
            metadata.pop("waiting_child_user_run_ids", None)
            record["metadata"] = metadata
            record["status"] = "running"
            record["waiting_since"] = None
            event = dict(event)
            event["sequence"] = int(conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_run_events WHERE run_id = ?",
                (parent_run_id,),
            ).fetchone()[0])
            event["event_id"] = f"{parent_run_id}_event_{event['sequence']:06d}"
            payload = dict(event.get("payload") or {})
            payload["child_run_ids"] = child_ids
            event["payload"] = payload
            conn.execute(
                "UPDATE agent_runs SET status = ?, record_payload = ?, updated_at = ? WHERE run_id = ?",
                ("running", _json_dump(record), updated_at, parent_run_id),
            )
            conn.execute(
                "INSERT INTO agent_run_events(run_id, sequence, event_payload, created_at) VALUES (?, ?, ?, ?)",
                (parent_run_id, event["sequence"], _json_dump(event), event["created_at"]),
            )
            conn.commit()
            return record, event, True

    def continue_user_question(
        self,
        *,
        run_id: str,
        command_id: str,
        answer: str,
        event: dict[str, Any],
        updated_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool, str]:
        """Atomically journal an answer and resume a WAITING_USER run.

        The answer is only stored in the private continuation journal. Public run
        metadata and the event contain the command/question references only.
        """

        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                "SELECT run_id, question_id, answer FROM agent_user_continuations WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if command is not None:
                if command["run_id"] != run_id or command["answer"] != answer:
                    raise ValueError("command_id was already used with a different continuation.")
                row = conn.execute(
                    "SELECT record_payload FROM agent_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Agent run not found: {run_id}")
                run_record = _json_load(row["record_payload"])
                event_row = conn.execute(
                    """
                    SELECT event_payload FROM agent_run_events
                    WHERE run_id = ? AND json_extract(event_payload, '$.type') = 'multi_agent_user_answer_received'
                      AND json_extract(event_payload, '$.payload.command_id') = ?
                    ORDER BY sequence LIMIT 1
                    """,
                    (run_id, command_id),
                ).fetchone()
                conn.commit()
                return run_record, _json_load(event_row["event_payload"]) if event_row else None, True, command["question_id"]

            row = conn.execute(
                "SELECT record_payload FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Agent run not found: {run_id}")
            record = _json_load(row["record_payload"])
            metadata = record.get("metadata", {})
            question = metadata.get("pending_user_question") if isinstance(metadata, dict) else None
            if record.get("status") != "waiting_user" or not isinstance(question, dict):
                raise ValueError("Agent run is not waiting for a user response.")
            if metadata.get("cancel_requested") is True or metadata.get("completion_claimed") is True:
                raise ValueError("Agent run cannot be continued in its current state.")
            question_id = question.get("question_id")
            if not isinstance(question_id, str) or not question_id:
                raise ValueError("Agent run has no valid pending question.")
            event_payload = dict(event.get("payload") or {})
            event_payload["question_id"] = question_id
            event["payload"] = event_payload
            conn.execute(
                "INSERT INTO agent_user_continuations(command_id, run_id, question_id, answer, created_at) VALUES (?, ?, ?, ?, ?)",
                (command_id, run_id, question_id, answer, updated_at),
            )
            metadata = dict(metadata)
            metadata["pending_user_answer_command_id"] = command_id
            record["metadata"] = metadata
            record["status"] = "running"
            record["waiting_since"] = None
            event = dict(event)
            sequence_row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM agent_run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            event["sequence"] = int(sequence_row["next_sequence"])
            event["event_id"] = f"{run_id}_event_{event['sequence']:06d}"
            conn.execute(
                "UPDATE agent_runs SET status = ?, record_payload = ?, updated_at = ? WHERE run_id = ?",
                ("running", _json_dump(record), updated_at, run_id),
            )
            conn.execute(
                "INSERT INTO agent_run_events(run_id, sequence, event_payload, created_at) VALUES (?, ?, ?, ?)",
                (run_id, event["sequence"], _json_dump(event), event["created_at"]),
            )
            conn.commit()
            return record, event, False, question_id

    def load_user_continuation_answer(self, *, run_id: str, command_id: str) -> str | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT answer FROM agent_user_continuations WHERE run_id = ? AND command_id = ?",
                (run_id, command_id),
            ).fetchone()
        return row["answer"] if row is not None else None

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT event_payload FROM agent_run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [_json_load(row["event_payload"]) for row in rows]

    @staticmethod
    def _event_for_storage(event: dict[str, Any]) -> dict[str, Any]:
        """Store token deltas once, not every growing prefix snapshot.

        Live SSE events retain ``content_snapshot`` for compatibility.  Durable
        readers reconstruct it from ordered deltas when rehydrating a run.
        """

        if event.get("type") != "llm_delta":
            return event
        payload = event.get("payload")
        if not isinstance(payload, dict) or "content_snapshot" not in payload:
            return event
        stored = dict(event)
        stored_payload = dict(payload)
        stored_payload.pop("content_snapshot", None)
        stored_payload["content_snapshot_omitted"] = True
        stored["payload"] = stored_payload
        return stored

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

    def list_pending_reviews(self) -> list[dict[str, Any]]:
        """List actionable manual reviews in global creation order.

        Terminal runs are ignored so abandoned confirmation requests cannot
        block the queue. Run status is stored beside the payload for this join.
        """
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT review.review_payload
                FROM agent_safety_reviews AS review
                JOIN agent_runs AS run ON run.run_id = review.run_id
                WHERE review.status = 'pending'
                  AND json_extract(review.review_payload, '$.mode') = 'manual'
                  AND run.status = 'waiting_confirmation'
                ORDER BY review.created_at, review.review_id
                """
            ).fetchall()
        return [_json_load(row["review_payload"]) for row in rows]

    def decide_pending_review(
        self,
        *,
        review_id: str,
        review: dict[str, Any],
        event: dict[str, Any],
        updated_at: str,
    ) -> tuple[dict[str, Any], bool, str | None, dict[str, Any] | None]:
        """Atomically enforce queue head and persist one decision across processes.

        The returned conflict is ``not_head`` or ``inactive``. A decided review
        is returned unchanged with ``transitioned=False`` for idempotent replay;
        its decision event is also inserted or recovered without duplication.
        """

        def persist_decision_event(conn, *, decided_review: dict[str, Any]) -> dict[str, Any]:
            existing = conn.execute(
                """
                SELECT event_payload FROM agent_run_events
                WHERE run_id = ?
                  AND json_extract(event_payload, '$.type') = 'safety_review_decided'
                  AND json_extract(event_payload, '$.payload.review.review_id') = ?
                ORDER BY sequence LIMIT 1
                """,
                (decided_review["run_id"], review_id),
            ).fetchone()
            if existing is not None:
                return _json_load(existing["event_payload"])

            event_payload = dict(event)
            payload = dict(event_payload.get("payload") or {})
            payload["review"] = decided_review
            event_payload["payload"] = payload
            sequence_row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM agent_run_events WHERE run_id = ?",
                (decided_review["run_id"],),
            ).fetchone()
            sequence = int(sequence_row["sequence"])
            event_payload["sequence"] = sequence
            event_payload["event_id"] = f"{decided_review['run_id']}_event_{sequence:06d}"
            conn.execute(
                """
                INSERT INTO agent_run_events(run_id, sequence, event_payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    decided_review["run_id"],
                    sequence,
                    _json_dump(event_payload),
                    event_payload["created_at"],
                ),
            )
            return event_payload

        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT review_payload, status FROM agent_safety_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            if current_row is None:
                raise KeyError(f"Safety review not found: {review_id}")
            current = _json_load(current_row["review_payload"])
            if current_row["status"] != "pending":
                persisted_event = None
                if current_row["status"] in {"approved", "rejected"}:
                    persisted_event = persist_decision_event(conn, decided_review=current)
                conn.commit()
                return current, False, None, persisted_event

            run_row = conn.execute(
                "SELECT status, record_payload FROM agent_runs WHERE run_id = ?",
                (current["run_id"],),
            ).fetchone()
            if run_row is None or run_row["status"] != "waiting_confirmation":
                conn.commit()
                return current, False, "inactive", None

            head_row = conn.execute(
                """
                SELECT review.review_id
                FROM agent_safety_reviews AS review
                JOIN agent_runs AS run ON run.run_id = review.run_id
                WHERE review.status = 'pending'
                  AND json_extract(review.review_payload, '$.mode') = 'manual'
                  AND run.status = 'waiting_confirmation'
                ORDER BY review.created_at, review.review_id
                LIMIT 1
                """
            ).fetchone()
            head_review_id = head_row["review_id"] if head_row is not None else None
            if head_review_id != review_id:
                conn.commit()
                return current, False, f"not_head:{head_review_id or ''}", None

            conn.execute(
                """
                UPDATE agent_safety_reviews
                SET status = ?, review_payload = ?, updated_at = ?
                WHERE review_id = ? AND status = 'pending'
                """,
                (review["status"], _json_dump(review), updated_at, review_id),
            )
            # A decided review must never be durable while its run still looks
            # paused: a crash in that gap makes an idempotent retry unable to
            # distinguish "decision committed, resume not started" from a
            # review that needs no further action. Persist the resumable run
            # state in the same SQLite transaction as the decision.
            run_payload = _json_load(run_row["record_payload"])
            run_payload["status"] = "running"
            run_payload["waiting_since"] = None
            conn.execute(
                """
                UPDATE agent_runs
                SET status = 'running', record_payload = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (_json_dump(run_payload), updated_at, current["run_id"]),
            )
            persisted_event = persist_decision_event(conn, decided_review=review)
            conn.commit()
            return review, True, None, persisted_event

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

    def load_tool_result_artifact(self, artifact_id: str, run_id: str) -> dict[str, Any] | None:
        """Load a tool result only when both its artifact kind and owning run match."""
        with connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT payload FROM agent_run_artifacts
                   WHERE artifact_id = ? AND run_id = ? AND kind = 'tool_result'""",
                (artifact_id, run_id),
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

    def list_completed_tool_results(
        self, *, run_id: str, tool_names: tuple[str, ...], limit: int = 32
    ) -> list[dict[str, Any]]:
        """Read a bounded set of a run's durable, completed tool results."""
        if not tool_names:
            return []
        placeholders = ", ".join("?" for _ in tool_names)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT tool_name, result_payload FROM agent_tool_invocations "
                f"WHERE run_id = ? AND status = 'completed' AND tool_name IN ({placeholders}) "
                "AND result_payload IS NOT NULL "
                "ORDER BY updated_at DESC, invocation_id DESC LIMIT ?",
                (run_id, *tool_names, min(max(1, limit), 64)),
            ).fetchall()
        return [
            {"tool_name": row["tool_name"], "result": _json_load(row["result_payload"])}
            for row in rows
        ]
