"""User-authored feedback about memory decisions.

This module contains storage-safe data contracts only. Feedback examples are
not sources or permissions and are never fed back into memory automatically.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any, Literal

from pydantic import BaseModel

FeedbackOrigin = Literal["user_api", "user_memory_file", "user_message"]
MemoryFeedbackReason = Literal[
    "unspecified", "factual_error", "wording", "outdated", "duplicate",
    "not_worth_remembering", "do_not_store",
]
FeedbackAction = Literal["correct", "discard"]


class MemoryFeedbackInput(BaseModel):
    """Explicit user feedback; absent input means the mutation is automated."""

    origin: FeedbackOrigin = "user_api"
    reason: MemoryFeedbackReason = "unspecified"


class MemoryFeedback(BaseModel):
    feedback_id: str
    action: FeedbackAction
    old_memory_id: str
    old_version: int
    new_memory_id: str | None = None
    new_version: int | None = None
    scope: Literal["global", "project"]
    project_id: str | None = None
    memory_type: str
    reason: MemoryFeedbackReason
    origin: FeedbackOrigin
    created_at: str
    old_content: str | None = None
    new_content: str | None = None
    old_confidence: float | None = None
    extraction_model: str | None = None
    grounding: dict[str, Any] | None = None
    redacted: bool = False


def ensure_feedback_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_feedback(
            feedback_id TEXT PRIMARY KEY,
            action TEXT NOT NULL CHECK(action IN ('correct','discard')),
            old_memory_id TEXT NOT NULL,
            old_version INTEGER NOT NULL,
            new_memory_id TEXT,
            new_version INTEGER,
            scope TEXT NOT NULL CHECK(scope IN ('global','project')),
            project_id TEXT,
            memory_type TEXT NOT NULL,
            reason TEXT NOT NULL,
            origin TEXT NOT NULL,
            created_at TEXT NOT NULL,
            old_content TEXT,
            new_content TEXT,
            old_confidence REAL,
            extraction_model TEXT,
            grounding TEXT,
            redacted INTEGER NOT NULL DEFAULT 0 CHECK(redacted IN (0,1))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_feedback_scope_created "
        "ON memory_feedback(scope,project_id,created_at DESC,feedback_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_feedback_memory "
        "ON memory_feedback(old_memory_id,new_memory_id)"
    )


def record_feedback(
    conn: sqlite3.Connection,
    *,
    feedback: MemoryFeedbackInput,
    action: FeedbackAction,
    old: sqlite3.Row,
    old_version: int,
    new: sqlite3.Row | None,
    created_at: str,
) -> None:
    """Append feedback within the caller's already-open mutation transaction."""
    ensure_feedback_schema(conn)
    if action == "discard" and feedback.reason == "wording":
        raise ValueError("wording feedback requires correcting memory")
    sensitive = old["sensitivity"] == "personal"
    redact = sensitive or feedback.reason == "do_not_store"
    grounding_value = _safe_grounding(old["metadata"])
    conn.execute(
        """INSERT INTO memory_feedback(
            feedback_id,action,old_memory_id,old_version,new_memory_id,new_version,
            scope,project_id,memory_type,reason,origin,created_at,old_content,new_content,
            old_confidence,extraction_model,grounding,redacted
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            uuid.uuid4().hex, action, old["memory_id"], old_version,
            new["memory_id"] if new is not None else None,
            int(new["version"]) if new is not None else None,
            old["scope"], old["project_id"], old["memory_type"], feedback.reason,
            feedback.origin, created_at,
            None if redact else old["content"],
            None if redact or new is None else new["content"],
            old["confidence"], old["extraction_model"],
            None if redact or grounding_value is None else json.dumps(
                grounding_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            int(redact),
        ),
    )
    if feedback.reason == "do_not_store":
        redact_correction_chain(conn, str(old["memory_id"]))


def redact_correction_chain(conn: sqlite3.Connection, memory_id: str) -> None:
    """Erase prior feedback text in both directions along supersedes links."""
    rows = conn.execute(
        """WITH RECURSIVE chain(memory_id) AS (
               SELECT ?
               UNION
               SELECT e.memory_id FROM memory_entries e JOIN chain c
                 ON e.supersedes_id=c.memory_id
               UNION
               SELECT e.supersedes_id FROM memory_entries e JOIN chain c
                 ON e.memory_id=c.memory_id WHERE e.supersedes_id IS NOT NULL
           ) SELECT memory_id FROM chain""",
        (memory_id,),
    ).fetchall()
    ids = [str(row[0]) for row in rows]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"""UPDATE memory_feedback SET old_content=NULL,new_content=NULL,grounding=NULL,
              redacted=1 WHERE old_memory_id IN ({placeholders}) OR new_memory_id IN ({placeholders})""",
        [*ids, *ids],
    )


def feedback_rows(
    conn: sqlite3.Connection,
    *,
    scope: str | None,
    project_id: str | None,
    limit: int,
    offset: int,
) -> list[MemoryFeedback]:
    clauses: list[str] = []
    args: list[Any] = []
    if scope is not None:
        clauses.append("scope=?")
        args.append(scope)
    if project_id is not None:
        clauses.append("project_id=?")
        args.append(project_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        "SELECT * FROM memory_feedback" + where +
        " ORDER BY created_at DESC,feedback_id LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    return [_to_model(row) for row in rows]


def relevant_feedback_rows(
    conn: sqlite3.Connection,
    *,
    query: str,
    scope: str,
    project_id: str | None,
    limit: int = 3,
    max_json_chars: int = 2000,
) -> list[dict[str, Any]]:
    """Retrieve bounded safe examples using deterministic lexical overlap."""
    if limit <= 0 or max_json_chars <= 0:
        return []
    clauses = [
        "f.scope=?", "f.redacted=0", "f.reason!='do_not_store'",
        "f.old_content IS NOT NULL", "e.sensitivity IN ('normal','public')",
        ("EXISTS (SELECT 1 FROM memory_entry_sources es JOIN memory_sources s USING(source_id) "
        "WHERE es.memory_id=e.memory_id AND s.status='active' "
         "AND (s.expires_at IS NULL OR s.expires_at>?))"),
    ]
    from datetime import UTC, datetime
    args: list[Any] = [scope, datetime.now(UTC).isoformat()]
    if scope == "project":
        clauses.append("f.project_id=?")
        args.append(project_id or "")
    elif project_id is not None:
        # Global preferences and the exact project scope can be merged by the
        # caller as separate requests; this query remains strict by design.
        return []
    rows = conn.execute(
        "SELECT f.*, e.sensitivity AS decision_sensitivity FROM memory_feedback f "
        "JOIN memory_entries e ON e.memory_id=COALESCE(f.new_memory_id,f.old_memory_id) "
        "WHERE " + " AND ".join(clauses) +
        " ORDER BY f.created_at DESC,f.feedback_id LIMIT 200", args,
    ).fetchall()
    query_terms = _terms(query)
    if not query_terms:
        return []
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for row in rows:
        if (row["memory_type"] != "preference" or row["redacted"]
                or row["decision_sensitivity"] not in {"normal", "public"}):
            continue
        old_content, new_content = row["old_content"], row["new_content"]
        if not old_content or not new_content:
            continue
        text = f"{old_content} {new_content} {row['reason']}"
        overlap = len(query_terms & _terms(text))
        if overlap == 0:
            continue
        item = {
            "action": row["action"], "reason": row["reason"],
            "before": old_content, "after": new_content,
        }
        candidates.append((overlap, str(row["created_at"]), item))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    result: list[dict[str, Any]] = []
    for _, _, item in candidates:
        if len(result) >= limit:
            break
        trial = [*result, item]
        if len(json.dumps(trial, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) > max_json_chars:
            continue
        result.append(item)
    return result


def _terms(value: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9_+-]{2,}", value.casefold()))
    cjk = re.findall(r"[\u3400-\u9fff]+", value)
    for run in cjk:
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def _safe_grounding(raw_metadata: str) -> dict[str, Any] | None:
    try:
        metadata = json.loads(raw_metadata)
    except (TypeError, json.JSONDecodeError):
        return None
    grounding = metadata.get("grounding") if isinstance(metadata, dict) else None
    if isinstance(grounding, str) and grounding in {"direct", "confirmed", "inferred"}:
        return {"mode": grounding}
    if not isinstance(grounding, dict):
        return None
    # Grounding references are identifiers/offsets, not copied source text.
    safe: dict[str, Any] = {}
    for key in ("source_id", "source_ids", "claim_id", "selector", "confidence"):
        value = grounding.get(key)
        if isinstance(value, (str, int, float, bool)) or (
            isinstance(value, list) and all(isinstance(item, (str, int)) for item in value)
        ):
            safe[key] = value
    return safe or None


def _to_model(row: sqlite3.Row) -> MemoryFeedback:
    return MemoryFeedback(
        feedback_id=row["feedback_id"], action=row["action"],
        old_memory_id=row["old_memory_id"], old_version=row["old_version"],
        new_memory_id=row["new_memory_id"], new_version=row["new_version"],
        scope=row["scope"], project_id=row["project_id"], memory_type=row["memory_type"],
        reason=row["reason"], origin=row["origin"], created_at=row["created_at"],
        old_content=row["old_content"], new_content=row["new_content"],
        old_confidence=row["old_confidence"], extraction_model=row["extraction_model"],
        grounding=json.loads(row["grounding"]) if row["grounding"] else None,
        redacted=bool(row["redacted"]),
    )
