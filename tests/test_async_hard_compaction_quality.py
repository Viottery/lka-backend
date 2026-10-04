from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.prompt_tokens import PromptTokenCounter
from app.core.sessions import SessionService
from tests.test_compaction_retention_quality import _persistent_service


def _state(connect):
    with connect() as conn:
        row = conn.execute(
            "SELECT revision, next_seq, covered_seq, summary_revision, summary_metadata "
            "FROM agent_session_context_state WHERE session_id='s'"
        ).fetchone()
        return tuple(row) if row else None


def _enqueue(session_id, revision, target_seq, *, conn):
    conn.execute("INSERT INTO compact_jobs VALUES(?, ?, ?)", (session_id, revision, target_seq))


@pytest.mark.parametrize("budget", range(9))
def test_async_tiny_unicode_projection_and_replay_preserve_original_sequence(tmp_path, budget):
    _, connect = _persistent_service(tmp_path)
    service = SessionService(connect, context_token_counter=PromptTokenCounter())
    original = "当前任务：验证离线恢复。" * 100 + "不要覆盖原数据库。"
    calls = []

    def summarize(*args):
        calls.append(args)
        return "summary"

    args = {
        "session_id": "s", "user_input": original, "agent_answer": "未完成。", "trace_id": "t",
        "effect_id": "once", "token_budget": budget, "context_summarizer": summarize,
        "background_enqueue": _enqueue,
    }
    first = service.record_context_exchange(**args)
    before = _state(connect)
    second = service.record_context_exchange(**args)
    restarted = SessionService(connect, context_token_counter=PromptTokenCounter())
    prompt = restarted.get_prompt_context_window(session_id="s", token_budget=budget)
    for window in (first, second, prompt):
        assert window.token_budget == budget
        assert window.token_estimate == service._context_token_estimate(
            window.summary, window.recent_messages,
        )
        assert window.token_estimate <= budget
        assert window.summary_metadata["raw_interval"] == {"from_seq": 1, "to_seq": 2}
    assert calls == []
    assert _state(connect) == before
    assert before[:4] == (1, 3, 0, 0)
    assert restarted.get_context_window(session_id="s").recent_messages[0].content == original
    with connect() as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT seq,content FROM agent_session_context_messages ORDER BY seq"
        )] == [(1, original), (2, "未完成。")]
        assert conn.execute("SELECT COUNT(*) FROM compact_jobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_session_effects").fetchone()[0] == 1


