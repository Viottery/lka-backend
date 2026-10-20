"""Additive session provenance migration from durable creation evidence."""
from __future__ import annotations

import sqlite3


def add_session_origin_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_sessions)")}
    if not columns:
        return
    if "creation_source" not in columns:
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN creation_source TEXT NOT NULL "
                     "DEFAULT 'unknown' CHECK(creation_source IN ('user','assistant','system','unknown'))")
    if "creator_session_id" not in columns:
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN creator_session_id TEXT")


def backfill_session_origins(conn: sqlite3.Connection) -> None:
    """Never infer origin from titles, arbitrary metadata or later intervention.

    Run on startup so interrupted upgrades and records written by an older
    process can converge. Already classified sessions are immutable here.
    Missing evidence stays unknown; classification grants no permissions.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "manager_delegates" in tables:
        conn.execute("""
            UPDATE agent_sessions SET creation_source='assistant', creator_session_id=(
                SELECT d.parent_session_id FROM manager_delegates d
                WHERE d.session_id=agent_sessions.session_id
            ) WHERE creation_source='unknown' AND session_id IN (
                SELECT session_id FROM manager_delegates
            )
        """)
    if {"managed_session_links", "management_commands"} <= tables:
        # send_instruction/resume/cancel also create links: only creation
        # commands prove that the assistant originally created a session.
        conn.execute("""
            WITH creations AS (
                SELECT l.task_session_id, l.manager_session_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY l.task_session_id ORDER BY c.created_at, c.command_id
                       ) AS position
                FROM managed_session_links l JOIN management_commands c
                  ON c.manager_session_id=l.manager_session_id AND c.command_id=l.command_id
                WHERE c.action IN ('start_task','start_work','queue_work')
            ) UPDATE agent_sessions SET creation_source='assistant', creator_session_id=(
                SELECT manager_session_id FROM creations
                WHERE task_session_id=agent_sessions.session_id AND position=1
            ) WHERE creation_source='unknown' AND session_id IN (SELECT task_session_id FROM creations)
        """)
    if "agent_runs" in tables:
        conn.execute("""
            WITH children AS (
                SELECT r.session_id, p.session_id AS parent_session_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY r.session_id ORDER BY r.created_at, r.run_id
                       ) AS position
                FROM agent_runs r JOIN agent_runs p ON p.run_id=json_extract(
                    CASE WHEN json_valid(r.record_payload) THEN r.record_payload ELSE '{}' END,
                    '$.parent_run_id'
                ) WHERE r.session_id!=p.session_id
            ) UPDATE agent_sessions SET creation_source='assistant', creator_session_id=(
                SELECT parent_session_id FROM children WHERE session_id=agent_sessions.session_id AND position=1
            ) WHERE creation_source='unknown' AND session_id IN (SELECT session_id FROM children)
        """)
    if {"watch_occurrences", "agent_runs"} <= tables:
        conn.execute("""
            UPDATE agent_sessions SET creation_source='system'
            WHERE creation_source='unknown' AND session_id IN (
                SELECT r.session_id FROM watch_occurrences o JOIN agent_runs r ON r.run_id=o.run_id
            )
        """)
