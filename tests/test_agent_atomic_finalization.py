"""Generic atomic publication helpers and the optional graph finalization hook."""
import asyncio
import json
from contextlib import closing

import pytest

from app.core.agent_graph import AgentTurnPaused
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.llm_workloads import current_workload, workload_scope
from app.core.sessions import SessionService
from app.storage.db import connect
from tests.test_unified_entry_quality import operation, runtime_for


@pytest.fixture
def publication(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "publication.sqlite3")
    factory = lambda: connect(store.db_path)
    sessions = SessionService(factory)
    session = sessions.create_session(title="Atomic publication").session
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id=session.session_id, user_input="Inspect")
    manager.mark_running(run.run_id)
    with closing(factory()) as conn, conn:
        sessions._ensure_context_state_table(conn)
        conn.execute("CREATE TABLE publication_outbox(message_id TEXT PRIMARY KEY)")
    yield store, factory, sessions, manager, run
    manager.close()


def publish(conn, sessions, manager, run, *, input_role="tool", callback=None, final_answer_event=None):
    window = sessions.record_context_exchange_in_transaction(
        conn, session_id=run.session_id, user_input="Delegated input", input_role=input_role,
        agent_answer="Published answer", trace_id=run.trace_id, effect_id="exchange", token_budget=1000,
    )
    sessions.append_message_in_transaction(
        conn, session_id=run.session_id, role="agent", content="Published answer", message_id="answer",
        payload={"run_id": run.run_id}, persisted_message_callback=callback,
    )
    completed = manager.complete_run_in_transaction(conn, run.run_id,
                                                   result_snapshot={"answer": "Published answer"}, log_path="local-log",
                                                   final_answer_event=final_answer_event)
    conn.execute("INSERT OR IGNORE INTO publication_outbox VALUES('answer')")
    return window, completed


def test_publication_rollback_leaves_context_message_run_event_slot_and_outbox_unchanged(publication):
    store, factory, sessions, manager, run = publication
    manager.flush_events()
    with pytest.raises(OSError), closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _, completed = publish(conn, sessions, manager, run)
        assert completed.status == AgentRunStatus.COMPLETED
        assert manager._runs[run.run_id].status == AgentRunStatus.RUNNING
        raise OSError("Crash before business commit")
    assert store.load_run(run.run_id)["status"] == "running"
    with closing(factory()) as conn:
        for table in ("agent_session_messages", "agent_session_context_messages", "agent_session_effects", "publication_outbox"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT run_id FROM session_admissions WHERE session_id=?", (run.session_id,)).fetchone()[0] == run.run_id
        assert conn.execute("SELECT count(*) FROM agent_run_events WHERE json_extract(event_payload,'$.type')='run_completed'").fetchone()[0] == 0


def test_publication_replays_once_and_cache_refresh_waits_for_commit(publication):
    store, factory, sessions, manager, run = publication
    callbacks = []

    def callback(conn, message):
        assert conn.in_transaction
        callbacks.append(message.message_id)

    manager.flush_events()
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        window, completed = publish(conn, sessions, manager, run, callback=callback)
        replay_window, replay_completed = publish(conn, sessions, manager, run, callback=callback)
        assert replay_window == window and replay_completed == completed
        assert manager._runs[run.run_id].status == AgentRunStatus.RUNNING
    assert callbacks == ["answer"]
    manager.refresh_after_transaction(completed)
    assert manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
    assert [message.role for message in window.recent_messages] == ["tool", "agent"]
    with closing(factory()) as conn:
        assert conn.execute("SELECT count(*) FROM agent_session_context_messages").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM agent_session_messages").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM publication_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT run_id FROM session_admissions WHERE session_id=?", (run.session_id,)).fetchone()[0] is None
    assert len([event for event in manager.list_events(run.run_id) if event.type == "run_completed"]) == 1
    assert store.load_run(run.run_id)["result_snapshot"] == {"answer": "Published answer"}


