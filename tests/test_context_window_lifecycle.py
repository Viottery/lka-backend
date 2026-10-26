"""Lifecycle boundaries for the opt-in intent conversation lane."""

import json
from types import SimpleNamespace

from app.core.background_jobs import BackgroundJobStore
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.prompt_tokens import PromptTokenCounter
from app.core.sessions import SessionService
from app.storage.context_records import ContextRecordStore
from app.storage.db import connect, init_db


def setup(tmp_path):
    db = tmp_path / "lifecycle.sqlite3"
    init_db(db)
    factory = lambda: connect(db)
    sessions = SessionService(factory, context_token_counter=PromptTokenCounter())
    session = sessions.create_session(title="window").session.session_id
    records = ContextRecordStore(factory)
    records.ensure_schema()
    sessions.atomic_context_store = records
    sessions.atomic_context_mode = "active"
    sessions.atomic_context_profile = "intent"
    sessions.get_context_window(session_id=session)
    return db, factory, sessions, session, records


def add(factory, session, role, body, trace):
    with factory() as conn:
        conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) "
                     "SELECT ?,COALESCE(MAX(seq),0)+1,?,?, '2026-10-27T00:00:00+00:00',? "
                     "FROM agent_session_context_messages WHERE session_id=?",
                     (session, role, body, trace, session))


def test_small_budget_never_keeps_only_answer_and_turn_order_is_old_to_new(tmp_path):
    _, _, sessions, session, _ = setup(tmp_path)
    sessions.record_context_exchange(session_id=session, user_input="U" * 300,
                                     agent_answer="A" * 300, trace_id="old")
    prompt = sessions.get_prompt_context_window(session_id=session, token_budget=100)
    assert prompt.token_estimate <= 100
    assert [m.role for m in prompt.recent_messages] != ["agent"]
    assert prompt.summary_metadata["omission"] is True
    assert prompt.summary_metadata["omitted_from_seq"] == 1
    assert prompt.summary_metadata["omitted_to_seq"] == 2
    assert [m.content for m in sessions.get_context_window(session_id=session).recent_messages] == ["U" * 300, "A" * 300]


def test_batch_trace_is_kept_as_whole_group_and_output_is_sequence_ordered(tmp_path):
    _, factory, sessions, session, _ = setup(tmp_path)
    add(factory, session, "user", "batch-1", "batch")
    add(factory, session, "user", "batch-2", "batch")
    add(factory, session, "agent", "batch-answer", "batch")
    add(factory, session, "user", "later-user", "later")
    add(factory, session, "agent", "later-answer", "later")
    prompt = sessions.get_prompt_context_window(session_id=session, token_budget=1000)
    assert [m.content for m in prompt.recent_messages] == ["batch-1", "batch-2", "batch-answer", "later-user", "later-answer"]


def test_recent_user_query_finds_turn_after_more_than_4096_background_rows(tmp_path):
    _, factory, sessions, session, _ = setup(tmp_path)
    sessions.record_context_exchange(session_id=session, user_input="human-question",
                                     agent_answer="human-answer", trace_id="human")
    with factory() as conn:
        conn.executemany("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) VALUES(?,?,?,?,?,?)",
            [(session, seq, "tool", "background", "2026-10-27T00:00:00+00:00", f"bg-{seq}")
             for seq in range(3, 4103)])
    prompt = sessions.get_prompt_context_window(session_id=session, token_budget=1000)
    assert [m.content for m in prompt.recent_messages] == ["human-question", "human-answer"]


