from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.core.sessions import SessionService


def _service(tmp_path):
    db_path = tmp_path / "sessions.sqlite3"

    def connect():
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connect()
    conn.executescript(
        """
        CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY, title TEXT, status TEXT, metadata TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE agent_session_messages(message_id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, payload TEXT, created_at TEXT);
        CREATE TABLE agent_session_context_windows(session_id TEXT PRIMARY KEY, token_budget INTEGER, summary TEXT, recent_messages TEXT, token_estimate INTEGER, updated_at TEXT);
        CREATE TABLE agent_session_effects(effect_id TEXT PRIMARY KEY, session_id TEXT, effect_type TEXT, created_at TEXT);
        INSERT INTO agent_sessions VALUES('s', 's', 'active', '{}', 't', 't');
        """
    )
    conn.close()
    return SessionService(connect), connect


def test_concurrent_exchanges_keep_all_messages_and_enqueue_transactionally(tmp_path):
    service, connect = _service(tmp_path)

    def enqueue(session_id, revision, target_seq, *, conn):
        conn.execute(
            "INSERT OR IGNORE INTO compact_outbox VALUES(?, ?, ?)",
            (f"{session_id}:{target_seq}", revision, target_seq),
        )

    conn = connect()
    conn.execute("CREATE TABLE compact_outbox(job TEXT PRIMARY KEY, revision INTEGER, target_seq INTEGER)")
    conn.commit()
    conn.close()

    def record(i):
        service.record_context_exchange(
            session_id="s", user_input=f"u{i}", agent_answer=f"a{i}", trace_id=f"t{i}",
            token_budget=1000, background_enqueue=enqueue,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, range(8)))
    window = service.get_context_window(session_id="s")
    assert len(window.recent_messages) == 16
    assert {message.content for message in window.recent_messages} == {
        *(f"u{i}" for i in range(8)), *(f"a{i}" for i in range(8))
    }
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM compact_outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM agent_session_messages").fetchone()[0] == 0
    finally:
        conn.close()


def test_stale_summary_publish_rejected_after_concurrent_exchange(tmp_path):
    service, _ = _service(tmp_path)
    service.record_context_exchange(
        session_id="s", user_input="first", agent_answer="answer", trace_id="t1", token_budget=1000
    )
    assert not service.publish_context_summary(
        session_id="s", expected_revision=0, expected_covered_seq=0,
        target_seq=2, summary="stale"
    )
    service.record_context_exchange(
        session_id="s", user_input="second", agent_answer="answer2", trace_id="t2", token_budget=1000
    )
    window = service.get_context_window(session_id="s")
    assert "stale" not in window.summary
    assert {m.content for m in window.recent_messages} >= {"first", "second", "answer2"}


def test_effect_id_is_idempotent_and_sync_hard_threshold_fallback_runs(tmp_path):
    service, connect = _service(tmp_path)
    calls = []

    def summarizer(summary, old, recent, budget):
        calls.append([m.content for m in old])
        return "sync summary"

    args = {
        "session_id": "s", "user_input": "u" * 400,
        "agent_answer": "a" * 400, "trace_id": "t",
        "effect_id": "once", "token_budget": 30,
    }
    first = service.record_context_exchange(**args, context_summarizer=summarizer)
    second = service.record_context_exchange(**args, context_summarizer=summarizer)
    assert first.summary == "sync summary"
    assert len(calls) == 1
    assert second.summary == first.summary
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_session_messages").fetchone()[0] == 0
    finally:
        conn.close()


def test_agent_message_callback_is_atomic_and_role_filtered(tmp_path):
    service, connect = _service(tmp_path)
    conn = connect()
    conn.execute("CREATE TABLE memory_outbox(message_id TEXT PRIMARY KEY, content TEXT)")
    conn.commit()
    conn.close()

    def enqueue(db, message):
        db.execute(
            "INSERT OR IGNORE INTO memory_outbox VALUES(?, ?)",
            (message.message_id, message.content),
        )

    service.append_message(
        session_id="s", role="user", content="user", persisted_message_callback=enqueue
    )
    agent = service.append_message(
        session_id="s", role="agent", content="answer", persisted_message_callback=enqueue
    )
    conn = connect()
    try:
        assert conn.execute("SELECT message_id, content FROM memory_outbox").fetchall()[0][:] == (
            agent.message_id, "answer"
        )
    finally:
        conn.close()

    def fail(db, message):
        db.execute("INSERT INTO missing_table VALUES(?)", (message.content,))

    try:
        service.append_message(
            session_id="s", role="agent", content="rollback", persisted_message_callback=fail
        )
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("callback failure should abort message persistence")
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_session_messages WHERE content = 'rollback'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_background_enqueue_waits_for_soft_threshold(tmp_path):
    service, connect = _service(tmp_path)
    conn = connect()
    conn.execute("CREATE TABLE compact_outbox(job TEXT PRIMARY KEY, revision INTEGER, target_seq INTEGER)")
    conn.commit()
    conn.close()

    def enqueue(session_id, revision, target_seq, *, conn):
        conn.execute(
            "INSERT OR IGNORE INTO compact_outbox VALUES(?, ?, ?)",
            (f"{session_id}:{target_seq}", revision, target_seq),
        )

    service.record_context_exchange(
        session_id="s", user_input="small", agent_answer="answer", trace_id="t0",
        token_budget=1000, background_enqueue=enqueue,
    )
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM compact_outbox").fetchone()[0] == 0
    conn.close()
    long = "x" * 2500
    service.record_context_exchange(
        session_id="s", user_input=long, agent_answer="answer", trace_id="t1",
        token_budget=1000, background_enqueue=enqueue,
    )
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM compact_outbox").fetchone()[0] == 0
    conn.close()


def test_background_publish_uses_fixed_target_and_keeps_tail(tmp_path):
    service, connect = _service(tmp_path)
    conn = connect()
    conn.execute("CREATE TABLE compact_outbox(job TEXT PRIMARY KEY, revision INTEGER, target_seq INTEGER)")
    conn.commit()
    conn.close()

    def enqueue(session_id, revision, target_seq, *, conn):
        conn.execute(
            "INSERT OR IGNORE INTO compact_outbox VALUES(?, ?, ?)",
            (f"{session_id}:{target_seq}", revision, target_seq),
        )

    service.record_context_exchange(
        session_id="s", user_input="x" * 300, agent_answer="a" * 300,
        trace_id="t1", token_budget=200, background_enqueue=enqueue,
    )
    # This configuration reaches soft threshold while remaining under the hard budget.
    conn = connect()
    revision, target = conn.execute(
        "SELECT revision, target_seq FROM compact_outbox ORDER BY target_seq DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert service.publish_context_summary(
        session_id="s", expected_revision=revision, expected_covered_seq=0,
        target_seq=target, summary="published summary", token_budget=200,
    )
    before = service.get_context_window(session_id="s")
    service.record_context_exchange(
        session_id="s", user_input="new tail", agent_answer="tail answer",
        trace_id="t2", token_budget=200,
    )
    after = service.get_context_window(session_id="s")
    assert after.summary == "published summary"
    assert "new tail" in {message.content for message in after.recent_messages}
    assert len(after.recent_messages) >= len(before.recent_messages)