@pytest.mark.parametrize("state", ["cancel_requested", "pause_requested", "paused", "waiting_user"])
def test_cancel_or_blocked_run_cannot_publish_and_partial_effects_roll_back(publication, state):
    store, factory, sessions, manager, run = publication
    if state == "cancel_requested":
        manager.request_cancel(run.run_id)
    elif state == "pause_requested":
        manager.request_pause(run.run_id)
    else:
        manager._update_run(run.run_id, status=AgentRunStatus(state))
    before = store.load_run(run.run_id)
    with pytest.raises(ValueError), closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        publish(conn, sessions, manager, run)
    assert store.load_run(run.run_id) == before
    assert sessions.get_session(session_id=run.session_id).messages == []


def test_context_helper_preserves_legacy_summary_and_full_raw_tail_on_overflow(publication, monkeypatch):
    _, factory, sessions, _, run = publication
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO agent_session_context_windows VALUES(?,?,?,?,?,?)",
                     (run.session_id, 20, "Earlier summary", json.dumps([
                         {"role": "user", "content": "Earlier raw", "created_at": "then", "trace_id": "earlier"}
                     ]), 1, "then"))
    monkeypatch.setattr(sessions, "_fit_context_window", lambda **kwargs: (_ for _ in ()).throw(AssertionError("No sync compaction")))
    queues = []

    def enqueue(session_id, revision, seq, *, conn):
        assert conn.in_transaction
        queues.append((session_id, revision, seq))

    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        kwargs = {"session_id": run.session_id, "user_input": "x" * 5000, "input_role": "tool", "agent_answer": "Answer",
                  "trace_id": run.trace_id, "effect_id": "overflow", "token_budget": 20, "background_enqueue": enqueue}
        first = sessions.record_context_exchange_in_transaction(conn, **kwargs)
        second = sessions.record_context_exchange_in_transaction(conn, **kwargs)
        assert first == second
        assert first.summary == "Earlier summary" and len(first.recent_messages) == 3
        assert first.recent_messages[1].content == "x" * 5000
        assert first.token_estimate > first.token_budget
        assert len(queues) == 1
        assert conn.execute("SELECT covered_seq FROM agent_session_context_state").fetchone()[0] == 0


def test_message_id_conflict_and_callback_failure_roll_back(publication):
    _, factory, sessions, _, run = publication

    def fail(conn, message):
        conn.execute("INSERT INTO publication_outbox VALUES(?)", (message.message_id,))
        raise OSError("Callback failed")

    with pytest.raises(OSError), closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        sessions.append_message_in_transaction(conn, session_id=run.session_id, role="agent", content="One", message_id="same", persisted_message_callback=fail)
    assert sessions.get_session(session_id=run.session_id).messages == []
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        sessions.append_message_in_transaction(conn, session_id=run.session_id, role="agent", content="One", message_id="same")
        with pytest.raises(ValueError, match="different content"):
            sessions.append_message_in_transaction(conn, session_id=run.session_id, role="agent", content="Two", message_id="same")