def test_active_intent_queues_only_record_job_and_skips_queued_legacy_compaction(tmp_path):
    db, factory, sessions, session, _ = setup(tmp_path)
    queue = BackgroundJobStore(db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(db), memory=None, store=queue,
        session_service=sessions, atomic_context_mode="active", atomic_context_profile="intent")
    with factory() as conn:
        conn.execute("BEGIN")
        sessions.record_context_exchange_in_transaction(conn, session_id=session, user_input="u",
            agent_answer="a", trace_id="t", effect_id="e", token_budget=1,
            background_enqueue=coordinator.enqueue_compaction)
        conn.commit()
    jobs = queue.list(scope_id=session)
    assert {job["kind"] for job in jobs} == {"context_records"}
    legacy = queue.enqueue("context_compact", session, "legacy", {"revision": 1, "target_seq": 2})
    model_calls = []
    coordinator.llm_client = SimpleNamespace(complete_text=lambda **kwargs: model_calls.append(kwargs))
    assert coordinator.worker.run_one()
    retired = queue.get(legacy["job_id"], include_payload=True)
    assert retired["status"] == "succeeded"
    assert retired["error_class"] == "retired_active_intent"
    assert model_calls == []


def test_normal_active_record_path_never_calls_legacy_summarizer(tmp_path):
    _, _, sessions, session, _ = setup(tmp_path)
    calls = []
    result = sessions.record_context_exchange(session_id=session, user_input="u" * 500,
        agent_answer="a" * 500, trace_id="t", token_budget=100,
        context_summarizer=lambda *args: calls.append(args))
    assert calls == []
    assert sessions.get_context_window(session_id=session).recent_messages
    assert result.session_id == session


def test_shadow_profile_keeps_both_background_paths(tmp_path):
    db, factory, sessions, session, _ = setup(tmp_path)
    queue = BackgroundJobStore(db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(db), memory=None, store=queue,
        session_service=sessions, atomic_context_mode="shadow", atomic_context_profile="intent")
    with factory() as conn:
        conn.execute("BEGIN")
        sessions.record_context_exchange_in_transaction(conn, session_id=session, user_input="u",
            agent_answer="a", trace_id="t", effect_id="shadow", token_budget=1,
            background_enqueue=coordinator.enqueue_compaction)
        conn.commit()
    jobs = queue.list(scope_id=session)
    assert {job["kind"] for job in jobs} == {"context_compact", "context_records"}


def test_active_intent_legacy_window_is_bounded_without_scanning_or_deleting_source(tmp_path, monkeypatch):
    _, factory, sessions, session, _ = setup(tmp_path)
    with factory() as conn:
        conn.executemany(
            "INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) "
            "VALUES(?,?,?,?,?,?)",
            [(session, seq, "tool", "SOURCE_ROW_" + str(seq) + " x" * 20,
              "2026-10-27T00:00:00+00:00", f"bg-{seq}") for seq in range(1, 2201)],
        )
        conn.execute("INSERT INTO agent_session_context_state(session_id,revision,next_seq,covered_seq) VALUES(?,1,2201,0) "
                     "ON CONFLICT(session_id) DO UPDATE SET revision=1,next_seq=2201,covered_seq=0", (session,))

    def unexpected_full_scan(*args, **kwargs):
        raise AssertionError("active intent write must not load all source history")

    monkeypatch.setattr(sessions, "_context_messages_from_connection", unexpected_full_scan)
    sessions.record_context_exchange(session_id=session, user_input="new request",
        agent_answer="new answer", trace_id="current", token_budget=1)
    with factory() as conn:
        source_count = conn.execute("SELECT COUNT(*) FROM agent_session_context_messages WHERE session_id=?",
                                    (session,)).fetchone()[0]
        window = json.loads(conn.execute("SELECT recent_messages FROM agent_session_context_windows WHERE session_id=?",
                                         (session,)).fetchone()[0])
    assert source_count == 2202
    assert len(window) <= 512
    assert sum(len(item["content"]) for item in window) <= 32_768
    assert window[-2]["content"] == "new request"
    assert window[-1]["content"] == "new answer"


