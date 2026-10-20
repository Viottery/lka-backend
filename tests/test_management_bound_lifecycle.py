"""Deletion checks bound business authority without rewriting scheduling history."""
from contextlib import closing
from types import SimpleNamespace

import pytest

from app.domains.management import ManagementConflictError, ManagementService
from app.domains.matters import MatterUpdateInput
from tests.test_management_goal_matter import bound as bound_fixture
from tests.test_management_goals import state as goal_fixture


@pytest.fixture
def state(tmp_path):
    original = goal_fixture.__wrapped__(tmp_path)
    factory, goals, matters, sid, goal, matter = bound_fixture.__wrapped__(original)
    _, sessions, management, _, _ = original
    management.goal_service = goals
    sessions.before_delete = management.guard_delete
    sessions.after_delete = management.after_delete
    sessions.after_restore = management.after_restore
    return SimpleNamespace(factory=factory, goals=goals, matters=matters, sid=sid,
                           goal=goal, matter=matter, sessions=sessions, management=management)


def test_unbound_active_goal_still_prevents_deletion(state):
    # Make bound work inactive so this assertion specifically covers the unbound goal.
    state.matters.update_matter(matter_id=state.matter.matter_id, payload=MatterUpdateInput(status="done"))
    state.goals.create_goal(state.sid, objective="Independent responsibility")
    with pytest.raises(ManagementConflictError, match="active goals"):
        state.sessions.delete_session(session_id=state.sid)
    assert state.management.get(state.sid)["session"]["status"] == "active"


def test_bound_active_work_prevents_deletion_and_checks_same_transaction(state, monkeypatch):
    original = state.goals.enabled_in_transaction
    seen = []
    def enabled(conn, goal_id):
        assert conn.in_transaction
        seen.append(goal_id)
        return original(conn, goal_id)
    monkeypatch.setattr(state.goals, "enabled_in_transaction", enabled)
    with pytest.raises(ManagementConflictError, match="active goals"):
        state.sessions.delete_session(session_id=state.sid)
    assert seen == [state.goal["goal_id"]]


@pytest.mark.parametrize("blocking", ["done", "cancelled", "management_paused"])
def test_inactive_bound_work_allows_delete_restore_with_history_retained(state, blocking):
    if blocking == "management_paused":
        with closing(state.factory()) as conn, conn:
            conn.execute("INSERT INTO matter_management VALUES(?,?, 'goal','agent','Keep',NULL,'paused',1)",
                         (state.matter.matter_id, state.sid))
    else:
        state.matters.update_matter(matter_id=state.matter.matter_id, payload=MatterUpdateInput(status=blocking))
    goal_before = state.goals.get_goal(state.sid, state.goal["goal_id"])
    assert goal_before["status"] == "waiting"
    assert state.sessions.delete_session(session_id=state.sid)
    with pytest.raises(KeyError):
        state.management.get(state.sid)
    with closing(state.factory()) as conn:
        assert conn.execute("SELECT status FROM manager_goals WHERE goal_id=?", (state.goal["goal_id"],)).fetchone()[0] == "waiting"
        assert conn.execute("SELECT matter_id FROM manager_goal_matter_links WHERE goal_id=?", (state.goal["goal_id"],)).fetchone()[0] == state.matter.matter_id
    assert state.sessions.restore_session(session_id=state.sid)
    restored = state.management.get(state.sid)
    assert restored["controller"]["status"] == "paused" and restored["controller"]["revision"] == 3
    assert state.goals.get_goal(state.sid, state.goal["goal_id"]) == goal_before
    assert state.matters.get_matter(matter_id=state.matter.matter_id).status == ("open" if blocking == "management_paused" else blocking)


def test_without_injection_legacy_active_goal_rule_remains(state):
    state.matters.update_matter(matter_id=state.matter.matter_id, payload=MatterUpdateInput(status="done"))
    legacy = ManagementService(state.factory, state.sessions)
    with closing(state.factory()) as conn, pytest.raises(ManagementConflictError, match="active goals"):
        legacy.guard_delete(conn, state.sid)
    injected = ManagementService(state.factory, state.sessions, goal_service=state.goals)
    with closing(state.factory()) as conn:
        injected.guard_delete(conn, state.sid)


def test_inactive_work_does_not_bypass_controller_or_unfinished_runs(state):
    state.matters.update_matter(matter_id=state.matter.matter_id, payload=MatterUpdateInput(status="done"))
    state.management.update(state.sid, expected_revision=1, status="active")
    with pytest.raises(ManagementConflictError, match="Pause management"):
        state.sessions.delete_session(session_id=state.sid)
    state.management.update(state.sid, expected_revision=2, status="paused")
    with closing(state.factory()) as conn, conn:
        conn.execute("INSERT INTO agent_runs(run_id,session_id,status,record_payload,created_at,updated_at) "
                     "VALUES('unfinished',?,'paused','{}','now','now')", (state.sid,))
    with pytest.raises(ManagementConflictError, match="unfinished runs"):
        state.sessions.delete_session(session_id=state.sid)
    with closing(state.factory()) as conn, pytest.raises(ManagementConflictError, match="automatically cleaned"):
        state.management.guard_delete(conn, state.sid, only_if_empty=True)
