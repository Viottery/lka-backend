from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.sessions import SessionService
from tests.test_compaction_retention_quality import _persistent_service


def _runtime(tmp_path, monkeypatch, *, fail_after_enqueue=False):
    service, connect = _persistent_service(tmp_path)
    store = BackgroundJobStore(tmp_path / "sessions.sqlite3")
    store.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(
        db_path=str(tmp_path / "sessions.sqlite3"), memory=None,
        store=store, session_service=service,
    )
    runtime = object.__new__(LocalKnowledgeAgentRuntime)
    runtime.memory_background = coordinator
    runtime.last_background_error = None
    original_enqueue = coordinator.enqueue_compaction

    def fail(*args, **kwargs):
        if fail_after_enqueue:
            original_enqueue(*args, **kwargs)
        raise RuntimeError("private queue diagnostic must not enter the payload")

    monkeypatch.setattr(coordinator, "enqueue_compaction", fail)
    return runtime, service, connect, store


def _overflow(runtime, service, *, token_budget=100):
    service.record_context_exchange(
        session_id="s", user_input="original fixed prefix", agent_answer="original answer",
        trace_id="original", token_budget=10_000,
    )

    def inline(*_args):
        pytest.fail("outbox recovery must not invoke the foreground summarizer")

    answer = "completed answer " * 40
    window = service.record_context_exchange(
        session_id="s", user_input="current task " * 40, agent_answer=answer,
        trace_id="current", effect_id="once", token_budget=token_budget,
        context_summarizer=inline, background_enqueue=runtime._enqueue_compaction,
    )
    service.append_message(session_id="s", role="agent", content=answer)
    return window, answer


@pytest.mark.parametrize("fail_after_enqueue", [False, True])
def test_runtime_enqueue_failure_preserves_answer_and_recovers_after_restart(
    tmp_path, monkeypatch, fail_after_enqueue,
):
    runtime, service, connect, store = _runtime(
        tmp_path, monkeypatch, fail_after_enqueue=fail_after_enqueue,
    )
    with connect() as conn:
        schema_before = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    window, answer = _overflow(runtime, service)
    assert window.token_estimate <= 100
    assert runtime.last_background_error == "RuntimeError"
    with connect() as conn:
        pending = conn.execute("SELECT * FROM background_pending_watermarks").fetchall()
        assert len(pending) == 1
        assert pending[0]["watermark_order"] == 4
        assert json.loads(pending[0]["payload_json"]) == {"revision": 2, "target_seq": 4}
        assert conn.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM agent_session_context_messages").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM agent_session_effects").fetchone()[0] == 1
        assert conn.execute("SELECT content FROM agent_session_messages").fetchone()[0] == answer
        assert tuple(conn.execute(
            "SELECT covered_seq, summary_revision FROM agent_session_context_state",
        ).fetchone()) == (0, 0)
    assert service.get_context_window(session_id="s").summary == ""
    model_inputs = []

    class Model:
        def complete_text(self, **kwargs):
            model_inputs.append(json.loads(kwargs["user_prompt"])["messages"])
            return SimpleNamespace(content='{"summary":"original task remains open"}')

    restarted = SessionService(connect)
    recovered = MemoryBackgroundCoordinator(
        db_path=str(tmp_path / "sessions.sqlite3"), memory=None,
        store=BackgroundJobStore(store.db_path), session_service=restarted, llm_client=Model(),
    )
    assert recovered.worker.run_one()
    assert [[m["content"] for m in messages] for messages in model_inputs] == [
        ["original fixed prefix", "original answer"],
    ]
    raw = restarted.get_context_window(session_id="s")
    assert raw.summary_metadata["covered_seq"] == 2
    assert [m.trace_id for m in raw.recent_messages] == ["current", "current"]
    with connect() as conn:
        # Session sidecar creation is existing behavior, not a recovery migration.
        schema_after = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'agent_session_context_%' "
            "AND name NOT LIKE 'sqlite_autoindex_agent_session_context_%' ORDER BY name",
        ).fetchall()
    assert [tuple(row) for row in schema_after] == [
        tuple(row) for row in schema_before
        if not row["name"].startswith((
            "agent_session_context_", "sqlite_autoindex_agent_session_context_",
        ))
    ]