def test_active_intent_transactional_write_uses_bounded_legacy_mirror(tmp_path, monkeypatch):
    _, factory, sessions, session, _ = setup(tmp_path)
    with factory() as conn:
        conn.executemany(
            "INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) "
            "VALUES(?,?,?,?,?,?)",
            [(session, seq, "tool", "SOURCE_ROW_" + str(seq), "2026-10-27T00:00:00+00:00", f"bg-{seq}")
             for seq in range(1, 1201)],
        )
        conn.execute("INSERT INTO agent_session_context_state(session_id,revision,next_seq,covered_seq) VALUES(?,1,1201,0) "
                     "ON CONFLICT(session_id) DO UPDATE SET revision=1,next_seq=1201,covered_seq=0", (session,))

    def unexpected_full_scan(*args, **kwargs):
        raise AssertionError("active intent transaction must not load all source history")

    monkeypatch.setattr(sessions, "_context_messages_from_connection", unexpected_full_scan)
    with factory() as conn:
        conn.execute("BEGIN")
        sessions.record_context_exchange_in_transaction(conn, session_id=session,
            user_input="transaction user", agent_answer="transaction agent", trace_id="tx",
            effect_id="bounded-tx", token_budget=1)
        conn.commit()
        source_count = conn.execute("SELECT COUNT(*) FROM agent_session_context_messages WHERE session_id=?",
                                    (session,)).fetchone()[0]
        window = json.loads(conn.execute("SELECT recent_messages FROM agent_session_context_windows WHERE session_id=?",
                                         (session,)).fetchone()[0])
    assert source_count == 1202
    assert len(window) <= 512
    assert window[-2]["content"] == "transaction user"
    assert window[-1]["content"] == "transaction agent"


def test_intent_candidate_agent_cap_drops_partial_trace_and_reports_omission(tmp_path):
    _, factory, sessions, session, _ = setup(tmp_path)
    with factory() as conn:
        conn.executemany(
            "INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) "
            "VALUES(?,?,?,?,?,?)",
            [(session, 1, "user", "BATCH_USER", "2026-10-27T00:00:00+00:00", "huge-trace")]
            + [(session, seq, "agent", f"BATCH_AGENT_{seq}", "2026-10-27T00:00:00+00:00", "huge-trace")
               for seq in range(2, 1032)],
        )
    prompt = sessions.get_prompt_context_window(session_id=session, token_budget=4096)
    metadata = prompt.summary_metadata
    assert prompt.recent_messages == []
    assert metadata["candidate_row_limit_reached"] is True
    assert metadata["partial_turns_omitted"] == 1
    assert metadata["candidate_rows_omitted"] > 1000
    assert metadata["omission"] is True
    assert metadata["retained_from_seq"] is None
    assert metadata["retained_to_seq"] is None


def test_intent_user_cap_does_not_reset_when_agent_count_is_small(tmp_path):
    _, factory, sessions, session, _ = setup(tmp_path)
    with factory() as conn:
        conn.executemany(
            "INSERT INTO agent_session_context_messages VALUES(?,?,?,?,?,?)",
            [(session, seq, "user", "one batched turn", "now", "batch") for seq in range(1, 514)]
            + [(session, 514, "agent", "one answer", "now", "batch")],
        )
    prompt = sessions.get_prompt_context_window(session_id=session, token_budget=10000)
    assert prompt.recent_messages == []
    assert prompt.summary_metadata["candidate_row_limit_reached"]
    assert prompt.summary_metadata["partial_turns_omitted"] == 1


def test_intent_enqueues_latest_complete_exchange_for_direction_and_response(tmp_path):
    db, factory, sessions, session, _ = setup(tmp_path)
    queue = BackgroundJobStore(db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(db), memory=None, store=queue,
        session_service=sessions, atomic_context_mode="active", atomic_context_profile="intent")
    with factory() as conn:
        conn.execute("BEGIN")
        sessions.record_context_exchange_in_transaction(conn, session_id=session,
            user_input="latest intent", agent_answer="latest reply", trace_id="latest",
            effect_id="latest", background_enqueue=coordinator.enqueue_compaction)
        conn.commit()
    jobs = queue.list(scope_id=session)
    job = queue.get(next(j["job_id"] for j in jobs if j["kind"] == "context_records"), include_payload=True)
    assert job["payload"]["target_seq"] == 2
    prompt = sessions.get_prompt_context_window(session_id=session, token_budget=1000)
    assert [m.content for m in prompt.recent_messages] == ["latest intent", "latest reply"]
