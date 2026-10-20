"""Transactional root-run admission for existing local sessions.

This is a logical busy slot, not a worker execution lease. Paused and waiting
runs retain their position; child execution remains the scheduler's concern.
Callers serialize admission with their enclosing BEGIN IMMEDIATE transaction.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})


class SessionBusyError(ValueError):
    def __init__(self, session_id: str, run_id: str):
        self.session_id = session_id
        self.run_id = run_id
        super().__init__(f"Session {session_id} is busy with root run {run_id}.")


class SessionAdmissionStore:
    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS session_admissions (
            session_id TEXT PRIMARY KEY,
            run_id TEXT,
            generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0)
        )""")

    @staticmethod
    def _existing_session(conn: sqlite3.Connection, session_id: str) -> bool:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_sessions'").fetchone() is None:
            return False
        row = conn.execute("SELECT status FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is not None and row[0] == "deleted":
            raise ValueError("Cannot create a run in a deleted session.")
        return row is not None

    @staticmethod
    def reserve(
        conn: sqlite3.Connection, session_id: str, run_id: str,
        ignore_run_ids: tuple[str, ...] = (),
    ) -> int | None:
        """Reserve/revalidate a root slot; legacy active roots also block it.

        Unknown sessions preserve the low-level run store's historical behavior.
        ignore_run_ids is for atomic transitions whose old runs are being retired
        in the same transaction, never for bypassing a live root independently.
        """
        if not SessionAdmissionStore._existing_session(conn, session_id):
            return None
        SessionAdmissionStore.ensure_schema(conn)
        ignored = set(ignore_run_ids)
        rows = conn.execute(
            "SELECT run_id,status,record_payload FROM agent_runs WHERE session_id=? "
            "AND status NOT IN ('completed','failed','cancelled','timed_out') ORDER BY created_at,run_id",
            (session_id,),
        ).fetchall()
        live_roots = set()
        for candidate_id, status, payload in rows:
            if status in TERMINAL_RUN_STATUSES:
                continue
            try:
                record = json.loads(payload)
                is_root = not isinstance(record, dict) or record.get("parent_run_id") is None
            except (TypeError, ValueError):
                is_root = True  # Broken legacy payload must not open a second writer.
            if is_root:
                live_roots.add(candidate_id)
                if candidate_id != run_id and candidate_id not in ignored:
                    raise SessionBusyError(session_id, candidate_id)
        row = conn.execute(
            "SELECT run_id,generation FROM session_admissions WHERE session_id=?", (session_id,),
        ).fetchone()
        if row is not None and row[0] == run_id:
            return row[1]
        if row is not None and row[0] in live_roots and row[0] not in ignored:
            raise SessionBusyError(session_id, row[0])
        generation = (row[1] if row else 0) + 1
        conn.execute(
            "INSERT INTO session_admissions(session_id,run_id,generation) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET run_id=excluded.run_id,generation=excluded.generation",
            (session_id, run_id, generation),
        )
        return generation

    @staticmethod
    def release_if_terminal(conn: sqlite3.Connection, record: Mapping[str, Any]) -> None:
        if record.get("parent_run_id") is not None or record.get("status") not in TERMINAL_RUN_STATUSES:
            return
        SessionAdmissionStore.ensure_schema(conn)
        conn.execute(
            "UPDATE session_admissions SET run_id=NULL WHERE session_id=? AND run_id=?",
            (record["session_id"], record["run_id"]),
        )