def install_handler(runtime, *, fail_after_commit=False, fail_before_commit=False):
    sessions, manager = runtime.session_service, runtime.agent_run_manager
    calls = []

    def handler(run_id, result, completed_snapshot, **kwargs):
        assert not manager.get_run(run_id).metadata.get("completion_claimed")
        assert not any(message.role == "agent" for message in sessions.get_session(session_id=result.session_id).messages)
        if manager.get_run(run_id).metadata.get("defer_answer_publication"):
            assert not any(event.type == "final_answer" for event in manager.list_events(run_id))
        manager.flush_events()
        with closing(runtime._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            window = sessions.record_context_exchange_in_transaction(
                conn, session_id=result.session_id, user_input=kwargs["user_input"], input_role=kwargs["input_role"],
                agent_answer=result.answer, trace_id=result.trace_id, effect_id=kwargs["context_effect_id"],
                token_budget=kwargs["token_budget"], background_enqueue=kwargs["background_enqueue"],
            )
            payload = {**kwargs["message_payload"], "context_window": {"token_estimate": window.token_estimate}}
            sessions.append_message_in_transaction(conn, session_id=result.session_id, role="agent", content=result.answer,
                                                    message_id=kwargs["message_id"], payload=payload)
            final_answer = next(event for event in reversed(result.progress_events) if event.type == "final_answer")
            manager.complete_run_in_transaction(conn, run_id, result_snapshot=completed_snapshot, log_path=result.log_path,
                                                 final_answer_event=final_answer.model_dump(mode="json"))
            if fail_before_commit:
                raise OSError("Business publication fence failed before commit")
        calls.append(run_id)
        if fail_after_commit:
            raise OSError("Graph checkpoint not yet committed")
        return True

    runtime.agent_turn_loop.finalization_handler = handler
    return calls


@pytest.mark.parametrize("hook", ["absent", "declines", "handles"])
def test_optional_hook_and_ordinary_finalization_both_publish_once(tmp_path, monkeypatch, hook):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [operation("final_answer", reason="Just answer"), "Final answer"])
    try:
        calls = install_handler(runtime) if hook == "handles" else []
        if hook == "absent":
            runtime.agent_turn_loop.finalization_handler = None
        elif hook == "declines":
            runtime.agent_turn_loop.finalization_handler = lambda *args, **kwargs: False
        result = runtime.run_agent_turn(session_id="hook", user_input="Answer")
        assert result.answer == "Final answer" and len(client.calls) == 2
        assert calls == ([result.run_id] if hook == "handles" else [])
        assert len([message for message in runtime.get_session(session_id="hook").messages if message.role == "agent"]) == 1
        assert len([event for event in runtime.agent_run_manager.list_events(result.run_id) if event.type == "run_completed"]) == 1
    finally:
        runtime.stop()


def test_committed_result_survives_missing_final_graph_checkpoint_and_restart(tmp_path, monkeypatch):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [operation("final_answer", reason="Just answer"), "Committed answer"])
    install_handler(runtime, fail_after_commit=True)
    try:
        result = runtime.run_agent_turn(session_id="committed", user_input="Answer")
        assert result.answer == "Committed answer" and len(client.calls) == 2
        assert runtime.agent_run_manager.get_run(result.run_id).status == AgentRunStatus.COMPLETED
        assert runtime.agent_turn_runner.get_state(result.run_id).values.get("phase") != "finalized"
    finally:
        runtime.stop()
    reopened, recovered_client = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    try:
        monkeypatch.setattr(reopened.agent_turn_runner, "get_state", lambda _: (_ for _ in ()).throw(AssertionError("Terminal artifact must not require a graph checkpoint")))
        assert reopened.resume_agent_run(result.run_id).answer == "Committed answer"
        assert recovered_client.calls == []
        assert len([message for message in reopened.get_session(session_id="committed").messages if message.role == "agent"]) == 1
    finally:
        reopened.stop()


def test_final_answer_and_completion_events_commit_or_roll_back_together(publication):
    store, factory, sessions, manager, run = publication
    manager._update_run(run.run_id, status=AgentRunStatus.RUNNING, metadata_patch={"defer_answer_publication": True})
    progress = {"type": "final_answer", "stage": "answer", "status": "completed", "message": "Published answer",
                "metadata": {"answer": "Published answer"}}
    manager.flush_events()
    with pytest.raises(ValueError, match="requires its final_answer event"), closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        publish(conn, sessions, manager, run)
    with pytest.raises(OSError), closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        publish(conn, sessions, manager, run, final_answer_event=progress)
        assert [json.loads(row[0])["type"] for row in conn.execute("SELECT event_payload FROM agent_run_events ORDER BY sequence")] == ["final_answer", "run_completed"]
        raise OSError("Roll back canonical final answer")
    assert store.list_events(run.run_id) == []
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _, completed = publish(conn, sessions, manager, run, final_answer_event=progress)
        publish(conn, sessions, manager, run, final_answer_event=progress)
    manager.refresh_after_transaction(completed)
    events = manager.list_events(run.run_id)
    assert [event.type for event in events] == ["final_answer", "run_completed"]
    assert events[0].event_id == f"{run.run_id}_final_answer"
    assert events[0].sequence + 1 == events[1].sequence
    assert events[0].payload == {"tool_name": None, "package_name": None, "status": "completed", "metadata": {"answer": "Published answer"}}


