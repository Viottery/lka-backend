from __future__ import annotations

import json
import os
import sqlite3
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.agent_storage import SqliteAgentRunStore, _json_dump
from app.core.safety import SafetyReviewRecord, SafetyReviewStatus


def _seed(path: Path, *, run_id: str, status: str = "failed") -> SqliteAgentRunStore:
    store = SqliteAgentRunStore(path)
    now = "2026-10-30T00:00:00+00:00"
    run = {"run_id": run_id, "session_id": "cleanup-session", "status": status,
           "parent_run_id": "synthetic-parent", "created_at": now}
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO agent_runs(run_id,session_id,status,record_payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (run_id, run["session_id"], status, _json_dump(run), now, now),
        )
        review = {
            "review_id": f"review-{run_id}", "run_id": run_id, "session_id": "cleanup-session",
            "trace_id": "trace", "invocation_id": "invocation", "tool_name": "matter.create",
            "tool_input": {"title": "preserved"}, "mode": "manual", "reason": "review reason",
            "created_at": now, "status": "pending", "decided_by": None,
            "decision_reason": None, "decided_at": None,
        }
        conn.execute(
            "INSERT INTO agent_safety_reviews(review_id,run_id,status,review_payload,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (review["review_id"], run_id, "pending", _json_dump(review), now, now),
        )
        conn.execute(
            "INSERT INTO agent_tool_invocations(invocation_id,run_id,tool_name,input_hash,status,result_payload,claimed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (f"invocation-{run_id}", run_id, "mail.send", sha256(_json_dump({}).encode()).hexdigest(),
             "executing", None, now, now),
        )
    return store


def _audit_types(path: Path, run_id: str) -> list[str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT event_payload FROM agent_run_events WHERE run_id=? ORDER BY sequence", (run_id,),
        ).fetchall()
    return [json.loads(row[0])["type"] for row in rows]


def test_terminal_cleanup_is_atomic_audited_and_idempotent(tmp_path):
    path = tmp_path / "cleanup.sqlite3"
    run_id = "cleanup-run"
    store = _seed(path, run_id=run_id)

    assert store.expire_terminal_safety_reviews(run_id, "stale terminal request", "2026-10-30T01:00:00Z") == 1
    [expired] = store.list_reviews(run_id)
    assert SafetyReviewRecord.model_validate(expired).status is SafetyReviewStatus.EXPIRED
    assert expired["tool_input"] == {"title": "preserved"}
    assert expired["decided_by"] == "system.terminal_cleanup"
    assert expired["decision_reason"] == "stale terminal request"
    assert store.expire_terminal_safety_reviews(run_id, "stale terminal request", "2026-10-30T01:00:00Z") == 0

    assert store.quarantine_terminal_tool_invocation(
        f"invocation-{run_id}", "run failed before acknowledgement", "2026-10-30T01:01:00Z",
    ) is True
    assert store.quarantine_terminal_tool_invocation(
        f"invocation-{run_id}", "run failed before acknowledgement", "2026-10-30T01:01:00Z",
    ) is False
    assert store.claim_tool_invocation(
        invocation_id=f"invocation-{run_id}", run_id=run_id, tool_name="mail.send",
        tool_input={}, claimed_at="2026-10-30T01:02:00Z",
    ) == {"status": "uncertain"}
    with sqlite3.connect(path) as conn:
        status, result = conn.execute(
            "SELECT status,result_payload FROM agent_tool_invocations WHERE invocation_id=?",
            (f"invocation-{run_id}",),
        ).fetchone()
        assert (status, result) == ("unknown", None)
    assert _audit_types(path, run_id) == ["safety_reviews_expired", "tool_invocation_quarantined"]


@pytest.mark.parametrize("status", ["queued", "running", "waiting_confirmation"])
def test_cleanup_rejects_nonterminal_or_wrong_terminal_run(tmp_path, status):
    store = _seed(tmp_path / f"{status}.sqlite3", run_id="run", status=status)
    with pytest.raises(ValueError):
        store.expire_terminal_safety_reviews("run", "cleanup", "2026-10-30T00:00:00Z")
    with pytest.raises(ValueError):
        store.quarantine_terminal_tool_invocation(
            "invocation-run", "cleanup", "2026-10-30T00:00:00Z",
        )


