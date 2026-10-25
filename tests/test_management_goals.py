"""Goals, event capture and local control APIs use disposable SQLite only."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.managers import router
from app.core.sessions import SessionService
from app.domains.management import ManagementConflictError, ManagementService
from app.domains.management_goals import ManagementGoalService
from app.storage.db import connect, init_db


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "goals.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    sessions = SessionService(factory)
    management = ManagementService(factory, sessions)
    management.ensure_schema()
    service = ManagementGoalService(factory, management)
    service.ensure_schema()
    sid = management.create()["session"]["session_id"]
    return factory, sessions, management, service, sid


def task(state, *, status="completed", parent_run_id=None, manager=False, deleted=False):
    factory, sessions, management, _, _ = state
    sid = management.create()["session"]["session_id"] if manager else sessions.create_session().session.session_id
    run_id = "run_" + uuid4().hex
    payload = {"run_id": run_id, "session_id": sid, "parent_run_id": parent_run_id, "status": status,
               "error_type": "SyntheticFailure" if status == "failed" else None,
               "user_input": "PRIVATE_PROMPT", "metadata": {"raw_prompt": "PRIVATE_PROMPT"}}
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO agent_runs VALUES(?,?,?,?,?,?)", (run_id, sid, status, json.dumps(payload), "now", "now"))
        if deleted:
            conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (sid,))
    return run_id, sid


def test_schema_additively_preserves_legacy_goals(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    management = ManagementService(factory, SessionService(factory))
    management.ensure_schema()
    sid = management.create()["session"]["session_id"]
    with closing(factory()) as conn, conn:
        assert "next_wake_at" not in {row["name"] for row in conn.execute("PRAGMA table_info(manager_goals)")}
        conn.execute("INSERT INTO manager_goals(goal_id,session_id,status,objective,created_at,updated_at) "
                     "VALUES('legacy',?,'paused','Keep this objective','a','b')", (sid,))
    service = ManagementGoalService(factory, management)
    service.ensure_schema()
    service.ensure_schema()
    old = service.get_goal(sid, "legacy")
    assert old["objective"] == "Keep this objective" and old["revision"] == 1
    assert old["timer_generation"] == 1 and old["next_wake_at"] is None and old["wait_run_id"] is None


def test_goal_updates_use_revision_and_preserve_omitted_fields_but_clear_explicit_null(state):
    _, _, _, service, sid = state
    run_id, _ = task(state)
    goal = service.create_goal(sid, objective="Follow up", status="waiting", wait_run_id=run_id,
                               next_wake_at="2026-10-20T10:00:00+08:00")
    assert goal["next_wake_at"] == "2026-10-20T02:00:00+00:00"
    changed = service.update_goal(sid, goal["goal_id"], expected_revision=1, objective="Refined")
    assert changed["wait_run_id"] == run_id and changed["next_wake_at"] == goal["next_wake_at"]
    assert changed["revision"] == changed["timer_generation"] == 2
    with pytest.raises(ManagementConflictError):
        service.update_goal(sid, goal["goal_id"], expected_revision=1, status="completed")
    cleared = service.update_goal(sid, goal["goal_id"], expected_revision=2, next_wake_at=None, wait_run_id=None)
    assert cleared["revision"] == cleared["timer_generation"] == 3
    assert cleared["next_wake_at"] is None and cleared["wait_run_id"] is None


@pytest.mark.parametrize("value", ["2026-10-20", "2026-10-20T10:00:00", "not-a-date"])
def test_goal_rejects_naive_or_invalid_times_without_writes(state, value):
    _, _, _, service, sid = state
    with pytest.raises(ValueError):
        service.create_goal(sid, objective="Follow up", next_wake_at=value)
    assert service.list_goals(sid)["goals"] == []


@pytest.mark.parametrize("options", [{"manager": True}, {"parent_run_id": "some-parent"}, {"deleted": True}])
def test_goal_wait_target_is_an_existing_ordinary_root(state, options):
    _, _, _, service, sid = state
    run_id, _ = task(state, **options)
    with pytest.raises(ValueError, match="ordinary root"):
        service.create_goal(sid, objective="Follow up", wait_run_id=run_id)
    with pytest.raises(ValueError):
        service.create_goal(sid, objective="Follow up", wait_run_id="missing")


def test_due_timer_capture_is_once_revision_bound_and_independent_of_controller_pause(state):
    _, _, _, service, sid = state
    now = datetime(2026, 10, 20, 1, tzinfo=UTC)
    goal = service.create_goal(sid, objective="Check back", next_wake_at=now.isoformat())
    service.create_goal(sid, objective="Not due", next_wake_at=(now + timedelta(hours=1)).isoformat())
    service.create_goal(sid, objective="Paused goal", status="paused", next_wake_at=now.isoformat())
    assert service.capture_events(now) == 1
    assert service.capture_events(now) == 0
    current = service.get_goal(sid, goal["goal_id"])
    assert current["revision"] == 1 and current["next_wake_at"] is None
    event = service.list_events(sid)["events"][0]
    assert event["kind"] == "timer" and event["goal_revision"] == 1
    assert event["dedup_key"] == f"timer:{goal['goal_id']}:1"
    service.update_goal(sid, goal["goal_id"], expected_revision=1, next_wake_at=now.isoformat())
    assert service.capture_events(now) == 1
    assert [item["goal_revision"] for item in service.list_events(sid)["events"]] == [1, 2]


def test_concurrent_capture_commits_one_timer_event(state):
    _, _, _, service, sid = state
    now = datetime.now(UTC)
    service.create_goal(sid, objective="Exactly one wake", next_wake_at=now.isoformat())
    with ThreadPoolExecutor(max_workers=4) as pool:
        captured = list(pool.map(lambda _: service.capture_events(now), range(8)))
    assert sum(captured) == 1
    assert len(service.list_events(sid)["events"]) == 1


def test_empty_capture_skips_write_transaction_and_does_not_advance_inbox_sequence(state):
    factory, _, _, service, sid = state
    run_id, task_sid = task(state)
    with closing(factory()) as conn, conn:
        conn.execute("CREATE TABLE managed_session_links(manager_session_id TEXT,task_session_id TEXT,run_id TEXT)")
        conn.execute("INSERT INTO managed_session_links VALUES(?,?,?)", (sid, task_sid, run_id))
    assert service.capture_events() == 1
    with closing(factory()) as conn, conn:
        before_seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='manager_inbox'").fetchone()[0]
    write_transactions = 0
    def traced_factory():
        nonlocal write_transactions
        conn = factory()
        execute = conn.execute
        def traced(sql, *args, **kwargs):
            nonlocal write_transactions
            if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                write_transactions += 1
            return execute(sql, *args, **kwargs)
        conn.execute = traced
        return conn
    quiet = ManagementGoalService(traced_factory, state[2])
    quiet.ensure_schema()
    write_transactions = 0
    for _ in range(4):
        assert quiet.capture_events() == 0
    assert write_transactions == 0
    with closing(factory()) as conn:
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='manager_inbox'").fetchone()[0] == before_seq


def test_terminal_capture_uses_links_and_unlinked_goal_wait_without_raw_content(state):
    factory, _, _, service, sid = state
    linked, task_sid = task(state, status="failed")
    waited, _ = task(state, status="completed")
    running, _ = task(state, status="running")
    service.create_goal(sid, objective="Wait without link", wait_run_id=waited, status="waiting")
    service.create_goal(sid, objective="Still running", wait_run_id=running, status="waiting")
    with closing(factory()) as conn, conn:
        conn.execute("CREATE TABLE managed_session_links(manager_session_id TEXT,task_session_id TEXT,run_id TEXT)")
        conn.execute("INSERT INTO managed_session_links VALUES(?,?,?)", (sid, task_sid, linked))
    assert service.capture_events() == 2
    assert service.capture_events() == 0
    events = service.list_events(sid)["events"]
    assert {item["payload"]["run_id"] for item in events} == {linked, waited}
    assert all(item["kind"] == "task_terminal" for item in events)
    assert "PRIVATE" not in json.dumps(events) and "user_input" not in json.dumps(events)
    assert next(item for item in events if item["payload"]["run_id"] == waited)["goal_revision"] == 1


def test_cursor_pages_and_manager_lifecycle_filters(state):
    factory, _, management, service, sid = state
    now = datetime.now(UTC)
    for index in range(3):
        service.create_goal(sid, objective=f"Goal {index}", next_wake_at=now.isoformat())
    service.capture_events(now)
    first = service.list_events(sid, limit=2)
    second = service.list_events(sid, after=first["next_after"], limit=2)
    assert len(first["events"]) == 2 and len(second["events"]) == 1 and second["next_after"] is None
    with closing(factory()) as conn, conn:
        for i in range(2):
            conn.execute("INSERT INTO manager_executions VALUES(?,?,?,'completed',1,'[1]','{}','now','now')",
                         (f"execution-{i}", sid, f"run-{i}"))
            conn.execute("INSERT INTO manager_deliveries VALUES(?,?,?,?,?,'now')",
                         (f"delivery-{i}", sid, f"execution-{i}", f"run-{i}", f"message-{i}"))
    page = service.list_executions(sid, limit=1)
    assert page["executions"][0]["event_ids"] == [1]
    assert len(service.list_executions(sid, after=page["next_after"])["executions"]) == 1
    assert service.list_deliveries(sid, limit=1)["next_after"] is not None
    for method in (service.list_goals, service.list_events, service.list_executions, service.list_deliveries):
        with pytest.raises(ValueError):
            method(sid, limit=101)
        with pytest.raises(KeyError):
            method("missing")
    other = management.create()["session"]["session_id"]
    goal_id = service.list_goals(sid)["goals"][0]["goal_id"]
    with pytest.raises(KeyError):
        service.get_goal(other, goal_id)
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (sid,))
    with pytest.raises(KeyError):
        service.list_events(sid)
    assert service.capture_events(now) == 0


def test_goal_and_activity_apis_keep_local_auth_and_revision_contracts(state):
    _, _, management, service, sid = state
    app = FastAPI()
    app.state.runtime = SimpleNamespace(management_service=management, management_goals=service,
                                       local_app_config=SimpleNamespace(management=SimpleNamespace(enabled=True)))
    app.include_router(router)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=("127.0.0.1", 9000)), base_url="http://test") as client:
            created = await client.post(f"/managers/{sid}/goals", json={"objective": "Follow up"})
            assert created.status_code == 201, created.text
            goal = created.json()
            assert (await client.get(f"/managers/{sid}/goals/{goal['goal_id']}")).status_code == 200
            changed = await client.patch(f"/managers/{sid}/goals/{goal['goal_id']}",
                                         json={"expected_revision": 1, "next_wake_at": None})
            assert changed.json()["revision"] == 2
            assert (await client.patch(f"/managers/{sid}/goals/{goal['goal_id']}",
                                       json={"expected_revision": 1, "status": "paused"})).status_code == 409
            assert (await client.post(f"/managers/{sid}/goals", json={"objective": "Bad", "next_wake_at": "2026-10-20"})).status_code == 422
            for suffix in ("events", "executions", "deliveries"):
                assert (await client.get(f"/managers/{sid}/{suffix}")).status_code == 200
                assert (await client.get(f"/managers/{sid}/{suffix}?limit=101")).status_code == 422
            assert (await client.get("/managers/missing/events")).status_code == 404
        async with AsyncClient(transport=ASGITransport(app=app, client=("192.0.2.1", 9000)), base_url="http://test") as client:
            assert (await client.get(f"/managers/{sid}/events")).status_code == 403

    asyncio.run(exercise())


def test_transaction_helpers_share_caller_rollback_and_stable_goal_id(state):
    factory, _, _, service, sid = state
    with closing(factory()) as conn:
        with pytest.raises(ValueError, match="caller-owned"):
            service.create_goal_in_transaction(conn, sid, objective="Must not implicitly commit")
        conn.execute("BEGIN IMMEDIATE")
        created = service.create_goal_in_transaction(conn, sid, goal_id="goal_command_stable", objective="One command")
        changed = service.update_goal_in_transaction(conn, sid, created["goal_id"], expected_revision=1,
                                                     status="waiting", next_wake_at=None)
        assert conn.in_transaction and changed["revision"] == 2
        assert changed["timer_generation"] == 2
        conn.rollback()
    with pytest.raises(KeyError):
        service.get_goal(sid, "goal_command_stable")
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        service.create_goal_in_transaction(conn, sid, goal_id="goal_command_stable", objective="One command")
        with pytest.raises(ManagementConflictError):
            service.create_goal_in_transaction(conn, sid, goal_id="goal_command_stable", objective="Duplicate")
    assert service.get_goal(sid, "goal_command_stable")["revision"] == 1


def test_goal_terminal_events_follow_goal_revision_and_do_not_wake_paused_or_cancelled_goals(state):
    _, _, _, service, sid = state
    run_id, _ = task(state)
    goal = service.create_goal(sid, objective="Wait", status="waiting", wait_run_id=run_id)
    assert service.capture_events() == 1
    service.update_goal(sid, goal["goal_id"], expected_revision=1, objective="Revised wait")
    assert service.capture_events() == 1
    service.update_goal(sid, goal["goal_id"], expected_revision=2, status="paused")
    assert service.capture_events() == 0
    service.update_goal(sid, goal["goal_id"], expected_revision=3, status="active")
    assert service.capture_events() == 1
    service.update_goal(sid, goal["goal_id"], expected_revision=4, status="cancelled")
    assert service.capture_events() == 0
    events = service.list_events(sid)["events"]
    assert [event["goal_revision"] for event in events] == [1, 2, 4]
    assert all(event["goal_id"] == goal["goal_id"] for event in events)
    assert all(event["dedup_key"].endswith(f":{goal['goal_id']}:{event['goal_revision']}") for event in events)


def test_two_goals_waiting_same_terminal_keep_separate_revision_fences_without_unbound_event(state):
    _, _, _, service, sid = state
    run_id, _ = task(state)
    first = service.create_goal(sid, objective="First wait", wait_run_id=run_id)
    second = service.create_goal(sid, objective="Second wait", wait_run_id=run_id)
    assert service.capture_events() == 2
    assert service.capture_events() == 0
    events = service.list_events(sid)["events"]
    assert {event["goal_id"] for event in events} == {first["goal_id"], second["goal_id"]}
    assert all(event["goal_revision"] == 1 for event in events)
    service.update_goal(sid, first["goal_id"], expected_revision=1, status="cancelled")
    service.update_goal(sid, second["goal_id"], expected_revision=1, objective="Changed second wait")
    assert service.capture_events() == 1
    last = service.list_events(sid)["events"][-1]
    assert last["goal_id"] == second["goal_id"] and last["goal_revision"] == 2
