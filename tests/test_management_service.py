from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from app.core.sessions import SessionService
from app.domains.management import ManagementConflictError, ManagementService
from app.storage.db import connect, init_db


@pytest.fixture
def services(tmp_path):
    path = tmp_path / "lka.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    sessions = SessionService(factory)
    management = ManagementService(factory, sessions)
    management.ensure_schema()
    return factory, sessions, management


def test_ordinary_is_not_promoted_by_metadata_and_hooks_are_noops(services):
    factory, sessions, management = services
    ordinary = sessions.create_session(metadata={"kind": "manager", "manager": True}).session
    with pytest.raises(KeyError):
        management.get(ordinary.session_id)
    with closing(factory()) as conn, conn:
        management.guard_delete(conn, ordinary.session_id, only_if_empty=True)
        management.after_delete(conn, ordinary.session_id)
        management.after_restore(conn, ordinary.session_id)
        assert conn.execute("SELECT kind FROM agent_sessions").fetchone()[0] == "ordinary"
    assert management.list() == []


def test_concurrent_default_is_one_session_and_controller(services):
    factory, _, management = services
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: management.get_default(), range(12)))
    assert len({item["session"]["session_id"] for item in results}) == 1
    assert results[0]["controller"]["status"] == "paused"
    assert results[0]["session"]["metadata"] == {}
    assert results[0]["session"]["workspace"] is None
    with closing(factory()) as conn:
        assert conn.execute("SELECT count(*) FROM manager_controllers").fetchone()[0] == 1
    assert len(management.list(limit=1)) == 1


def test_controller_lookup_does_not_load_full_history(services, monkeypatch):
    _, sessions, management = services
    sid = management.create()["session"]["session_id"]

    def no_history(**kwargs):
        raise AssertionError("Controller lookup must not load conversation history")

    monkeypatch.setattr(sessions, "get_session", no_history)
    assert management.get(sid)["session"]["session_id"] == sid


def test_rename_and_model_settings_persist_with_cas(services):
    factory, sessions, management = services
    result = management.create(title="First", llm_client_name="client", llm_model="model")
    sid = result["session"]["session_id"]
    sessions.rename_session(session_id=sid, title="Renamed")
    result = management.update(sid, expected_revision=1, status="active", llm_model=None)
    assert result["session"]["title"] == "Renamed"
    assert result["controller"]["llm_model"] is None
    assert result["controller"]["llm_client_name"] == "client"
    assert result["controller"]["revision"] == 2
    with pytest.raises(ManagementConflictError):
        management.update(sid, expected_revision=1, status="paused")
    restored = ManagementService(factory, sessions).get(sid)
    assert restored == result
    with pytest.raises(ValueError):
        management.update(sid, expected_revision=2, status="deleted")
    with pytest.raises(KeyError):
        management.update("missing", expected_revision=1)


def test_default_tombstone_requires_explicit_switch_and_restore_stays_paused(services):
    factory, _, management = services
    old = management.get_default()["session"]["session_id"]
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        management.guard_delete(conn, old)
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (old,))
        management.after_delete(conn, old)
    with pytest.raises(ManagementConflictError):
        management.get_default()
    with pytest.raises(KeyError):
        management.get(old)
    assert management.list() == []
    replacement = management.create(make_default=True)
    assert management.get_default() == replacement
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='active' WHERE session_id=?", (old,))
        management.after_restore(conn, old)
    assert management.get(old)["controller"]["status"] == "paused"
    assert management.get(old)["controller"]["revision"] == 3
    assert management.get_default() == replacement


@pytest.mark.parametrize("run_status", ["queued", "running", "paused", "waiting_user", "waiting_confirmation"])
def test_delete_rejects_unfinished_manager_run(services, run_status):
    factory, _, management = services
    sid = management.create()["session"]["session_id"]
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO agent_runs(run_id,session_id,status,record_payload,created_at,updated_at) "
                     "VALUES('run',?,?, '{}','now','now')", (sid, run_status))
        with pytest.raises(ManagementConflictError):
            management.guard_delete(conn, sid)


def test_delete_requires_paused_controller_and_goals_but_ignores_independent_runs(services):
    factory, sessions, management = services
    sid = management.create()["session"]["session_id"]
    with closing(factory()) as conn, pytest.raises(ManagementConflictError):
        management.guard_delete(conn, sid, only_if_empty=True)
    management.update(sid, expected_revision=1, status="active")
    with closing(factory()) as conn, pytest.raises(ManagementConflictError):
        management.guard_delete(conn, sid)
    management.update(sid, expected_revision=2, status="paused")
    other = sessions.create_session().session.session_id
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO agent_runs(run_id,session_id,status,record_payload,created_at,updated_at) "
                     "VALUES('independent',?,'running','{}','now','now')", (other,))
        conn.execute("INSERT INTO manager_goals(goal_id,session_id,status,objective,created_at,updated_at) "
                     "VALUES('goal',?,'active','Observe','now','now')", (sid,))
        for status in ("active", "waiting"):
            conn.execute("UPDATE manager_goals SET status=?", (status,))
            with pytest.raises(ManagementConflictError):
                management.guard_delete(conn, sid)
        conn.execute("UPDATE manager_goals SET status='paused'")
        management.guard_delete(conn, sid)
        assert conn.execute("SELECT status FROM agent_runs WHERE run_id='independent'").fetchone()[0] == "running"
