from contextlib import closing
from types import SimpleNamespace

import pytest

from app.core.agent_finalization import publish_root_result
from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import AgentTurnProgressEvent
from app.core.sessions import SessionService
from app.storage.db import connect
from app.storage.mysql import MySQLCommitOutcomeUnknown


@pytest.fixture
def publication(tmp_path):
    path = tmp_path / "publication.sqlite3"
    store = SqliteAgentRunStore(path)
    manager = InMemoryAgentRunManager(durable_store=store)
    sessions = SessionService(lambda: connect(path))
    session = sessions.ensure_session(session_id="synthetic-publication")
    sessions.prepare_context_schema()
    run = manager.create_run(session_id=session.session_id, user_input="synthetic")
    manager.mark_running(run.run_id)
    result = SimpleNamespace(answer="Saved once.", log_path=None, progress_events=[
        AgentTurnProgressEvent(event_index=1, created_at=run.created_at, type="final_answer",
                               stage="answer", status="completed", message="Saved once.",
                               metadata={"answer": "Saved once."}),
    ])
    kwargs = {"manager": manager, "sessions": sessions, "run_id": run.run_id, "result": result,
              "snapshot": {"session_id": run.session_id, "trace_id": run.trace_id, "answer": result.answer},
              "user_input": "synthetic", "input_role": "user", "message_payload": {"run_id": run.run_id},
              "token_budget": 1000}
    try:
        yield path, manager, sessions, run, kwargs
    finally:
        manager.close()


def test_atomic_publication_commits_answer_terminal_event_and_context_once(publication):
    path, manager, _sessions, run, kwargs = publication
    callbacks = []
    kwargs["message_callback"] = lambda _conn, msg: callbacks.append(msg.message_id)
    publish_root_result(**kwargs)
    publish_root_result(**kwargs)
    assert callbacks == [f"agent_graph_agent_{run.run_id}"]
    assert manager.get_run(run.run_id).status.value == "completed"
    assert [e.type for e in manager.list_events(run.run_id)].count("run_completed") == 1
    assert [e.type for e in manager.list_events(run.run_id)].count("final_answer") == 1
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM agent_session_effects").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM agent_session_messages WHERE role='agent'").fetchone()[0] == 1


def test_failed_completion_rolls_back_answer_context_and_outbox(publication, monkeypatch):
    path, manager, _sessions, run, kwargs = publication

    def reject(*_args, **_kwargs):
        raise ValueError("synthetic terminal persistence failure")

    monkeypatch.setattr(manager, "complete_run_in_transaction", reject)
    with pytest.raises(ValueError, match="terminal persistence"):
        publish_root_result(**kwargs)
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM agent_session_messages WHERE role='agent'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM agent_session_effects").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM agent_session_context_messages").fetchone()[0] == 0
    assert manager.get_run(run.run_id).status.value == "running"
    assert not any(e.type in {"final_answer", "run_completed"} for e in manager.list_events(run.run_id))


@pytest.mark.parametrize("actually_committed", [True, False])
def test_unknown_commit_checks_receipts_without_repeating_writes(publication, actually_committed):
    path, manager, sessions, run, kwargs = publication
    factory = sessions._conn_factory
    commits = []
    callbacks = []
    kwargs["message_callback"] = lambda _conn, _msg: callbacks.append("outbox")

    class AmbiguousConnection:
        def __init__(self):
            self.connection = factory()

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            commits.append("commit")
            if actually_committed:
                self.connection.commit()
            else:
                self.connection.rollback()
            self.connection.close()
            raise MySQLCommitOutcomeUnknown("synthetic acknowledgement lost")

    first = True

    def connections():
        nonlocal first
        if first:
            first = False
            return AmbiguousConnection()
        return factory()

    sessions._conn_factory = connections
    if actually_committed:
        publish_root_result(**kwargs)
        assert manager.get_run(run.run_id).status.value == "completed"
    else:
        with pytest.raises(MySQLCommitOutcomeUnknown):
            publish_root_result(**kwargs)
        assert manager.get_run(run.run_id).status.value == "running"
    assert callbacks == ["outbox"] and commits == ["commit"]
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM agent_session_messages WHERE role='agent'").fetchone()[0] == int(actually_committed)


@pytest.mark.parametrize("pause_before_commit", [False, True])
def test_graph_publishes_final_answer_and_completed_after_atomic_commit(tmp_path, monkeypatch, pause_before_commit):
    import json

    from tests.test_unified_entry_quality import operation, runtime_for

    review = json.dumps({"faithful": True, "hallucination_free": True, "request_aligned": True,
                         "decision": "pass", "issues": [], "revision_instructions": "", "evidence_gap": ""})
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("final_answer", reason="No external evidence needed."), "Saved once.", review,
    ])
    try:
        if pause_before_commit:
            from app.core.agent_graph import AgentTurnPaused

            manager = runtime.agent_run_manager
            claim = manager.claim_completion
            paused_ids = []

            def pause_first(run_id):
                if not paused_ids:
                    paused_ids.append(run_id)
                    manager.request_pause(run_id)
                return claim(run_id)

            monkeypatch.setattr(manager, "claim_completion", pause_first)
            with pytest.raises(AgentTurnPaused):
                runtime.run_agent_turn(session_id="atomic-graph", user_input="Just say hello.")
            paused = manager.get_run(paused_ids[0])
            assert paused.status.value == "paused" and not paused.metadata.get("completion_claimed")
            assert not any(e.type in {"run_failed", "final_answer"} for e in manager.list_events(paused.run_id))
            result = runtime.resume_agent_run(paused.run_id)
        else:
            result = runtime.run_agent_turn(session_id="atomic-graph", user_input="Just say hello.")
        assert result.answer == "Saved once."
        events = runtime.agent_run_manager.list_events(result.run_id)
        assert [event.type for event in events][-2:] == ["final_answer", "run_completed"]
        assert runtime.agent_run_manager.get_run(result.run_id).status.value == "completed"
        assert len(client.calls) == 3
        with closing(runtime._conn()) as conn:
            row = conn.execute("SELECT content FROM agent_session_messages WHERE message_id=?",
                               ("agent_graph_agent_" + result.run_id,)).fetchone()
            assert row["content"] == "Saved once."
    finally:
        runtime.stop()
