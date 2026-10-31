"""Opt-in MySQL lease, uncertain-effect, and root-admission recovery tests."""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from app.core.agent_storage import SqliteAgentRunStore
from app.core.background_jobs import BackgroundJobStore
from app.core.session_admission import SessionBusyError
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.mysql import MySQLConfig, close_mysql_pools

pytestmark = pytest.mark.skipif(
    not os.environ.get("LKA_MYSQL_TEST_CONFIG"),
    reason="LKA_MYSQL_TEST_CONFIG is required for the disposable live MySQL database",
)


@pytest.fixture
def mysql_recovery(tmp_path):
    config_path = Path(os.environ["LKA_MYSQL_TEST_CONFIG"]).expanduser().resolve(strict=True)
    config = MySQLConfig(**json.loads(config_path.read_text(encoding="utf-8")))
    identity = tmp_path / "mysql-recovery-test.sqlite3"
    configure_mysql(identity, config)
    prefix = "test.mysql.recovery." + uuid4().hex
    ready = False
    try:
        with connect_database(identity) as conn:
            table_count = conn.execute_native(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
                "AND table_name IN ('background_jobs','background_job_scopes','agent_sessions','agent_runs',"
                "'agent_run_events','agent_tool_invocations','session_admissions')",
            ).fetchone()[0]
            if table_count != 7:
                pytest.skip("The disposable MySQL recovery schema is not bootstrapped")
        ready = True
        yield identity, config, prefix
    finally:
        if ready:
            with connect_database(identity) as conn:
                conn.execute("BEGIN")
                rows = conn.execute("SELECT scope_id FROM background_job_scopes WHERE kind=? AND scope_id LIKE ?",
                                    ("test.mysql.recovery", prefix + ".%",)).fetchall()
                scopes = [row[0] for row in rows]
                if scopes:
                    marks = ",".join("?" for _ in scopes)
                    for table in ("background_job_inputs", "background_pending_inputs",
                                  "background_pending_watermarks", "background_watermark_heads"):
                        conn.execute(f"DELETE FROM {table} WHERE scope_id IN ({marks})", scopes)
                    conn.execute(
                        "DELETE FROM background_jobs WHERE kind=? AND scope_id IN (" + marks + ")",
                        ("test.mysql.recovery", *scopes),
                    )
                    conn.execute(
                        "DELETE FROM background_job_scopes WHERE kind=? AND scope_id IN (" + marks + ")",
                        ("test.mysql.recovery", *scopes),
                    )
                session_ids = [prefix + ".same", prefix + ".cross-a", prefix + ".cross-b"]
                session_marks = ",".join("?" for _ in session_ids)
                run_rows = conn.execute(
                    "SELECT run_id FROM agent_runs WHERE run_id LIKE ?", (prefix + ".run.%",),
                ).fetchall()
                run_ids = [row[0] for row in run_rows]
                if run_ids:
                    run_marks = ",".join("?" for _ in run_ids)
                    conn.execute(f"DELETE FROM agent_tool_invocations WHERE run_id IN ({run_marks})", run_ids)
                    conn.execute(f"DELETE FROM agent_run_events WHERE run_id IN ({run_marks})", run_ids)
                    conn.execute(f"DELETE FROM agent_runs WHERE run_id IN ({run_marks})", run_ids)
                conn.execute(f"DELETE FROM session_inputs WHERE session_id IN ({session_marks})", session_ids)
                conn.execute(f"DELETE FROM session_input_queues WHERE session_id IN ({session_marks})", session_ids)
                conn.execute(f"DELETE FROM session_admissions WHERE session_id IN ({session_marks})", session_ids)
                conn.execute(f"DELETE FROM agent_sessions WHERE session_id IN ({session_marks})", session_ids)
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)


def _insert_session(identity, session_id):
    stamp = "2026-10-29T00:00:00+00:00"
    with connect_database(identity) as conn:
        conn.execute(
            "INSERT INTO agent_sessions(session_id,title,status,kind,creation_source,metadata,created_at,updated_at) "
            "VALUES(?,?,'active','ordinary','user','{}',?,?)",
            (session_id, "Synthetic MySQL recovery session", stamp, stamp),
        )


