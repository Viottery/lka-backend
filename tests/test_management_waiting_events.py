"""Waiting reminders carry references, never answers or tool approval authority."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from app.experts.management import ManagementCoordinator
from tests.test_management_goals import state as state_fixture
from tests.test_management_goals import task


@pytest.fixture
def state(tmp_path):
    original = state_fixture.__wrapped__(tmp_path)
    with closing(original[0]()) as conn, conn:
        conn.execute("CREATE TABLE managed_session_links(manager_session_id TEXT,task_session_id TEXT,run_id TEXT,command_id TEXT,created_at TEXT)")
    return original


def link(state, run_id, task_sid, *, owner=None):
    factory, _, _, _, sid = state
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO managed_session_links VALUES(?,?,?,?,?)", (owner or sid, task_sid, run_id, run_id, "now"))


def change(state, run_id, *, status=None, **metadata):
    with closing(state[0]()) as conn, conn:
        row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        record = json.loads(row[0])
        record["metadata"].update(metadata)
        if status is not None:
            record["status"] = status
        conn.execute("UPDATE agent_runs SET status=?,record_payload=? WHERE run_id=?", (record["status"], json.dumps(record), run_id))


@pytest.mark.parametrize("status", ["waiting_user", "waiting_confirmation"])
def test_capture_once_reference_only_and_next_manager_round_can_consume(state, status):
    factory, _, _, goals, sid = state
    run_id, task_sid = task(state, status=status)
    link(state, run_id, task_sid)
    wait_goal = goals.create_goal(sid, objective="Wait for task terminal", wait_run_id=run_id)
    if status == "waiting_user":
        change(state, run_id, pending_user_question={"question_id": "question-one", "question": "PRIVATE_QUESTION"})
        expected_ref = {"question_id": "question-one"}
    else:
        change(state, run_id, confirmation_id="review-one")
        with closing(factory()) as conn, conn:
            conn.execute("INSERT INTO agent_safety_reviews VALUES(?,?, 'pending', ?, 'now','now')",
                         ("review-one", run_id, json.dumps({"tool_input": "PRIVATE_TOOL_INPUT"})))
        expected_ref = {"review_id": "review-one"}
    assert goals.capture_events() == 1
    assert goals.capture_events() == 0
    event = goals.list_events(sid)["events"][0]
    assert event["kind"] == "timer" and event["goal_id"] is None
    assert event["payload"] == {"task_waiting": True, "run_id": run_id, "status": status, **expected_ref}
    assert "PRIVATE" not in json.dumps(event)
    assert goals.get_goal(sid, wait_goal["goal_id"])["status"] == "active"
    assert goals.get_goal(sid, wait_goal["goal_id"])["wait_run_id"] == run_id
    coordinator = ManagementCoordinator.__new__(ManagementCoordinator)
    coordinator.goals = goals
    coordinator.config = SimpleNamespace(max_causal_rounds=6, autonomous_enabled=True)
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        assert goals.waiting_event_current(conn, event)
        assert [e["event_id"] for e in coordinator._pending(conn, sid)] == [event["event_id"]]
        conn.execute("UPDATE manager_inbox SET consumed_execution_id='published-next-round' WHERE event_id=?", (event["event_id"],))
    assert goals.capture_events() == 0
    with closing(factory()) as conn:
        assert conn.execute("SELECT status FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()[0] == status


def test_question_change_answer_cancel_and_status_transition_invalidate_old_event(state):
    factory, _, _, goals, sid = state
    run_id, task_sid = task(state, status="waiting_user")
    link(state, run_id, task_sid)
    change(state, run_id, pending_user_question={"question_id": "q1", "question": "PRIVATE"})
    assert goals.capture_events() == 1
    first = goals.list_events(sid)["events"][0]
    change(state, run_id, pending_user_question={"question_id": "q2", "question": "PRIVATE"})
    with closing(factory()) as conn:
        assert not goals.waiting_event_current(conn, first)
    assert goals.capture_events() == 1
    second = goals.list_events(sid)["events"][1]
    change(state, run_id, pending_user_answer_command_id="answer-accepted")
    with closing(factory()) as conn:
        assert not goals.waiting_event_current(conn, second)
    assert goals.capture_events() == 0
    change(state, run_id, pending_user_answer_command_id=None, cancel_requested=True)
    with closing(factory()) as conn:
        assert not goals.waiting_event_current(conn, second)
    assert goals.capture_events() == 0
    change(state, run_id, status="running", cancel_requested=False)
    with closing(factory()) as conn:
        assert not goals.waiting_event_current(conn, second)
    assert goals.capture_events() == 0


def test_unlinked_children_foreign_and_deleted_sessions_are_not_captured_or_revived(state):
    factory, _, management, goals, sid = state
    unlinked, _ = task(state, status="waiting_user")
    child, child_sid = task(state, status="waiting_user", parent_run_id="parent")
    link(state, child, child_sid)
    foreign, foreign_sid = task(state, status="waiting_user")
    other = management.create()["session"]["session_id"]
    link(state, foreign, foreign_sid, owner=other)
    run_id, task_sid = task(state, status="waiting_user")
    link(state, run_id, task_sid)
    assert goals.capture_events() == 2  # Each manager sees only its own linked ordinary root.
    event = goals.list_events(sid)["events"][0]
    assert event["payload"]["run_id"] == run_id
    assert event["payload"]["run_id"] not in {unlinked, child, foreign}
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (task_sid,))
        assert not goals.waiting_event_current(conn, event)
    assert goals.capture_events() == 0
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='active' WHERE session_id=?", (task_sid,))
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (sid,))
        assert not goals.waiting_event_current(conn, event)
    assert goals.capture_events() == 0


def test_changed_review_invalidates_pending_and_generic_confirmation_is_bounded(state):
    factory, _, _, goals, sid = state
    run_id, task_sid = task(state, status="waiting_confirmation")
    link(state, run_id, task_sid)
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO agent_safety_reviews VALUES('r1',?,'pending','{}','one','one')", (run_id,))
    assert goals.capture_events() == 1
    event = goals.list_events(sid)["events"][0]
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE agent_safety_reviews SET status='approved' WHERE review_id='r1'")
        assert not goals.waiting_event_current(conn, event)
    assert goals.capture_events() == 0
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO agent_safety_reviews VALUES('r2',?,'pending','{}','two','two')", (run_id,))
    assert goals.capture_events() == 1
    generic, generic_sid = task(state, status="waiting_confirmation")
    link(state, generic, generic_sid)
    oversized, oversized_sid = task(state, status="waiting_user")
    link(state, oversized, oversized_sid)
    change(state, oversized, pending_user_question={"question_id": "q" * 10000, "question": "PRIVATE"})
    assert goals.capture_events() == 1
    assert goals.capture_events() == 0
    last = goals.list_events(sid)["events"][-1]
    assert last["payload"] == {"task_waiting": True, "run_id": generic, "status": "waiting_confirmation"}


def test_waiting_child_parent_uses_only_generic_status_and_link_removal_invalidates(state):
    factory, _, _, goals, sid = state
    run_id, task_sid = task(state, status="waiting_user")
    link(state, run_id, task_sid)
    change(state, run_id, waiting_child_user_run_ids=["child-private"], child_question="PRIVATE_CHILD_QUESTION")
    assert goals.capture_events() == 1
    event = goals.list_events(sid)["events"][0]
    assert set(event["payload"]) == {"task_waiting", "run_id", "status"}
    with closing(factory()) as conn, conn:
        assert goals.waiting_event_current(conn, event)
        conn.execute("DELETE FROM managed_session_links WHERE manager_session_id=? AND run_id=?", (sid, run_id))
        assert not goals.waiting_event_current(conn, event)
    assert goals.capture_events() == 0
