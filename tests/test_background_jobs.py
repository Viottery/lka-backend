from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.core.background_jobs import BackgroundJobStore, BackgroundJobWorker
from app.core.llm.errors import LLMNetworkError, LLMProviderHTTPError, LLMTimeoutError


def make_store(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3")
    store.ensure_schema()
    return store


def add(store, key="event-1", **kwargs):
    return store.enqueue("memory.extract", "session-1", key, {"session_id": "session-1"}, **kwargs)


def test_enqueue_is_idempotent_and_payload_is_hidden_from_status(tmp_path):
    store = make_store(tmp_path)
    first = add(store)
    again = add(store)
    assert first["job_id"] == again["job_id"]
    assert "payload" not in store.status(first["job_id"])
    assert store.get(first["job_id"], include_payload=True)["payload"] == {"session_id": "session-1"}
    assert len(store.list()) == 1


def test_input_progress_is_durable_and_old_lease_cannot_checkpoint(tmp_path):
    store = make_store(tmp_path)
    job = add(store)
    claim = store.claim("first", 30)
    assert store.complete_input(job["job_id"], "first", claim["lease_epoch"], "source-1")
    assert make_store(tmp_path).completed_inputs(job["job_id"]) == {"source-1"}
    with store._connect() as conn:
        conn.execute("UPDATE background_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["job_id"],))
    successor = store.claim("second", 30)
    assert successor["lease_epoch"] > claim["lease_epoch"]
    assert not store.complete_input(job["job_id"], "first", claim["lease_epoch"], "source-2")
    assert store.completed_inputs(job["job_id"]) == {"source-1"}
    assert store.complete_input(job["job_id"], "second", successor["lease_epoch"], "source-2")


def test_payload_rejects_sensitive_or_unbounded_data(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.enqueue("memory.extract", "s", "k", {"content": "private body"})
    with pytest.raises(ValueError):
        store.enqueue("memory.extract", "s", "k", {"session_id": "s", "other": "x" * 5000})


def test_domain_workers_only_claim_registered_kinds(tmp_path):
    store = make_store(tmp_path)
    other = add(store)
    own = store.enqueue("message_analysis", "conversation-1", "range-1", {"start_seq": 1})
    seen = []
    worker = BackgroundJobWorker(store, {"message_analysis": lambda job: seen.append(job["job_id"])})
    assert worker.run_one()
    assert seen == [own["job_id"]]
    assert not worker.run_one()
    assert store.get(other["job_id"])["status"] == "queued"
    assert not BackgroundJobWorker(store, {}).run_one()
    assert store.claim("memory-worker", 30, kinds=("memory.extract",))["job_id"] == other["job_id"]


def test_enqueue_can_join_callers_atomic_outbox_transaction(tmp_path):
    store = make_store(tmp_path)
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("CREATE TABLE source_events(event_id TEXT PRIMARY KEY)")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO source_events VALUES ('e-1')")
    queued = store.enqueue("memory.extract", "s", "e-1", {"session_id": "s"}, conn=conn)
    conn.rollback()
    conn.close()
    assert store.get(queued["job_id"]) is None


def enqueue_watermark(store, conn, scope, revision, target, *, order=None, deadline=None):
    return store.enqueue_latest_watermark(
        "generic_compact", scope, f"{revision}:{target}",
        {"revision": revision, "target_seq": target}, conn=conn,
        watermark_order=order, deadline=deadline,
    )


def test_latest_watermark_coalesces_twenty_updates_for_one_queued_scope(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3", max_pending_jobs=1)
    store.ensure_schema()
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    for target in range(1, 21):
        enqueue_watermark(store, conn, "session-a", target, target)
    conn.commit()
    conn.close()

    jobs = store.list(status="queued")
    assert len(jobs) == 1
    assert store.get(jobs[0]["job_id"], include_payload=True)["payload"] == {
        "revision": 20, "target_seq": 20,
    }
    with store._connect() as check:
        assert check.execute("SELECT COUNT(*) FROM background_pending_watermarks").fetchone()[0] == 0


def test_latest_watermark_keeps_new_target_separate_while_scope_is_running(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3", max_pending_jobs=2)
    store.ensure_schema()
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    first = enqueue_watermark(store, conn, "session-a", 1, 10)
    conn.commit()
    conn.close()
    running = store.claim("worker", 30)
    assert running["job_id"] == first["job_id"]

    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    enqueue_watermark(store, conn, "session-a", 2, 20)
    enqueue_watermark(store, conn, "session-a", 3, 30)
    conn.commit()
    conn.close()
    assert store.get(running["job_id"], include_payload=True)["payload"]["target_seq"] == 10
    with store._connect() as check:
        pending = check.execute(
            "SELECT watermark_id,payload_json FROM background_pending_watermarks WHERE scope_id='session-a'"
        ).fetchone()
    assert pending["watermark_id"] == "3:30"
    assert json.loads(pending["payload_json"]) == {"revision": 3, "target_seq": 30}

    assert store.complete(running["job_id"], "worker", running["lease_epoch"])
    successor = store.claim("worker-2", 30)
    assert successor["payload"] == {"revision": 3, "target_seq": 30}


def test_latest_watermark_updates_retry_wait_job_without_creating_another(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3", max_pending_jobs=2)
    store.ensure_schema()
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    first = enqueue_watermark(store, conn, "session-a", 1, 10)
    conn.commit()
    conn.close()
    claimed = store.claim("worker", 30)
    assert claimed["job_id"] == first["job_id"]
    assert store.fail(claimed["job_id"], "worker", claimed["lease_epoch"], "temporary", True)

    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    merged = enqueue_watermark(store, conn, "session-a", 2, 20)
    conn.commit()
    conn.close()
    assert merged["job_id"] == first["job_id"]
    assert merged["status"] == "retry_wait"
    assert store.get(first["job_id"], include_payload=True)["payload"] == {
        "revision": 2, "target_seq": 20,
    }
    assert len(store.list(status="retry_wait")) == 1


def test_latest_watermark_overflow_materializes_after_restart_and_capacity_returns(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = BackgroundJobStore(path, max_pending_jobs=1)
    store.ensure_schema()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    first = enqueue_watermark(store, conn, "session-a", 1, 10)
    overflow = enqueue_watermark(store, conn, "session-b", 2, 20)
    conn.commit()
    conn.close()
    assert first["status"] == "queued"
    assert overflow == {"job_id": None, "status": "backpressured"}

    restarted = BackgroundJobStore(path, max_pending_jobs=1)
    restarted.ensure_schema()
    active = restarted.claim("worker-a", 30)
    assert active["scope_id"] == "session-a"
    assert restarted.complete(active["job_id"], "worker-a", active["lease_epoch"])
    recovered = restarted.claim("worker-b", 30)
    assert recovered["scope_id"] == "session-b"
    assert recovered["payload"] == {"revision": 2, "target_seq": 20}


def test_latest_watermark_and_pending_payload_rollback_with_source_transaction(tmp_path):
    store = make_store(tmp_path)
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    result = enqueue_watermark(store, conn, "session-a", 1, 10)
    assert result["status"] == "queued"
    conn.rollback()
    conn.close()
    assert store.list() == []
    with store._connect() as check:
        assert check.execute("SELECT COUNT(*) FROM background_pending_watermarks").fetchone()[0] == 0


def test_older_followup_cannot_replace_newer_queued_watermark_but_equal_can_continue(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3", max_pending_jobs=2)
    store.ensure_schema()
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    newest = enqueue_watermark(store, conn, "session-a", 1, 200, order=200)
    stale = enqueue_watermark(store, conn, "session-a", 2, 80, order=80)
    continued = enqueue_watermark(store, conn, "session-a", 3, 200, order=200)
    conn.commit()
    conn.close()

    assert stale["job_id"] == newest["job_id"]
    assert continued["job_id"] == newest["job_id"]
    assert store.get(newest["job_id"], include_payload=True)["payload"] == {
        "revision": 3, "target_seq": 200,
    }


def test_older_followup_cannot_replace_newer_overflow_watermark(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = BackgroundJobStore(path, max_pending_jobs=1)
    store.ensure_schema()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    filler = enqueue_watermark(store, conn, "session-a", 1, 5, order=5)
    newest = enqueue_watermark(store, conn, "session-b", 1, 200, order=200)
    stale = enqueue_watermark(store, conn, "session-b", 2, 80, order=80)
    conn.commit()
    conn.close()
    assert newest["status"] == "backpressured"
    assert stale["status"] == "stale_watermark_ignored"

    restarted = BackgroundJobStore(path, max_pending_jobs=1)
    restarted.ensure_schema()
    active = restarted.claim("worker-a", 30)
    assert active["scope_id"] == "session-a"
    assert restarted.complete(active["job_id"], "worker-a", active["lease_epoch"])
    materialized = restarted.claim("worker-b", 30)
    assert materialized["scope_id"] == "session-b"
    assert materialized["payload"] == {"revision": 1, "target_seq": 200}
    assert filler["job_id"] == active["job_id"]


def test_persisted_head_rejects_late_old_followup_after_pending_was_materialized(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = BackgroundJobStore(path, max_pending_jobs=1)
    store.ensure_schema()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    enqueue_watermark(store, conn, "session-a", 1, 5, order=5)
    enqueue_watermark(store, conn, "session-b", 1, 200, order=200)
    conn.commit()
    conn.close()

    restarted = BackgroundJobStore(path, max_pending_jobs=1)
    restarted.ensure_schema()
    first = restarted.claim("worker-a", 30)
    restarted.complete(first["job_id"], "worker-a", first["lease_epoch"])
    ready = restarted.claim("worker-b", 30)
    assert ready["scope_id"] == "session-b"
    assert ready["payload"]["target_seq"] == 200
    assert restarted.complete(ready["job_id"], "worker-b", ready["lease_epoch"])

    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    stale = enqueue_watermark(restarted, conn, "session-b", 2, 80, order=80)
    conn.commit()
    conn.close()
    assert stale["status"] == "stale_watermark_ignored"
    assert restarted.claim("worker-c", 30) is None


def test_expired_pending_watermark_is_removed_without_blocking_claim(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3", max_pending_jobs=1)
    store.ensure_schema()
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    active = enqueue_watermark(store, conn, "session-a", 1, 10, order=10)
    enqueue_watermark(
        store, conn, "session-b", 1, 20, order=20,
        deadline=datetime.now(UTC) + timedelta(hours=1),
    )
    conn.commit()
    conn.close()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE background_pending_watermarks SET deadline=? WHERE scope_id='session-b'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds"),),
        )

    claimed = store.claim("worker", 30)
    assert claimed["job_id"] == active["job_id"]
    with store._connect() as check:
        assert check.execute(
            "SELECT COUNT(*) FROM background_pending_watermarks WHERE scope_id='session-b'"
        ).fetchone()[0] == 0


def test_health_metrics_counts_pending_latest_watermarks(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3", max_pending_jobs=2)
    store.ensure_schema()
    conn = sqlite3.connect(store.db_path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    initial = enqueue_watermark(store, conn, "session-a", 1, 10, order=10)
    conn.commit()
    conn.close()
    running = store.claim("worker", 30)
    assert running["job_id"] == initial["job_id"]

    conn = sqlite3.connect(store.db_path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    enqueue_watermark(store, conn, "session-a", 2, 20, order=20)
    conn.commit()
    conn.close()
    metrics = store.health_metrics()
    assert metrics["pending_watermark_count"] == 1
    assert metrics["backpressured_input_count"] == 0


def test_grouped_outbox_backpressure_survives_restart_and_materializes_once(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = BackgroundJobStore(path, max_pending_jobs=1)
    store.ensure_schema()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    original_factory = conn.row_factory
    conn.execute("BEGIN IMMEDIATE")
    session_a = [f"a-{index}" for index in range(8)]
    for source_id in session_a:
        result = store.enqueue_grouped(
            "memory.extract", "session-a", source_id, conn=conn,
        )
        assert result["status"] in {"queued", "backpressured"}
        assert conn.row_factory is original_factory
    conn.commit()
    conn.close()

    assert len(store.list(status="queued")) == 1
    group_a = store.list(status="queued")[0]
    assert store.get(group_a["job_id"], include_payload=True)["payload"]["message_ids"] == session_a

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    original_factory = conn.row_factory
    conn.execute("BEGIN IMMEDIATE")
    overflow = store.enqueue_grouped(
        "memory.extract", "session-b", "b-1", conn=conn,
    )
    assert overflow == {"job_id": None, "status": "backpressured"}
    assert conn.row_factory is original_factory
    conn.commit()
    conn.close()
    assert "payload" not in overflow

    # A fresh store instance simulates process restart while the source ID is
    # still in the durable overflow table.
    restarted = BackgroundJobStore(path, max_pending_jobs=1)
    restarted.ensure_schema()
    claim_a = restarted.claim("worker-a", 30)
    assert claim_a["job_id"] == group_a["job_id"]
    assert claim_a["payload"]["message_ids"] == session_a
    assert restarted.complete(claim_a["job_id"], "worker-a", claim_a["lease_epoch"])

    # A claim pumps pending IDs transactionally. The just-materialized job's
    # available_at can be a few microseconds newer than claim()'s initial
    # timestamp, so the first pump may return no runnable job; the next claim
    # must pick the same single materialized job.
    claim_b = restarted.claim("worker-b", 30)
    if claim_b is None:
        claim_b = restarted.claim("worker-b", 30)
    assert claim_b is not None
    assert claim_b["scope_id"] == "session-b"
    assert claim_b["payload"]["message_ids"] == ["b-1"]
    assert restarted.complete(claim_b["job_id"], "worker-b", claim_b["lease_epoch"])

    # Retained idempotency provenance prevents replay from materializing a
    # duplicate even after the original job reached a terminal state.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    replay = restarted.enqueue_grouped(
        "memory.extract", "session-b", "b-1", conn=conn,
    )
    conn.commit()
    conn.close()
    assert replay["job_id"] == claim_b["job_id"]
    assert replay["status"] == "succeeded"
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT COUNT(*) FROM background_pending_inputs WHERE input_id='b-1'"
        ).fetchone()[0] == 0
        assert check.execute(
            "SELECT COUNT(*) FROM background_job_inputs WHERE input_id='b-1'"
        ).fetchone()[0] == 1


def test_eighty_grouped_inputs_create_exactly_ten_eight_item_jobs(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite3", max_pending_jobs=20)
    store.ensure_schema()
    conn = sqlite3.connect(store.db_path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    inputs = [f"message-{index}" for index in range(80)]
    for source_id in inputs:
        result = store.enqueue_grouped(
            "memory.extract", "one-session", source_id, conn=conn,
        )
        assert result["job_id"] is not None
    conn.commit()
    conn.close()

    jobs = store.list(kind="memory.extract", scope_id="one-session", limit=20)
    assert len(jobs) == 10
    grouped_ids = [
        source_id
        for job in jobs
        for source_id in store.get(job["job_id"], include_payload=True)["payload"]["message_ids"]
    ]
    assert len(grouped_ids) == 80
    assert len(set(grouped_ids)) == 80
    assert set(grouped_ids) == set(inputs)
    assert all(
        len(store.get(job["job_id"], include_payload=True)["payload"]["message_ids"]) == 8
        for job in jobs
    )


def test_compact_history_clears_only_old_terminal_payload_and_keeps_source_identity(tmp_path):
    store = make_store(tmp_path)
    conn = sqlite3.connect(store.db_path, isolation_level=None)
    conn.execute(
        "CREATE TABLE agent_session_messages(message_id TEXT PRIMARY KEY,content TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE memory_sources(source_ref TEXT PRIMARY KEY)")
    conn.execute(
        "INSERT INTO agent_session_messages VALUES(?,?)",
        ("raw-message", "原始会话日志必须保留"),
    )
    conn.execute("INSERT INTO memory_sources VALUES('user-message-source')")
    conn.execute("BEGIN IMMEDIATE")
    conn.row_factory = sqlite3.Row
    source = store.enqueue_grouped(
        "memory.extract", "session-compact", "source-old", conn=conn,
    )
    conn.commit()
    conn.close()
    claim = store.claim("worker", 30)
    assert claim["job_id"] == source["job_id"]
    assert store.complete(claim["job_id"], "worker", claim["lease_epoch"])

    recent = add(store, "recent-terminal")
    recent_claim = store.claim("worker", 30)
    assert recent_claim["job_id"] == recent["job_id"]
    assert store.complete(recent_claim["job_id"], "worker", recent_claim["lease_epoch"])

    old_running = add(store, "old-running")
    running_claim = store.claim("worker", 30)
    assert running_claim["job_id"] == old_running["job_id"]
    old_stamp = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="microseconds")
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE background_jobs SET finished_at=?,updated_at=? WHERE job_id=?",
            (old_stamp, old_stamp, source["job_id"]),
        )
        db.execute(
            "UPDATE background_jobs SET finished_at=?,updated_at=? WHERE job_id=?",
            (old_stamp, old_stamp, old_running["job_id"]),
        )

    assert store.compact_history(retention_days=30, limit=500) == 1
    compacted = store.get(source["job_id"], include_payload=True)
    assert compacted["status"] == "succeeded"
    assert compacted["payload"] == {}
    assert compacted["idempotency_key"] == "source-old"
    assert store.get(recent["job_id"], include_payload=True)["payload"] == {"session_id": "session-1"}
    assert store.get(old_running["job_id"], include_payload=True)["payload"] == {"session_id": "session-1"}

    replay_conn = sqlite3.connect(store.db_path, isolation_level=None)
    replay_conn.execute("BEGIN IMMEDIATE")
    replay = store.enqueue_grouped(
        "memory.extract", "session-compact", "source-old", conn=replay_conn,
    )
    replay_conn.commit()
    replay_conn.close()
    assert replay["job_id"] == source["job_id"]
    assert replay["status"] == "succeeded"
    assert store.claim("worker", 30) is None

    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT content FROM agent_session_messages WHERE message_id='raw-message'"
        ).fetchone()[0] == "原始会话日志必须保留"
        assert db.execute(
            "SELECT source_ref FROM memory_sources"
        ).fetchone()[0] == "user-message-source"
        assert db.execute(
            "SELECT COUNT(*) FROM background_job_inputs "
            "WHERE kind='memory.extract' AND scope_id='session-compact' AND input_id='source-old'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM background_pending_inputs WHERE input_id='source-old'"
        ).fetchone()[0] == 0


def test_concurrent_claim_has_single_winner(tmp_path):
    store = make_store(tmp_path)
    add(store)
    barrier = threading.Barrier(2)

    def claim(owner):
        barrier.wait()
        return store.claim(owner, 30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0]["attempts"] == 1


def test_expired_lease_reclaims_with_epoch_fencing(tmp_path):
    store = make_store(tmp_path)
    job = add(store)
    first = store.claim("old", 0.03)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE background_jobs SET lease_expires_at=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job["job_id"]))
    second = store.claim("new", 30)
    assert second["job_id"] == job["job_id"]
    assert second["lease_epoch"] == first["lease_epoch"] + 1
    assert not store.heartbeat(job["job_id"], "old", first["lease_epoch"])
    assert not store.complete(job["job_id"], "old", first["lease_epoch"])
    assert store.complete(job["job_id"], "new", second["lease_epoch"])
    assert store.complete(job["job_id"], "new", second["lease_epoch"]) is False


def test_retry_wait_then_retry_and_terminal_nonretryable_failure(tmp_path):
    store = make_store(tmp_path)
    retry_job = add(store, "retry", max_attempts=2)
    claim = store.claim("w", 30)
    assert store.fail(claim["job_id"], "w", claim["lease_epoch"], "provider_unavailable", True)
    waiting = store.status(retry_job["job_id"])
    assert waiting["status"] == "retry_wait"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE background_jobs SET available_at=? WHERE job_id=?",
                     ((datetime.now(UTC) + timedelta(seconds=1)).isoformat(timespec="microseconds"), retry_job["job_id"]))
    # The retry delay is persisted; advance the test clock by moving its availability.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE background_jobs SET available_at=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds"), retry_job["job_id"]))
    second = store.claim("w2", 30)
    assert second["attempts"] == 2
    assert store.fail(second["job_id"], "w2", second["lease_epoch"], "invalid_schema", True)
    assert store.status(retry_job["job_id"])["status"] == "failed"


def test_deadline_is_terminal_without_running_handler(tmp_path):
    store = make_store(tmp_path)
    job = add(store, deadline=datetime.now(UTC) + timedelta(seconds=30))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE background_jobs SET deadline=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds"), job["job_id"]))
    assert store.claim("worker", 10) is None
    assert store.status(job["job_id"])["error_class"] == "deadline_exceeded"


def test_running_job_deadline_blocks_heartbeat_and_completion(tmp_path):
    store = make_store(tmp_path)
    job = add(store, deadline=datetime.now(UTC) + timedelta(seconds=30))
    claim = store.claim("worker", 30)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE background_jobs SET deadline=? WHERE job_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds"), job["job_id"]),
        )

    assert not store.heartbeat(job["job_id"], "worker", claim["lease_epoch"])
    assert not store.complete(job["job_id"], "worker", claim["lease_epoch"])
    status = store.status(job["job_id"])
    assert status["status"] == "failed"
    assert status["error_class"] == "deadline_exceeded"


def test_retry_failure_after_deadline_is_terminal(tmp_path):
    store = make_store(tmp_path)
    job = add(store, deadline=datetime.now(UTC) + timedelta(seconds=30))
    claim = store.claim("worker", 30)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE background_jobs SET deadline=? WHERE job_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds"), job["job_id"]),
        )

    assert not store.fail(job["job_id"], "worker", claim["lease_epoch"], "timeout", True)
    status = store.status(job["job_id"])
    assert status["status"] == "failed"
    assert status["error_class"] == "deadline_exceeded"


def test_cancel_prevents_claim_and_late_completion(tmp_path):
    store = make_store(tmp_path)
    job = add(store)
    claim = store.claim("worker", 30)
    assert store.cancel(job["job_id"])
    assert not store.complete(job["job_id"], "worker", claim["lease_epoch"])
    assert store.status(job["job_id"])["status"] == "cancelled"


def test_crash_recovery_claims_expired_running_job(tmp_path):
    store = make_store(tmp_path)
    job = add(store)
    crashed = store.claim("crashed-process", 0.02)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE background_jobs SET lease_expires_at=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds"), job["job_id"]))
    recovered = store.claim("restarted-process", 30)
    assert recovered["job_id"] == job["job_id"]
    assert recovered["attempts"] == 2
    assert recovered["lease_epoch"] == crashed["lease_epoch"] + 1
    assert recovered["lease_recovery_count"] == 1


def test_worker_runs_handler_and_marks_job_complete(tmp_path):
    store = make_store(tmp_path)
    job = add(store)
    seen = []
    worker = BackgroundJobWorker(store, {"memory.extract": seen.append}, worker_count=1)
    assert worker.run_one("test-worker")
    assert seen[0]["job_id"] == job["job_id"]
    assert store.status(job["job_id"])["status"] == "succeeded"


def test_scope_single_flight_and_aging_do_not_serialize_other_sessions(tmp_path):
    store = make_store(tmp_path)
    older = store.enqueue("context_compact", "session-a", "old", {}, priority=0)
    store.enqueue("context_compact", "session-a", "new", {}, priority=1)
    independent = store.enqueue("context_compact", "session-b", "other", {}, priority=1)
    with store._connect() as conn:
        conn.execute("UPDATE background_jobs SET created_at=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(minutes=15)).isoformat(), older["job_id"]))
    first = store.claim("first", 30)
    assert first["job_id"] == older["job_id"]
    second = store.claim("second", 30)
    assert second["job_id"] == independent["job_id"]
    assert store.claim("third", 30) is None


def test_two_workers_progress_independent_sessions_while_one_handler_is_slow(tmp_path):
    store = make_store(tmp_path)
    store.enqueue("kind", "slow-session", "slow", {})
    store.enqueue("kind", "fast-session", "fast", {})
    slow_entered, fast_entered, release = threading.Event(), threading.Event(), threading.Event()

    def handler(job):
        if job["scope_id"] == "slow-session":
            slow_entered.set()
            assert release.wait(timeout=5)
        else:
            fast_entered.set()

    worker = BackgroundJobWorker(store, {"kind": handler}, worker_count=2, poll_seconds=.01)
    worker.start()
    try:
        assert slow_entered.wait(timeout=2)
        assert fast_entered.wait(timeout=2)
    finally:
        release.set()
        worker.stop(timeout=2)
    assert all(job["status"] == "succeeded" for job in store.list())


def test_worker_stop_then_restart_does_not_overlap_a_live_handler(tmp_path):
    store = make_store(tmp_path)
    first = store.enqueue("kind", "scope-a", "one", {})
    second = store.enqueue("kind", "scope-b", "two", {})
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    guard = threading.Lock()
    state = {"calls": 0, "active": 0, "peak": 0}

    def handler(_job):
        with guard:
            index = state["calls"]
            state["calls"] += 1
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        entered[index].set()
        assert release[index].wait(timeout=3)
        with guard:
            state["active"] -= 1

    worker = BackgroundJobWorker(store, {"kind": handler}, worker_count=1, poll_seconds=0.01)
    worker.start()
    assert entered[0].wait(timeout=2)
    worker.stop(timeout=0.01)

    # Restart while the timed-out handler is still in flight. A second thread
    # must not be spawned alongside the retained live worker.
    worker.start()
    assert not entered[1].wait(timeout=0.1)
    release[0].set()
    for thread in worker._threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in worker._threads)
    worker.start()
    assert entered[1].wait(timeout=2)
    assert state["peak"] == 1
    release[1].set()
    worker.stop(timeout=2)
    assert store.status(first["job_id"])["status"] == "succeeded"
    assert store.status(second["job_id"])["status"] == "succeeded"


@pytest.mark.parametrize(
    ("error", "expected_class", "retryable"),
    [
        (TimeoutError("slow"), "timeout", True),
        (ConnectionError("offline"), "connection_unavailable", True),
        (LLMTimeoutError("slow provider"), "provider_timeout", True),
        (LLMNetworkError("offline provider"), "provider_network", True),
        (LLMProviderHTTPError(status_code=429, message="limited"), "provider_http_429", True),
        (LLMProviderHTTPError(status_code=529, message="overloaded"), "provider_http_529", True),
        (LLMProviderHTTPError(status_code=401, message="auth"), "provider_http_401", False),
        (sqlite3.OperationalError("database is locked"), "database_busy", True),
        (sqlite3.OperationalError("no such table: broken"), "database_operational_error", False),
        (ValueError("bad result"), "handler_error", False),
    ],
)
def test_worker_classifies_failures_without_retrying_permanent_errors(
    tmp_path, error, expected_class, retryable
):
    store = make_store(tmp_path)
    job = add(store)

    def fail(_job):
        raise error

    worker = BackgroundJobWorker(store, {"memory.extract": fail}, worker_count=1)
    assert worker.run_one("classification-worker")
    status = store.status(job["job_id"])
    assert status["error_class"] == expected_class
    assert status["status"] == ("retry_wait" if retryable else "failed")


def test_lease_recovery_metric_survives_schema_reopen(tmp_path):
    store = make_store(tmp_path)
    job = add(store)
    store.claim("lost-worker", 30)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE background_jobs SET lease_expires_at=? WHERE job_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds"), job["job_id"]),
        )
    store.claim("replacement", 30)

    reopened = BackgroundJobStore(store.db_path)
    reopened.ensure_schema()
    assert reopened.health_metrics()["lease_recovery_count"] == 1


def test_health_metrics_are_payload_free_and_aggregate_queue_state(tmp_path):
    store = make_store(tmp_path)
    queued = add(store, "queued", priority=-1)
    running = add(store, "running")
    failed = add(store, "failed")
    with sqlite3.connect(store.db_path) as conn:
        old = (datetime.now(UTC) - timedelta(seconds=30)).isoformat(timespec="microseconds")
        conn.execute("UPDATE background_jobs SET created_at=?,available_at=? WHERE job_id=?",
                     (old, old, queued["job_id"]))
        future = (datetime.now(UTC) + timedelta(seconds=1)).isoformat(timespec="microseconds")
        conn.execute("UPDATE background_jobs SET created_at=? WHERE job_id=?",
                     (future, running["job_id"]))
        conn.execute("UPDATE background_jobs SET created_at=? WHERE job_id=?",
                     (future, failed["job_id"]))
    claim = store.claim("worker", 30)
    assert claim["job_id"] == running["job_id"]
    assert store.fail(claim["job_id"], "worker", claim["lease_epoch"], "provider_unavailable", False)
    # Add a previously completed execution with known duration to verify aggregation.
    with sqlite3.connect(store.db_path) as conn:
        start = datetime.now(UTC) - timedelta(seconds=8)
        end = start + timedelta(seconds=3)
        conn.execute("UPDATE background_jobs SET status='succeeded',started_at=?,finished_at=? WHERE job_id=?",
                     (start.isoformat(timespec="microseconds"), end.isoformat(timespec="microseconds"), failed["job_id"]))

    metrics = store.health_metrics()
    assert metrics["queue_depth_by_status"] == {
        "queued": 1, "running": 0, "retry_wait": 0, "succeeded": 1, "failed": 1, "cancelled": 0,
    }
    assert metrics["oldest_queued_age_seconds"] >= 29
    assert metrics["expired_running_lease_count"] == 0
    assert metrics["recent_failure_categories"] == {"provider_unavailable": 1}
    assert metrics["lease_recovery_count"] == 0
    assert metrics["execution_duration_seconds"] == {"count": 1, "average_seconds": 3.0, "max_seconds": 3.0}
    assert "payload" not in str(metrics)


def test_health_metrics_bounds_failure_categories(tmp_path):
    store = make_store(tmp_path)
    assert store.health_metrics(recent_failure_limit=1)["recent_failure_categories"] == {}
    with pytest.raises(ValueError):
        store.health_metrics(recent_failure_limit=101)