def test_callback_body_typeerror_runs_once_and_rolls_back_exchange_and_job(tmp_path):
    service, connect = _persistent_service(tmp_path)
    calls = []

    def enqueue(session_id, revision, target_seq, conn):
        calls.append(target_seq)
        _enqueue(session_id, revision, target_seq, conn=conn)
        raise TypeError("bad conn payload inside callback")

    with pytest.raises(TypeError, match="inside callback"):
        service.record_context_exchange(
            session_id="s", user_input="u" * 500, agent_answer="a" * 500,
            trace_id="rollback", token_budget=40, effect_id="failed", background_enqueue=enqueue,
        )
    assert calls == [2]
    with connect() as conn:
        for table in ("compact_jobs", "agent_session_effects", "agent_session_context_windows"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # Sidecar tables were also created in the aborted transaction. Retrying is safe.
    service.record_context_exchange(
        session_id="s", user_input="retry", agent_answer="answer", trace_id="retry",
        token_budget=40, effect_id="failed", background_enqueue=_enqueue,
    )
    assert _state(connect)[:4] == (1, 3, 0, 0)


def test_concurrent_hard_exchanges_and_replays_keep_sequence_and_outbox_atomic(tmp_path):
    service, connect = _persistent_service(tmp_path)

    def record(index):
        trace = f"turn-{index % 4}"
        return service.record_context_exchange(
            session_id="s", user_input=trace + "u" * 500, agent_answer=trace + "a" * 500,
            trace_id=trace, effect_id=trace, token_budget=10, background_enqueue=_enqueue,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        windows = list(pool.map(record, range(12)))
    assert all(window.token_estimate <= 10 for window in windows)
    assert _state(connect)[:4] == (4, 9, 0, 0)
    raw = service.get_context_window(session_id="s")
    assert len(raw.recent_messages) == 8
    assert {m.trace_id for m in raw.recent_messages} == {f"turn-{i}" for i in range(4)}
    with connect() as conn:
        assert [row[0] for row in conn.execute(
            "SELECT seq FROM agent_session_context_messages ORDER BY seq"
        )] == list(range(1, 9))
        assert [row[0] for row in conn.execute(
            "SELECT target_seq FROM compact_jobs ORDER BY target_seq"
        )] == [2, 4, 6, 8]


def test_legacy_window_adoption_keeps_identical_messages_as_distinct_sequences(tmp_path):
    service, connect = _persistent_service(tmp_path)
    legacy = [
        {"role": "user", "content": "same", "created_at": "t", "trace_id": "legacy"},
    ] * 2
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_session_context_windows VALUES('s', 1000, 'old summary', ?, 1, 't')",
            (json.dumps(legacy),),
        )
    assert len(service.get_context_window(session_id="s").recent_messages) == 2
    service.record_context_exchange(
        session_id="s", user_input="new user", agent_answer="new answer", trace_id="new",
        token_budget=1000,
    )
    restarted = SessionService(connect)
    assert [m.content for m in restarted.get_context_window(session_id="s").recent_messages] == [
        "same", "same", "new user", "new answer",
    ]
    assert _state(connect)[:4] == (1, 5, 0, 0)


@pytest.mark.parametrize("connection_form", ["positional_only", "var_kwargs"])
def test_transactional_callback_connection_forms_skip_inline_summary(tmp_path, connection_form):
    service, connect = _persistent_service(tmp_path)

    def positional_only(session_id, revision, target_seq, conn, /):
        _enqueue(session_id, revision, target_seq, conn=conn)

    def var_kwargs(session_id, revision, target_seq, **kwargs):
        _enqueue(session_id, revision, target_seq, conn=kwargs["conn"])

    def inline(*_args):
        pytest.fail("transactional queue must skip inline model")

    service.record_context_exchange(
        session_id="s", user_input="u" * 500, agent_answer="a" * 500,
        trace_id="t", token_budget=40, context_summarizer=inline,
        background_enqueue=positional_only if connection_form == "positional_only" else var_kwargs,
    )
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM compact_jobs").fetchone()[0] == 1


@pytest.mark.parametrize("prompt", [False, True])
def test_read_snapshot_stays_consistent_when_summary_publishes_mid_read(tmp_path, prompt):
    service, connect = _persistent_service(tmp_path)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original = "Original fixed prefix " + "details " * 150
    service.record_context_exchange(
        session_id="s", user_input=original, agent_answer="original answer", trace_id="old",
        token_budget=10_000,
    )
    read_window = threading.Event()
    release = threading.Event()

    class PausedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if "SELECT session_id, token_budget, summary" in sql:
                read_window.set()
                assert release.wait(timeout=5)
            return cursor

    def paused_connect():
        conn = sqlite3.connect(tmp_path / "sessions.sqlite3", factory=PausedConnection, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    reader = SessionService(paused_connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            reader.get_prompt_context_window if prompt else reader.get_context_window,
            session_id="s", **({"token_budget": 100} if prompt else {}),
        )
        try:
            assert read_window.wait(timeout=5)
            assert service.publish_context_summary(
                session_id="s", expected_revision=1, expected_covered_seq=0,
                target_seq=2, summary="published", token_budget=10_000,
            )
            service.record_context_exchange(
                session_id="s", user_input="new tail", agent_answer="new answer", trace_id="new",
                token_budget=10_000,
            )
        finally:
            release.set()
        window = future.result(timeout=5)
    if prompt:
        assert window.summary_metadata["raw_interval"] == {"from_seq": 1, "to_seq": 2}
        assert window.summary_metadata["input_trace_ids"] == ["old"]
        assert window.token_estimate <= 100
        assert "Original fixed prefix" in window.summary
    else:
        assert window.summary == ""
        assert [m.content for m in window.recent_messages] == [original, "original answer"]
    current = service.get_context_window(session_id="s")
    assert current.summary == "published"
    assert [m.content for m in current.recent_messages] == ["new tail", "new answer"]


def test_real_worker_after_restart_summarizes_original_prefix_once_with_concurrent_turn(tmp_path):
    service, connect = _persistent_service(tmp_path)
    db_path = tmp_path / "sessions.sqlite3"
    store = BackgroundJobStore(db_path)
    store.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(
        db_path=str(db_path), memory=None, store=store, session_service=service,
    )
    user = "Restore the backup " + "diagnostic output " * 20 + "do not overwrite source."
    answer = "Restore remains open " + "notes " * 20
    inline_calls = []

    def slow_inline(*args):
        inline_calls.append(args)
        time.sleep(0.15)
        return "inline"

    service.record_context_exchange(
        session_id="s", user_input=user, agent_answer=answer, trace_id="original",
        token_budget=100, context_summarizer=slow_inline,
        background_enqueue=coordinator.enqueue_compaction,
    )
    assert inline_calls == []
    assert _state(connect)[:4] == (1, 3, 0, 0)
    # A single overflowing pair remains raw; the worker has no older prefix yet.
    assert coordinator.worker.run_one()
    assert _state(connect)[2:4] == (0, 0)
    service.record_context_exchange(
        session_id="s", user_input="second task", agent_answer="second answer", trace_id="second",
        token_budget=100, background_enqueue=coordinator.enqueue_compaction,
    )
    model_inputs = []

    class SlowModel:
        def complete_text(self, **kwargs):
            model_inputs.append(json.loads(kwargs["user_prompt"])["messages"])
            # A new raw turn during the model call must not invalidate the fixed prefix.
            service.record_context_exchange(
                session_id="s", user_input="newer task", agent_answer="newer answer", trace_id="newer",
                token_budget=100, context_summarizer=slow_inline,
                background_enqueue=coordinator.enqueue_compaction,
            )
            time.sleep(0.15)
            return SimpleNamespace(content='{"summary":"restore open; preserve source"}')

    restarted = SessionService(connect)
    restarted_store = BackgroundJobStore(db_path)
    restarted_store.ensure_schema()
    restarted_coordinator = MemoryBackgroundCoordinator(
        db_path=str(db_path), memory=None, store=restarted_store, session_service=restarted,
        llm_client=SlowModel(),
    )
    assert restarted_coordinator.worker.run_one()
    assert inline_calls == []
    assert len(model_inputs) == 1
    assert [m["content"] for m in model_inputs[0]] == [user, answer]
    assert _state(connect)[2:4] == (2, 1)
    raw = restarted.get_context_window(session_id="s")
    assert raw.summary_metadata["method"] == "model"
    assert "emergency_view" not in raw.summary_metadata
    assert [m.trace_id for m in raw.recent_messages] == ["second", "second", "newer", "newer"]
    before = _state(connect)
    bounded = restarted.get_prompt_context_window(session_id="s", token_budget=30)
    assert bounded.token_estimate <= 30
    assert _state(connect) == before
    # Process the continuation locally and show that the original model prefix is never replayed.
    restarted_coordinator.llm_client = None
    for _ in range(4):
        if not restarted_coordinator.worker.run_one():
            break
    assert not restarted_coordinator.worker.run_one()
    assert len(model_inputs) == 1
    assert _state(connect)[2] == 4
    assert [m.trace_id for m in restarted.get_context_window(session_id="s").recent_messages] == [
        "newer", "newer",
    ]


@pytest.mark.parametrize("queued", [False, True])
def test_no_transactional_queue_keeps_synchronous_summary_semantics(tmp_path, queued):
    service, _ = _persistent_service(tmp_path)
    calls = []
    queued_calls = []

    def summarize(summary, old, recent, budget):
        calls.append([m.content for m in old])
        return "sync summary"

    window = service.record_context_exchange(
        session_id="s", user_input="u" * 500, agent_answer="a" * 500, trace_id="sync",
        token_budget=40, context_summarizer=summarize,
        background_enqueue=(lambda s, r, t: queued_calls.append(t)) if queued else None,
    )
    assert window.summary == "sync summary"
    assert len(calls) == 1
    assert queued_calls == []


def test_manual_publication_invalidates_pending_worker_summary_cas(tmp_path):
    service, connect = _persistent_service(tmp_path)
    for i in range(3):
        service.record_context_exchange(
            session_id="s", user_input="user" * 100, agent_answer="answer" * 100,
            trace_id=str(i), token_budget=100, background_enqueue=_enqueue,
        )
    assert service.publish_context_summary(
        session_id="s", expected_revision=3, expected_covered_seq=0, target_seq=2,
        summary="manual summary", token_budget=100,
    )
    before = _state(connect)
    assert not service.publish_context_summary(
        session_id="s", expected_revision=3, expected_summary_revision=0,
        expected_covered_seq=0, target_seq=4, summary="stale worker", token_budget=100,
    )
    assert _state(connect) == before
    assert service.get_context_window(session_id="s").summary == "manual summary"