@pytest.mark.parametrize("failure", ["no_handler", "declines", "fence", "rollback"])
def test_deferred_answer_never_publishes_when_fence_or_hook_fails(tmp_path, monkeypatch, failure):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [operation("final_answer", reason="Answer"), "Unpublished answer"])
    run = runtime.create_agent_run(session_id="deferred", user_input="Answer")
    runtime.agent_run_manager._update_run(run.run_id, status=run.status, metadata_patch={"defer_answer_publication": True})
    if failure == "no_handler":
        runtime.agent_turn_loop.finalization_handler = None
    elif failure == "declines":
        runtime.agent_turn_loop.finalization_handler = lambda *args, **kwargs: False
    elif failure == "fence":
        def reject(*args, **kwargs):
            raise ValueError("Publication fence lost")
        runtime.agent_turn_loop.finalization_handler = reject
    else:
        install_handler(runtime, fail_before_commit=True)
    try:
        expected_error = "Deferred answer publication" if failure in {"no_handler", "declines"} else "fence"
        with pytest.raises((RuntimeError, ValueError, OSError), match=expected_error):
            runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        assert len(client.calls) == 2
        assert not any(event.type in {"final_answer", "run_completed"} for event in runtime.agent_run_manager.list_events(run.run_id))
        assert not any(message.role == "agent" for message in runtime.get_session(session_id=run.session_id).messages)
        with closing(runtime._conn()) as conn:
            assert conn.execute("SELECT count(*) FROM agent_session_effects WHERE session_id=?", (run.session_id,)).fetchone()[0] == 0
    finally:
        runtime.stop()


def test_deferred_answer_is_present_in_result_and_atomic_durable_events(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [operation("final_answer", reason="Answer"), "Committed final answer"])
    run = runtime.create_agent_run(session_id="deferred-success", user_input="Answer")
    runtime.agent_run_manager._update_run(run.run_id, status=run.status, metadata_patch={"defer_answer_publication": True})
    install_handler(runtime)
    try:
        result = runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        assert [event for event in result.progress_events if event.type == "final_answer"][-1].metadata["answer"] == result.answer
        events = [event for event in runtime.agent_run_manager.list_events(run.run_id) if event.type in {"final_answer", "run_completed"}]
        assert [event.type for event in events] == ["final_answer", "run_completed"]
        assert events[0].payload["metadata"]["answer"] == result.answer
        assert events[0].event_id == f"{run.run_id}_final_answer"
    finally:
        runtime.stop()


@pytest.mark.parametrize("resume", [False, True])
def test_async_graph_worker_preserves_workload_context(tmp_path, monkeypatch, resume):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [operation("final_answer", reason="Just answer"), "Scoped answer"])
    captured = []
    original = client.complete_text

    def scoped_client(**kwargs):
        captured.append((current_workload().pool, current_workload().task_id))
        return original(**kwargs)

    monkeypatch.setattr(client, "complete_text", scoped_client)
    run = runtime.create_agent_run(session_id="async-context", user_input="Answer")
    try:
        if resume:
            runtime.interrupt_agent_run(run.run_id)
            with pytest.raises(AgentTurnPaused):
                runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        with workload_scope("background_io", task_id="stable-execution"):
            call = runtime.agent_turn_runner.resume_async(run.run_id) if resume else runtime.agent_turn_runner.run_async(
                session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
            assert asyncio.run(call).answer == "Scoped answer"
        assert captured == [("background_io", "stable-execution")] * 2
    finally:
        runtime.stop()
