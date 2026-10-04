from __future__ import annotations

import sqlite3
import time

import pytest

from app.core.prompt_tokens import PromptTokenCounter
from app.core.sessions import SessionRecentMessage, SessionService


def _service() -> SessionService:
    # These retention helpers do not access persistence.
    return SessionService(lambda: sqlite3.connect(":memory:"))


def test_local_fallback_keeps_user_facts_at_both_ends_of_long_turns():
    service = _service()
    messages = [
        SessionRecentMessage(
            role="user",
            content=(
                "Current task: reconcile the release checklist. Keep the original source links. "
                + "Routine meeting notes and status updates. " * 40
                + "Correction: do not publish until offline recovery is verified; deadline is Nov 5."
            ),
            created_at="2026-10-01T10:00:00+00:00",
            trace_id="turn-1",
        ),
        SessionRecentMessage(
            role="agent",
            content="I will update the checklist after the recovery check.",
            created_at="2026-10-01T10:01:00+00:00",
            trace_id="turn-1",
        ),
        SessionRecentMessage(
            role="user",
            content=(
                "Next task: test restore from backup. "
                + "Routine command output and progress details. " * 40
                + "Current constraint: do not overwrite the existing database."
            ),
            created_at="2026-10-02T09:00:00+00:00",
            trace_id="turn-2",
        ),
        SessionRecentMessage(
            role="agent",
            content="The restore test is still open.",
            created_at="2026-10-02T09:01:00+00:00",
            trace_id="turn-2",
        ),
    ]

    summary = service._summarize_messages_locally("", messages)

    assert "Current task: reconcile the release checklist" in summary
    assert "do not publish until offline recovery is verified; deadline is Nov 5" in summary
    assert "Next task: test restore from backup" in summary
    assert "do not overwrite the existing database" in summary
    assert "omitted" in summary.lower() or "省略" in summary


def test_budget_trim_keeps_newer_state_around_long_intervening_noise():
    service = _service()
    summary = (
        "Earlier context: use SQLite and retain source links.\n"
        + "Routine status details with no changed decision. " * 80
        + "Additional routine log details. " * 80
        + "Latest state: backup restore remains unverified; keep the database untouched."
    )

    trimmed = service._trim_summary_for_budget(
        summary=summary, messages=[], token_budget=180,
    )

    assert "Earlier context: use SQLite" in trimmed
    assert "Latest state: backup restore remains unverified" in trimmed
    assert "keep the database untouched" in trimmed
    assert "omitted" in trimmed.lower() or "省略" in trimmed
    assert service._context_token_estimate(trimmed, []) <= 180


def test_small_excerpt_keeps_opening_fact_and_latest_state():
    service = _service()
    text = (
        "2026-10-01T10:00:00+00:00 user: first user message alpha "
        + "routine status " * 30
        + "Latest constraint: retain source."
    )
    excerpt = service._bounded_excerpt(text, max_chars=145, tail_fraction=0.6)
    assert len(excerpt) <= 145
    assert "alpha" in excerpt
    assert "retain source" in excerpt
    assert "omitted" in excerpt


@pytest.mark.parametrize("budget", range(9))
def test_tiny_unicode_budgets_never_publish_an_over_budget_window(tmp_path, budget):
    db_path = tmp_path / "tiny-window.sqlite3"

    def connect():
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connect()
    conn.executescript(
        """
        CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY, title TEXT, status TEXT,
            metadata TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE agent_session_messages(message_id TEXT PRIMARY KEY, session_id TEXT,
            role TEXT, content TEXT, payload TEXT, created_at TEXT);
        CREATE TABLE agent_session_context_windows(session_id TEXT PRIMARY KEY,
            token_budget INTEGER, summary TEXT, recent_messages TEXT,
            token_estimate INTEGER, updated_at TEXT);
        CREATE TABLE agent_session_effects(effect_id TEXT PRIMARY KEY, session_id TEXT,
            effect_type TEXT, created_at TEXT);
        INSERT INTO agent_sessions VALUES('s', 's', 'active', '{}', 't', 't');
        """
    )
    conn.close()
    counter = PromptTokenCounter()
    service = SessionService(connect, context_token_counter=counter)
    original = (
        "当前任务：先完成离线恢复验证。 "
        + "例行状态说明与重复日志。 " * 50
        + "最新约束：不要覆盖原数据库，完成后再灰度发布。"
    )
    service.append_message(session_id="s", role="user", content=original)

    window = service.record_context_exchange(
        session_id="s", user_input=original, agent_answer="恢复验证尚未完成。",
        trace_id="unicode-tiny", token_budget=budget,
    )

    assert window.token_budget == budget
    expected = service._context_token_estimate(window.summary, window.recent_messages)
    assert window.token_estimate == expected
    assert window.token_estimate <= budget
    conn = connect()
    try:
        assert conn.execute(
            "SELECT content FROM agent_session_messages WHERE session_id='s' AND role='user'"
        ).fetchone()[0] == original
        assert conn.execute(
            "SELECT content FROM agent_session_context_messages "
            "WHERE session_id='s' AND role='user' ORDER BY seq LIMIT 1"
        ).fetchone()[0] == original
    finally:
        conn.close()


