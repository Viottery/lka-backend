"""Opt-in live MySQL CAS checks for manager policy publication inputs."""
from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domains.management import ManagementConflictError, ManagementService
from app.domains.management_goals import ManagementGoalService
from app.domains.management_schedules import ManagementScheduleService
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.mysql import MySQLConfig, close_mysql_pools

pytestmark = pytest.mark.skipif(
    not os.environ.get("LKA_MYSQL_TEST_CONFIG"),
    reason="LKA_MYSQL_TEST_CONFIG is required for the disposable live MySQL database",
)


@pytest.fixture
def manager_state(tmp_path):
    config_path = Path(os.environ["LKA_MYSQL_TEST_CONFIG"]).expanduser().resolve(strict=True)
    config = MySQLConfig(**json.loads(config_path.read_text(encoding="utf-8")))
    identity = tmp_path / "mysql-manager-publication-test.sqlite3"
    configure_mysql(identity, config)
    suffix = uuid4().hex
    session_id, goal_id = "test.mysql.manager." + suffix, "test.mysql.goal." + suffix
    stamp = "2026-10-29T00:00:00+00:00"

    def factory():
        conn = connect_database(identity)
        conn.row_factory = sqlite3.Row
        return conn

    class SessionView:
        @staticmethod
        def model_dump(mode="json"):
            return {"session_id": session_id, "kind": "manager"}

    management = ManagementService(
        factory, session_service=SimpleNamespace(get_session_or_none=lambda **_: SessionView()),
    )
    goals = ManagementGoalService(factory, management)
    schedules = ManagementScheduleService(factory, goals, management)
    with factory() as conn:
        ready = conn.execute_native(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
            "AND table_name IN ('agent_sessions','session_admissions','manager_controllers',"
            "'manager_goals','manager_schedules','manager_schedule_commands')",
        ).fetchone()[0]
        if ready != 6:
            unregister_mysql(identity, expected=config)
            close_mysql_pools(config)
            pytest.skip("The disposable MySQL manager schema is not bootstrapped")
    with factory() as conn:
        conn.execute("INSERT INTO agent_sessions(session_id,title,status,kind,creation_source,metadata,created_at,updated_at) "
                     "VALUES(?,?,'active','manager','user','{}',?,?)",
                     (session_id, "Synthetic publication test", stamp, stamp))
        conn.execute("INSERT INTO manager_controllers(session_id,status,revision,created_at,updated_at) "
                     "VALUES(?,'active',1,?,?)", (session_id, stamp, stamp))
        conn.execute("INSERT INTO manager_goals(goal_id,session_id,status,revision,objective,created_at,updated_at) "
                     "VALUES(?,?,'active',1,'Synthetic goal',?,?)", (goal_id, session_id, stamp, stamp))
    try:
        yield factory, management, goals, schedules, session_id, goal_id
    finally:
        with closing(factory()) as conn, conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM manager_schedule_commands WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM manager_schedules WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM manager_goals WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM manager_controllers WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM session_admissions WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM agent_sessions WHERE session_id=?", (session_id,))
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)


def test_live_mysql_controller_revision_update_has_one_winner(manager_state):
    _, management, _, _, session_id, _ = manager_state
    barrier = Barrier(2)

    def update(status):
        barrier.wait(timeout=3)
        try:
            return management.update(session_id, expected_revision=1, status=status)["controller"]["status"]
        except ManagementConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ("paused", "active")))
    assert results.count("conflict") == 1
    assert sum(result in {"paused", "active"} for result in results) == 1


def test_live_mysql_schedule_revision_update_has_one_winner(manager_state):
    _, _, _, schedules, session_id, goal_id = manager_state
    receipt = schedules.configure(
        session_id, "initial", now="2026-10-29T00:00:00Z", goal_id=goal_id,
        timezone="UTC", kind="daily", local_time="09:00",
    )
    schedule_id = receipt["schedule"]["schedule_id"]
    barrier = Barrier(2)

    def update(local_time):
        barrier.wait(timeout=3)
        try:
            schedules.configure(
                session_id, "edit-" + local_time, now="2026-10-29T00:00:00Z",
                goal_id=goal_id, timezone="UTC", kind="daily", local_time=local_time,
                schedule_id=schedule_id, expected_revision=1,
            )
            return "updated"
        except ManagementConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ("10:00", "11:00")))
    assert results.count("updated") == results.count("conflict") == 1
    saved = schedules.get(session_id, schedule_id)
    assert saved["revision"] == 2 and saved["local_time"] in {"10:00", "11:00"}