def test_failed_outbox_recovers_by_live_worker_without_another_exchange(tmp_path, monkeypatch):
    runtime, service, connect, _ = _runtime(tmp_path, monkeypatch)
    _overflow(runtime, service)
    monkeypatch.undo()
    published = threading.Event()
    publish = service.publish_context_summary
    calls = []

    def observe_publication(**kwargs):
        calls.append((threading.get_ident(), kwargs["target_seq"]))
        result = publish(**kwargs)
        if result:
            published.set()
        return result

    monkeypatch.setattr(service, "publish_context_summary", observe_publication)
    coordinator = runtime.memory_background
    # This is the actual periodic worker loop, not a foreground repair scan.
    coordinator.start()
    try:
        assert published.wait(timeout=5)
    finally:
        coordinator.stop()
    assert len(calls) == 1
    assert calls[0][1] == 2
    assert calls[0][0] != threading.get_ident()
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_session_context_messages").fetchone()[0] == 4
    assert [m.trace_id for m in service.get_context_window(session_id="s").recent_messages] == [
        "current", "current",
    ]


def test_queue_full_defers_without_losing_completed_exchange(tmp_path, monkeypatch):
    runtime, service, connect, store = _runtime(tmp_path, monkeypatch)
    monkeypatch.undo()
    store.max_pending_jobs = 1
    blocker = store.enqueue("other_worker", "other_scope", "blocker", {"revision": 1})
    window, answer = _overflow(runtime, service)
    assert window.token_estimate <= 100
    assert runtime.last_background_error is None
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 1
        assert conn.execute("SELECT watermark_order FROM background_pending_watermarks").fetchone()[0] == 4
        assert conn.execute("SELECT content FROM agent_session_messages").fetchone()[0] == answer
    claimed = store.claim("other", 60, kinds=("other_worker",))
    assert claimed["job_id"] == blocker["job_id"]
    assert store.complete(claimed["job_id"], "other", claimed["lease_epoch"])
    assert runtime.memory_background.worker.run_one()
    assert service.get_context_window(session_id="s").summary_metadata["covered_seq"] == 2


def test_soft_due_failure_also_retains_intent(tmp_path, monkeypatch):
    runtime, service, connect, _ = _runtime(tmp_path, monkeypatch)
    window, _ = _overflow(runtime, service, token_budget=400)
    assert 280 <= window.token_estimate <= 400
    with connect() as conn:
        assert conn.execute("SELECT watermark_order FROM background_pending_watermarks").fetchone()[0] == 4
    monkeypatch.undo()
    assert runtime.memory_background.worker.run_one()
    assert service.get_context_window(session_id="s").summary_metadata["covered_seq"] == 2


@pytest.mark.parametrize("healthy", [False, True])
def test_no_recovery_intent_for_under_threshold_or_successful_enqueue(
    tmp_path, monkeypatch, healthy,
):
    runtime, service, connect, _ = _runtime(tmp_path, monkeypatch)
    if healthy:
        monkeypatch.undo()
        _overflow(runtime, service)
    else:
        service.record_context_exchange(
            session_id="s", user_input="small", agent_answer="answer", trace_id="small",
            token_budget=1000, background_enqueue=runtime._enqueue_compaction,
        )
    assert runtime.last_background_error is None
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM background_pending_watermarks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == int(healthy)