def _persistent_service(tmp_path):
    db_path = tmp_path / "sessions.sqlite3"

    def connect():
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connect()
    conn.executescript(
        """
        CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY, title TEXT, status TEXT,
            metadata TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE agent_session_messages(message_id TEXT PRIMARY KEY, session_id TEXT,
            role TEXT, content TEXT, payload TEXT, created_at TEXT);
        CREATE TABLE agent_session_context_windows(session_id TEXT PRIMARY KEY,
            token_budget INTEGER, summary TEXT, recent_messages TEXT,
            token_estimate INTEGER, updated_at TEXT);
        CREATE TABLE agent_session_effects(effect_id TEXT PRIMARY KEY, session_id TEXT,
            effect_type TEXT, created_at TEXT);
        CREATE TABLE compact_jobs(session_id TEXT, revision INTEGER, target_seq INTEGER);
        INSERT INTO agent_sessions VALUES('s', 's', 'active', '{}', 't', 't');
        """
    )
    conn.close()
    return SessionService(connect), connect


def test_hard_overflow_queues_transactionally_without_waiting_for_summarizer(tmp_path):
    service, connect = _persistent_service(tmp_path)
    summarize_calls = []

    def enqueue(session_id, revision, target_seq, *, conn):
        conn.execute(
            "INSERT INTO compact_jobs VALUES(?, ?, ?)",
            (session_id, revision, target_seq),
        )

    def slow_summarizer(summary, old, recent, budget):
        summarize_calls.append((summary, old, recent, budget))
        time.sleep(0.15)
        return "semantic summary"

    window = service.record_context_exchange(
        session_id="s", user_input="current task " + "noise " * 100,
        agent_answer="still active " + "details " * 100,
        trace_id="hard-async", token_budget=40,
        context_summarizer=slow_summarizer,
        background_enqueue=enqueue,
    )

    # This checks the cause of frontend stalls without a machine-speed threshold.
    assert summarize_calls == []
    assert window.token_estimate <= 40
    assert window.summary_metadata["emergency_view"] is True
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM compact_jobs").fetchone()[0] == 1
    finally:
        conn.close()


def test_over_budget_read_uses_local_view_without_mutating_raw_prefix_or_epoch(tmp_path):
    service, connect = _persistent_service(tmp_path)
    originals = []
    for index in range(3):
        user = (
            f"Task {index}: compare the backup restore results. "
            + "routine diagnostic output. " * 35
            + f"Constraint {index}: preserve source record {index} and do not overwrite the database."
        )
        answer = f"Turn {index} remains open. " + "routine notes. " * 25
        originals.append((user, answer))
        service.record_context_exchange(
            session_id="s", user_input=user, agent_answer=answer,
            trace_id=f"turn-{index}", token_budget=20_000,
        )

    conn = connect()
    try:
        before = conn.execute(
            "SELECT summary, recent_messages FROM agent_session_context_windows WHERE session_id='s'"
        ).fetchone()
        state_before = conn.execute(
            "SELECT covered_seq, summary_revision FROM agent_session_context_state WHERE session_id='s'"
        ).fetchone()
        raw_before = conn.execute(
            "SELECT seq, role, content, trace_id FROM agent_session_context_messages "
            "WHERE session_id='s' ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()

    raw_window = service.get_context_window(session_id="s")
    window = service.get_prompt_context_window(session_id="s", token_budget=100)

    assert len(raw_window.recent_messages) == 6
    assert [message.content for message in raw_window.recent_messages if message.role == "user"] == [
        row[0] for row in originals
    ]
    assert window.token_budget == 100
    assert window.token_estimate <= 100
    assert window.summary_metadata["emergency_view"] is True
    assert window.summary_metadata["lossy_fallback_possible"] is True
    assert window.summary_metadata["raw_interval"] == {"from_seq": 1, "to_seq": 6}
    assert "Task 2: compare the backup restore results" in window.summary
    assert "Constraint 2: preserve source record 2 and do not overwrite the database" in window.summary

    conn = connect()
    try:
        after = conn.execute(
            "SELECT summary, recent_messages FROM agent_session_context_windows WHERE session_id='s'"
        ).fetchone()
        state_after = conn.execute(
            "SELECT covered_seq, summary_revision FROM agent_session_context_state WHERE session_id='s'"
        ).fetchone()
        raw_after = conn.execute(
            "SELECT seq, role, content, trace_id FROM agent_session_context_messages "
            "WHERE session_id='s' ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    assert tuple(before) == tuple(after)
    assert tuple(state_before) == tuple(state_after)
    assert [tuple(row) for row in raw_before] == [tuple(row) for row in raw_after]
    assert [row[2] for row in raw_after if row[1] == "user"] == [row[0] for row in originals]