def _run_record(run_id, session_id):
    stamp = "2026-10-29T00:00:00+00:00"
    return {"run_id": run_id, "session_id": session_id, "status": "queued",
            "parent_run_id": None, "created_at": stamp, "metadata": {}}


def test_live_mysql_expired_lease_reclaims_new_epoch_and_fences_old_owner(mysql_recovery):
    identity, _, prefix = mysql_recovery
    store = BackgroundJobStore(identity)
    scope = prefix + ".lease"
    enqueued = store.enqueue("test.mysql.recovery", scope, "only-job", {"test_id": prefix})
    first = store.claim("worker-old", 0.15, kinds=("test.mysql.recovery",))
    assert first is not None and first["job_id"] == enqueued["job_id"]
    time.sleep(0.25)

    second = store.claim("worker-new", 2, kinds=("test.mysql.recovery",))
    assert second is not None and second["job_id"] == first["job_id"]
    assert second["lease_epoch"] == first["lease_epoch"] + 1
    assert second["lease_recovery_count"] == first["lease_recovery_count"] + 1
    assert not store.heartbeat(first["job_id"], "worker-old", first["lease_epoch"], 2)
    assert not store.complete(first["job_id"], "worker-old", first["lease_epoch"])
    assert store.complete(second["job_id"], "worker-new", second["lease_epoch"])


def test_live_mysql_tool_invocation_recovery_stays_uncertain_without_replay(mysql_recovery):
    identity, _, prefix = mysql_recovery
    session_id = prefix + ".same"
    run_id = prefix + ".run.tool"
    _insert_session(identity, session_id)
    store = SqliteAgentRunStore(identity)
    store.save_run(_run_record(run_id, session_id), updated_at="2026-10-29T00:00:00+00:00")
    args = {"invocation_id": prefix + ".invocation", "run_id": run_id,
            "tool_name": "synthetic.write", "tool_input": {"value": "no external action is called"},
            "claimed_at": "2026-10-29T00:00:00+00:00"}
    assert store.claim_tool_invocation(**args) == {"status": "claimed"}
    assert store.claim_tool_invocation(**args) == {"status": "uncertain"}
    assert store.claim_tool_invocation(**args) == {"status": "uncertain"}
    invocations = store.list_tool_invocations(run_id)
    assert len(invocations) == 1 and invocations[0]["status"] == "executing"


def test_live_mysql_root_admission_one_winner_per_session_and_parallel_sessions(mysql_recovery):
    identity, _, prefix = mysql_recovery
    sessions = {name: prefix + "." + name for name in ("same", "cross-a", "cross-b")}
    for session_id in sessions.values():
        _insert_session(identity, session_id)
    stores = (SqliteAgentRunStore(identity), SqliteAgentRunStore(identity))

    same_session = sessions["same"]
    barrier = Barrier(2)

    def save_same(index):
        barrier.wait(timeout=3)
        run_id = f"{prefix}.run.same-{index}"
        try:
            stores[index].save_run(_run_record(run_id, same_session), updated_at="2026-10-29T00:00:00+00:00")
            return "saved"
        except SessionBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        same_results = list(pool.map(save_same, (0, 1)))
    assert same_results.count("saved") == 1 and same_results.count("busy") == 1

    cross_barrier = Barrier(2)

    def save_cross(args):
        index, session_name = args
        cross_barrier.wait(timeout=3)
        session_id = sessions[session_name]
        run_id = f"{prefix}.run.{session_name}"
        stores[index].save_run(_run_record(run_id, session_id), updated_at="2026-10-29T00:00:00+00:00")
        return run_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        run_ids = list(pool.map(save_cross, ((0, "cross-a"), (1, "cross-b"))))
    with connect_database(identity) as conn:
        rows = conn.execute("SELECT session_id,run_id FROM session_admissions WHERE session_id IN (?,?)",
                            (sessions["cross-a"], sessions["cross-b"])).fetchall()
    assert {(row[0], row[1]) for row in rows} == {
        (sessions["cross-a"], run_ids[0]), (sessions["cross-b"], run_ids[1]),
    }
