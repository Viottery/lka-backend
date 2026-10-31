from __future__ import annotations

from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunStatus
from tests.test_session_input_queue import runtime as base_runtime
from tests.test_session_input_queue import sid, submit


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from base_runtime.__wrapped__(tmp_path, monkeypatch)


def test_queue_observation_includes_active_root_without_queue_row(runtime):
    session_id = sid(runtime)
    run = runtime.create_agent_run(session_id=session_id, user_input="Synthetic active root")
    runtime.agent_run_manager.mark_running(run.run_id)

    with closing(runtime._conn()) as conn:
        observation = runtime.session_inputs._queue_observation(conn, session_id)

    assert observation["state"] is None
    assert observation["summary"] == {
        "paused": False,
        "pending_count": 0,
        "active_run_id": run.run_id,
    }
    assert observation["watched_user_run_id"] == run.run_id


def test_background_failed_watch_does_not_pause_user_queue(runtime):
    session_id = sid(runtime)
    run = runtime.create_agent_run(session_id=session_id, user_input="Synthetic background run")
    runtime.agent_run_manager._update_run(
        run.run_id,
        status=AgentRunStatus.FAILED,
        metadata_patch={"input_envelope": {"origin": "scheduler"}},
    )
    with closing(runtime._conn()) as conn, conn:
        conn.execute(
            "INSERT INTO session_input_queues(session_id,paused,watched_run_id,next_sequence) "
            "VALUES(?,0,?,4)",
            (session_id, run.run_id),
        )

    with closing(runtime._conn()) as conn:
        observation = runtime.session_inputs._queue_observation(conn, session_id)
    assert observation["summary"]["paused"] is False
    assert observation["summary"]["active_run_id"] is None


def test_failed_user_watch_pauses_queue_and_submit_uses_observed_sequence(runtime):
    session_id = sid(runtime)
    run = runtime.create_agent_run(session_id=session_id, user_input="Synthetic failed user run")
    runtime.agent_run_manager._update_run(
        run.run_id,
        status=AgentRunStatus.FAILED,
        metadata_patch={"session_input_id": "synthetic-input"},
    )
    with closing(runtime._conn()) as conn, conn:
        conn.execute(
            "INSERT INTO session_input_queues(session_id,paused,watched_run_id,next_sequence) "
            "VALUES(?,0,?,9)",
            (session_id, run.run_id),
        )

    with closing(runtime._conn()) as conn:
        observation = runtime.session_inputs._queue_observation(conn, session_id)
    assert observation["summary"]["paused"] is True

    accepted = submit(runtime, session_id, "after-watch", "New user input")
    assert accepted["input"]["sequence"] == 9
    with closing(runtime._conn()) as conn:
        queue = conn.execute(
            "SELECT next_sequence,paused,watched_run_id FROM session_input_queues WHERE session_id=?",
            (session_id,),
        ).fetchone()
    assert queue["next_sequence"] == 10
    assert queue["paused"] == 1
    assert queue["watched_run_id"] == run.run_id


def test_observation_keeps_public_queue_summary_shape(runtime):
    session_id = sid(runtime)
    with closing(runtime._conn()) as conn:
        summary = runtime.session_inputs._queue_summary(conn, session_id)
    assert set(summary) == {"paused", "pending_count", "active_run_id"}