def test_recovery_intent_rolls_back_with_enclosing_exchange(tmp_path, monkeypatch):
    runtime, service, connect, _ = _runtime(tmp_path, monkeypatch)

    def abort_after_callback(session_id, revision, target_seq, *, conn):
        runtime._enqueue_compaction(session_id, revision, target_seq, conn=conn)
        raise ValueError("abort enclosing transaction")

    with pytest.raises(ValueError, match="enclosing transaction"):
        service.record_context_exchange(
            session_id="s", user_input="u" * 500, agent_answer="a" * 500,
            trace_id="aborted", effect_id="aborted", token_budget=100,
            background_enqueue=abort_after_callback,
        )
    with connect() as conn:
        for table in (
            "background_pending_watermarks", "background_watermark_heads", "background_jobs",
            "agent_session_context_windows", "agent_session_effects",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_repeated_failure_coalesces_and_stale_recovery_cannot_regress_watermark(
    tmp_path, monkeypatch,
):
    runtime, service, connect, store = _runtime(tmp_path, monkeypatch)
    _overflow(runtime, service)
    service.record_context_exchange(
        session_id="s", user_input="latest" * 100, agent_answer="latest answer" * 100,
        trace_id="latest", effect_id="latest", token_budget=100,
        background_enqueue=runtime._enqueue_compaction,
    )
    with connect() as conn:
        before = tuple(conn.execute("SELECT * FROM background_pending_watermarks").fetchone())
        runtime.memory_background.recover_missing_compaction("s", 2, 4, conn=conn)
        assert tuple(conn.execute("SELECT * FROM background_pending_watermarks").fetchone()) == before
        assert json.loads(conn.execute(
            "SELECT payload_json FROM background_pending_watermarks",
        ).fetchone()[0]) == {"revision": 3, "target_seq": 6}
    # Idempotent effect replay neither appends originals nor creates a second intent.
    service.record_context_exchange(
        session_id="s", user_input="ignored", agent_answer="ignored", trace_id="latest",
        effect_id="latest", token_budget=100, background_enqueue=runtime._enqueue_compaction,
    )
    restarted = MemoryBackgroundCoordinator(
        db_path=store.db_path, memory=None, store=BackgroundJobStore(store.db_path),
        session_service=SessionService(connect),
    )
    assert restarted.worker.run_one()
    raw = restarted.session_service.get_context_window(session_id="s")
    assert raw.summary_metadata["covered_seq"] == 4
    assert [m.trace_id for m in raw.recent_messages] == ["latest", "latest"]
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_session_context_messages").fetchone()[0] == 6


def test_concurrent_runtime_failures_and_replays_keep_latest_durable_intent(tmp_path, monkeypatch):
    runtime, service, connect, _ = _runtime(tmp_path, monkeypatch)

    def record(index):
        trace_id = f"turn-{index % 4}"
        return service.record_context_exchange(
            session_id="s", user_input=trace_id + "u" * 500,
            agent_answer=trace_id + "a" * 500, trace_id=trace_id, effect_id=trace_id,
            token_budget=20, background_enqueue=runtime._enqueue_compaction,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        windows = list(pool.map(record, range(12)))
    assert all(window.token_estimate <= 20 for window in windows)
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_session_effects").fetchone()[0] == 4
        assert [row[0] for row in conn.execute(
            "SELECT seq FROM agent_session_context_messages ORDER BY seq",
        )] == list(range(1, 9))
        assert [row[0] for row in conn.execute(
            "SELECT watermark_order FROM background_pending_watermarks",
        )] == [8]
        assert conn.execute("SELECT watermark_order FROM background_watermark_heads").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 0


@pytest.mark.parametrize("enabled,background_enabled", [(False, False), (False, True), (True, False)])
def test_disabled_runtime_does_not_start_recovery_or_process_pending_history(
    tmp_path, monkeypatch, enabled, background_enabled,
):
    runtime, service, connect, _ = _runtime(tmp_path, monkeypatch)
    _overflow(runtime, service)
    runtime.local_app_config = SimpleNamespace(
        memory=SimpleNamespace(enabled=enabled, background_enabled=background_enabled),
        message_history=SimpleNamespace(enabled=False, background_enabled=False),
    )
    runtime._run_startup_mail_sync = lambda: None
    runtime._start_background_mail_sync = lambda: None
    runtime.watch_scheduler = SimpleNamespace(start=lambda: None)

    def unexpected_start():
        pytest.fail("disabled memory/background must not start a recovery worker")

    monkeypatch.setattr(runtime.memory_background, "start", unexpected_start)
    runtime.start()
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM background_pending_watermarks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT covered_seq FROM agent_session_context_state").fetchone()[0] == 0
