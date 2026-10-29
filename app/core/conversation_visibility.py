"""Shared eligibility for chat-page messages and their sequenced history."""

from __future__ import annotations

import re

_MESSAGE_ORIGIN = "trim(coalesce(nullif(json_extract(payload, '$.input_origin'), ''), json_extract(payload, '$.input_envelope.origin'), ''))"
CONVERSATION_MESSAGES = f"""
    AND json_type(payload, '$.user_visible') IS NOT 'false'
    AND json_type(payload, '$.metadata.user_visible') IS NOT 'false'
    AND (
        role IN ('agent', 'assistant')
        OR {_MESSAGE_ORIGIN} = 'user'
        OR ({_MESSAGE_ORIGIN} = '' AND role IN ('user', 'tool'))
        OR (role = 'tool' AND {_MESSAGE_ORIGIN} = 'manager'
            AND json_extract(payload, '$.input_envelope.origin') = 'manager'
            AND json_type(payload, '$.input_envelope.purpose') = 'text'
            AND trim(json_extract(payload, '$.input_envelope.purpose')) != '')
    )
"""


def conversation_source_predicate(conn, session_id: str, *, alias: str = "m") -> tuple[str, list]:
    """Use current publication visibility without loading message bodies.

    Read once in the caller's snapshot, then reuse the bound predicate for a
    batch. Legacy exchanges without a published message retain their ordinary
    user/answer pairing; an unpaired work answer requires a visible publication.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("Invalid conversation source alias")
    projection = CONVERSATION_MESSAGES.strip().removeprefix("AND ")
    rows = conn.execute(
        "SELECT role,json_extract(payload,'$.trace_id') AS trace_id,"
        f"CASE WHEN {projection} THEN 1 ELSE 0 END AS visible "
        "FROM agent_session_messages WHERE session_id=? "
        "AND json_type(payload,'$.trace_id')='text'",
        (session_id,),
    ).fetchall()
    visible: dict[str, set[str]] = {}
    hidden: dict[str, set[str]] = {}
    for row in rows:
        role, trace, shown = row[0], row[1], row[2]
        if role == "assistant":
            role = "agent"
        if trace and role in {"user", "agent", "tool"}:
            (visible if shown else hidden).setdefault(role, set()).add(trace)
    params: list = []

    def membership(traces: set[str], *, negate: bool = False) -> str:
        values = sorted(traces)
        params.extend(values)
        return f"{alias}.trace_id {'NOT IN' if negate else 'IN'} ({','.join('?' for _ in values)})"

    ordinary_user = f"{alias}.role='user'"
    if blocked := hidden.get("user", set()) - visible.get("user", set()):
        ordinary_user += f" AND ({alias}.trace_id IS NULL OR {membership(blocked, negate=True)})"
    ordinary_agent = (
        f"{alias}.role='agent' AND {alias}.trace_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM agent_session_context_messages u "
        f"WHERE u.session_id={alias}.session_id AND u.trace_id={alias}.trace_id AND u.role='user') "
        "AND NOT EXISTS (SELECT 1 FROM agent_session_context_messages t "
        f"WHERE t.session_id={alias}.session_id AND t.trace_id={alias}.trace_id AND t.role='tool')"
    )
    if blocked := hidden.get("agent", set()) - visible.get("agent", set()):
        ordinary_agent += f" AND {membership(blocked, negate=True)}"
    clauses = [f"({ordinary_user})", f"({ordinary_agent})"]
    for role in ("user", "agent", "tool"):
        if traces := visible.get(role):
            clauses.append(f"({alias}.role='{role}' AND {membership(traces)})")
    return "(" + " OR ".join(clauses) + ")", params
