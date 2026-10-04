from __future__ import annotations

import sqlite3

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


@pytest.mark.parametrize("budget", [0, 1, 2, 4, 8])
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
