"""Opt-in live MySQL queue locking checks; uses only unique synthetic rows."""
from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.core.session_inputs import SessionInputQueue
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.mysql import MySQLConfig, close_mysql_pools

pytestmark = pytest.mark.skipif(
    not os.environ.get("LKA_MYSQL_TEST_CONFIG"),
    reason="LKA_MYSQL_TEST_CONFIG is required for the disposable live MySQL database",
)


@pytest.fixture
def mysql_queue(tmp_path):
    config_path = Path(os.environ["LKA_MYSQL_TEST_CONFIG"]).expanduser().resolve(strict=True)
    config = MySQLConfig(**json.loads(config_path.read_text(encoding="utf-8")))
    identity = tmp_path / "mysql-concurrency-test.sqlite3"
    configure_mysql(identity, config)
    store = BackgroundJobStore(identity)
    prefix = "test.mysql." + uuid4().hex
    schema_ready = False
    try:
        with connect_database(identity) as conn:
            ready = conn.execute_native(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
                "AND table_name IN ('background_jobs','background_job_scopes','session_inputs',"
                "'session_input_queues','session_admissions','agent_sessions')",
            ).fetchone()[0]
            if ready != 6:
                pytest.skip("The disposable MySQL schema is not bootstrapped for these tests")
        schema_ready = True
        yield store, identity, config, prefix
    finally:
        # Remove only rows bearing this fixture's random scope prefix; no table
        # drops or broad database cleanup are permitted in the shared lab.
        if schema_ready:
            with connect_database(identity) as conn:
                conn.execute("BEGIN")
                scope_rows = conn.execute("SELECT scope_id FROM background_job_scopes WHERE scope_id LIKE ?",
                                          (prefix + ".%",)).fetchall()
                scopes = [row[0] for row in scope_rows]
                if scopes:
                    marks = ",".join("?" for _ in scopes)
                    for table in ("background_pending_inputs", "background_pending_watermarks",
                                  "background_watermark_heads", "background_job_inputs"):
                        conn.execute(f"DELETE FROM {table} WHERE scope_id IN ({marks})", scopes)
                    conn.execute(f"DELETE FROM background_jobs WHERE scope_id IN ({marks})", scopes)
                    conn.execute(f"DELETE FROM background_job_scopes WHERE scope_id IN ({marks})", scopes)
                session_id = prefix + ".session"
                conn.execute("DELETE FROM agent_session_messages WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM session_inputs WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM session_input_queues WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM session_admissions WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM agent_sessions WHERE session_id=?", (session_id,))
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)


def test_live_mysql_same_scope_claim_has_one_winner(mysql_queue):
    store, _, _, prefix = mysql_queue
    scope = prefix + ".same"
    store.enqueue("test.mysql.concurrency", scope, "first", {"run_id": "run-first"})
    store.enqueue("test.mysql.concurrency", scope, "second", {"run_id": "run-second"})
    barrier = Barrier(2)

    def claim(owner):
        barrier.wait(timeout=3)
        return store.claim(owner, 30, kinds=("test.mysql.concurrency",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))

    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert winners[0]["scope_id"] == scope


def test_live_mysql_distinct_scopes_can_be_claimed_concurrently(mysql_queue):
    store, _, _, prefix = mysql_queue
    scopes = (prefix + ".one", prefix + ".two")
    for index, scope in enumerate(scopes):
        store.enqueue("test.mysql.concurrency", scope, f"job-{index}", {"run_id": f"run-{index}"})
    barrier = Barrier(2)

    def claim(owner):
        barrier.wait(timeout=3)
        return store.claim(owner, 30, kinds=("test.mysql.concurrency",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))

    assert {item["scope_id"] for item in results if item is not None} == set(scopes)


class _SessionMessages:
    @staticmethod
    def append_message_in_transaction(conn, *, session_id, role, content, payload, message_id):
        conn.execute("INSERT INTO agent_session_messages(message_id,session_id,role,content,payload,created_at) "
                     "VALUES(?,?,?,?,?,?)",
                     (message_id, session_id, role, content, json.dumps(payload),
                      "2026-10-29T00:00:00+00:00"))


def _live_input_queue(identity):
    def connect_rows():
        conn = connect_database(identity)
        conn.row_factory = sqlite3.Row
        return conn

    return SessionInputQueue(
        connect_rows, sessions=_SessionMessages(), run_store=None, dispatcher=None,
        authorize=lambda conn, session_id, accepting=False: None,
        policy=lambda session_id: SimpleNamespace(
            llm_answer_client_name=None, llm_answer_model=None,
        ), validate_attachments=lambda **kwargs: None,
    )


def test_live_mysql_submit_serializes_fifo_and_idempotency(mysql_queue):
    _, identity, _, prefix = mysql_queue
    session_id = prefix + ".session"
    with connect_database(identity) as conn:
        conn.execute("INSERT INTO agent_sessions(session_id,title,status,metadata,created_at,updated_at) "
                     "VALUES(?,?,'active','{}','2026-10-29T00:00:00+00:00','2026-10-29T00:00:00+00:00')",
                     (session_id, "Concurrency test"))
    queue = _live_input_queue(identity)
    barrier = Barrier(2)

    def submit(index):
        barrier.wait(timeout=3)
        return queue.submit(session_id, client_message_id=f"client-{index}",
                            user_input=f"message {index}", attachment_ids=[])

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit, (1, 2)))
    assert sorted(item["input"]["sequence"] for item in receipts) == [1, 2]

    replay_barrier = Barrier(2)

    def replay(_):
        replay_barrier.wait(timeout=3)
        return queue.submit(session_id, client_message_id="same-client",
                            user_input="same message", attachment_ids=[])

    with ThreadPoolExecutor(max_workers=2) as pool:
        retries = list(pool.map(replay, range(2)))
    assert retries[0]["input"]["input_id"] == retries[1]["input"]["input_id"]
    assert retries[0]["input"]["sequence"] == retries[1]["input"]["sequence"] == 3
