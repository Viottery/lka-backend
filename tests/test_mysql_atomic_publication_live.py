"""Real MySQL commits with a synthetic lost acknowledgement; no external LLM."""
from contextlib import closing
from uuid import uuid4

import pytest

from app.storage.mysql import MySQLCommitOutcomeUnknown
from tests.test_mysql_runtime_live import SYNTHETIC_REVIEW_PASS
from tests.test_mysql_runtime_live import runtime as mysql_runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from mysql_runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def test_graph_confirms_lost_commit_ack_without_repeating_turn(runtime):
    session = runtime.create_session(title="Synthetic commit ack " + uuid4().hex).session
    client = ScriptedClient([
        operation("final_answer", reason="Synthetic atomic publication"),
        "SYNTHETIC_ACK_REPLY", SYNTHETIC_REVIEW_PASS,
    ])
    runtime.agent_turn_loop.llm_client = client
    original = runtime.session_service._conn_factory
    acknowledgements_lost = []

    def connection():
        conn = original()
        execute, commit = conn.execute, conn.commit
        publishes_answer = False

        def track(statement, parameters=()):
            nonlocal publishes_answer
            if (statement.lstrip().startswith("INSERT INTO agent_session_messages")
                    and parameters and str(parameters[0]).startswith("agent_graph_agent_")):
                publishes_answer = True
            return execute(statement, parameters)

        def lose_ack():
            commit()
            if publishes_answer and not acknowledgements_lost:
                acknowledgements_lost.append(True)
                conn.close()
                raise MySQLCommitOutcomeUnknown("Synthetic lost commit acknowledgement")

        conn.execute, conn.commit = track, lose_ack
        return conn

    runtime.session_service._conn_factory = connection
    result = runtime.run_agent_turn(session_id=session.session_id, user_input="Synthetic reply please.")
    assert result.answer == "SYNTHETIC_ACK_REPLY"
    assert acknowledgements_lost == [True] and len(client.calls) == 3
    assert runtime.agent_run_manager.get_run(result.run_id).status.value == "completed"
    events = runtime.agent_run_manager.list_events(result.run_id)
    assert [e.type for e in events].count("run_completed") == 1
    assert not any(e.type == "run_failed" for e in events)
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_session_messages WHERE message_id=?",
                            ("agent_graph_agent_" + result.run_id,)).fetchone()[0] == 1