def test_quarantine_rejects_completed_run(tmp_path):
    store = _seed(tmp_path / "completed.sqlite3", run_id="run", status="completed")
    assert store.expire_terminal_safety_reviews("run", "cleanup", "2026-10-30T00:00:00Z") == 1
    with pytest.raises(ValueError):
        store.quarantine_terminal_tool_invocation("invocation-run", "cleanup", "2026-10-30T00:00:00Z")


def test_cleanup_validation_and_nonexecuting_invocation_are_rejected(tmp_path):
    store = _seed(tmp_path / "validation.sqlite3", run_id="run")
    with pytest.raises(ValueError):
        store.expire_terminal_safety_reviews("run", "", "2026-10-30T00:00:00Z")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE agent_tool_invocations SET status='completed',result_payload='{}' WHERE invocation_id='invocation-run'")
    with pytest.raises(ValueError):
        store.quarantine_terminal_tool_invocation("invocation-run", "cleanup", "2026-10-30T00:00:00Z")
    assert _audit_types(store.db_path, "run") == []


@pytest.mark.skipif(not os.environ.get("LKA_MYSQL_TEST_CONFIG"), reason="Disposable MySQL config required")
def test_live_mysql_terminal_cleanup(tmp_path):
    from app.storage.connections import configure_mysql, connect_database, unregister_mysql
    from app.storage.mysql import close_mysql_pools
    from tests.test_mysql_owner_live import _config

    config = _config()
    identity = tmp_path / "terminal-cleanup-authority.sqlite3"
    configure_mysql(identity, config)
    run_id = "test.terminal-cleanup." + uuid4().hex
    store = SqliteAgentRunStore(identity)
    now = "2026-10-30T00:00:00+00:00"
    run = {"run_id": run_id, "session_id": "test-cleanup", "status": "failed",
           "parent_run_id": "synthetic-parent", "created_at": now}
    review_id = "review." + uuid4().hex
    invocation_id = "invocation." + uuid4().hex
    review = {"review_id": review_id, "run_id": run_id, "session_id": "test-cleanup",
              "trace_id": "trace", "invocation_id": invocation_id, "tool_name": "mail.send",
              "tool_input": {}, "mode": "manual", "reason": "test", "created_at": now,
              "status": "pending", "decided_by": None, "decision_reason": None, "decided_at": None}
    try:
        with connect_database(identity) as conn:
            conn.execute("INSERT INTO agent_runs(run_id,session_id,status,record_payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                         (run_id, "test-cleanup", "failed", _json_dump(run), now, now))
            conn.execute("INSERT INTO agent_safety_reviews(review_id,run_id,status,review_payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                         (review_id, run_id, "pending", _json_dump(review), now, now))
            conn.execute("INSERT INTO agent_tool_invocations(invocation_id,run_id,tool_name,input_hash,status,result_payload,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (invocation_id, run_id, "mail.send", sha256(_json_dump({}).encode()).hexdigest(),
             "executing", None, now, now))
        assert store.expire_terminal_safety_reviews(run_id, "test cleanup", now) == 1
        assert store.quarantine_terminal_tool_invocation(invocation_id, "test cleanup", now)
        assert store.quarantine_terminal_tool_invocation(invocation_id, "test cleanup", now) is False
        assert store.claim_tool_invocation(
            invocation_id=invocation_id, run_id=run_id, tool_name="mail.send",
            tool_input={}, claimed_at=now,
        ) == {"status": "uncertain"}
        with connect_database(identity) as conn:
            assert conn.execute("SELECT status FROM agent_safety_reviews WHERE review_id=?", (review_id,)).fetchone()[0] == "expired"
            assert tuple(conn.execute("SELECT status,result_payload FROM agent_tool_invocations WHERE invocation_id=?",
                                      (invocation_id,)).fetchone()) == ("unknown", None)
            assert conn.execute("SELECT COUNT(*) FROM agent_run_events WHERE run_id=?", (run_id,)).fetchone()[0] == 2
    finally:
        with connect_database(identity) as conn:
            conn.execute("DELETE FROM agent_run_events WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM agent_tool_invocations WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM agent_safety_reviews WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM agent_runs WHERE run_id=?", (run_id,))
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)
