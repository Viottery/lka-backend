"""Checkpoint history remains exact while growing logs share immutable chunks."""

import json
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore


@pytest.fixture
def graph_store(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="chunks", user_input="synthetic")
    return store, manager, run


def test_growing_snapshots_share_bytes_and_restore_every_checkpoint(graph_store):
    store, _, run = graph_store
    data = {"context": {"source": "private" * 4096}, "events": [], "empty": [], "pending": None}
    saved, full_bytes = [], 0
    for i in range(48):
        data["events"].append({"i": i, "result": str(i) + "x" * 16384})
        full_bytes += len(json.dumps(data).encode())
        saved.append(store.put_graph_runtime(run.run_id, data, created_at=datetime.now(UTC).isoformat()))
    with sqlite3.connect(store.db_path) as conn:
        total = conn.execute("SELECT sum(length(CAST(payload AS BLOB))) FROM agent_run_artifacts WHERE run_id=?",
                             (run.run_id,)).fetchone()[0]
    assert total < full_bytes * .3, (total, full_bytes)
    restored = SqliteAgentRunStore(store.db_path)
    for i in (0, 7, 8, 31, 47):
        result = restored.load_graph_runtime(saved[i]["artifact_id"])
        assert result == {**data, "events": data["events"][:i + 1]}
    # Updating a prior entry produces a new chunk without changing old checkpoints.
    data["events"][0]["result"] = "changed"
    changed = restored.put_graph_runtime(run.run_id, data, created_at=datetime.now(UTC).isoformat())
    assert restored.load_graph_runtime(changed["artifact_id"]) == data
    assert store.load_graph_runtime(saved[0]["artifact_id"])["events"][0]["result"] != "changed"


def test_identical_checkpoint_is_read_only_and_legacy_is_readable(graph_store, monkeypatch):
    store, _, run = graph_store
    data = {"events": [{"text": "private"}], "status": "running"}
    ref = store.put_graph_runtime(run.run_id, data, created_at="now")
    from app.core import agent_storage
    original, sql = agent_storage.connect, []

    def traced(path):
        conn = original(path); conn.set_trace_callback(sql.append); return conn

    monkeypatch.setattr(agent_storage, "connect", traced)
    assert store.put_graph_runtime(run.run_id, data, created_at="later") == ref
    assert not any(line.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE")) for line in sql)
    legacy = store.put_artifact(artifact_id="legacy", run_id=run.run_id, kind="agent_graph_runtime",
                                payload=data, summary="legacy", created_at="before")
    assert store.load_graph_runtime(legacy["artifact_id"]) == data


def test_shared_parts_restore_independent_mutable_fields(graph_store):
    store, _, run = graph_store
    data = {"one": {"items": []}, "two": {"items": []}, "repeated": [{"items": []} for _ in range(16)],
            "first": [{"items": []}], "second": [{"items": []}]}
    ref = store.put_graph_runtime(run.run_id, data, created_at="now")
    restored = store.load_graph_runtime(ref["artifact_id"])
    restored["one"]["items"].append("changed")
    restored["first"][0]["items"].append("changed")
    restored["repeated"][0]["items"].append("changed")
    assert restored["two"] == {"items": []}
    assert restored["second"] == [{"items": []}]
    assert restored["repeated"][8] == {"items": []}
    assert store.load_graph_runtime(ref["artifact_id"]) == data


def test_parts_are_bound_to_run_and_checksum_and_pruned_only_after_terminal(graph_store):
    store, manager, run = graph_store
    first = store.put_graph_runtime(run.run_id, {"events": ["old"]}, created_at="first")
    current = store.put_graph_runtime(run.run_id, {"events": ["new"]}, created_at="current")
    assert store.prune_graph_runtime_artifacts(run.run_id, keep_artifact_id=current["artifact_id"]) == 0
    manager.complete_run(run.run_id, result_snapshot={"answer": "done"})
    assert store.prune_graph_runtime_artifacts(run.run_id, keep_artifact_id=current["artifact_id"]) == 2
    assert store.load_graph_runtime(first["artifact_id"]) is None
    assert store.load_graph_runtime(current["artifact_id"]) == {"events": ["new"]}
    part = store.load_artifact(current["artifact_id"])["fields"]["events"]["parts"][0]["artifact_id"]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE agent_run_artifacts SET payload=? WHERE artifact_id=?", ('{"value":["tampered"]}', part))
    with pytest.raises(ValueError, match="checksum"):
        store.load_graph_runtime(current["artifact_id"])
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE agent_run_artifacts SET run_id=? WHERE artifact_id=?", ("another-run", part))
    with pytest.raises(ValueError, match="missing or belongs"):
        store.load_graph_runtime(current["artifact_id"])


def test_new_manifest_revalidates_parts_removed_after_preflight(graph_store, monkeypatch):
    store, manager, run = graph_store
    store.put_graph_runtime(run.run_id, {"events": ["old"]}, created_at="old")
    kept = store.put_graph_runtime(run.run_id, {"events": ["kept"]}, created_at="kept")
    manager.complete_run(run.run_id, result_snapshot={"answer": "done"})
    from app.core import agent_storage
    original, pending = agent_storage.connect, [True]

    class Connection:
        def __init__(self, path):
            self.conn = original(path)
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE" and pending[0]:
                pending[0] = False
                store.prune_graph_runtime_artifacts(run.run_id, keep_artifact_id=kept["artifact_id"])
            return self.conn.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.conn.__exit__(*args)

    monkeypatch.setattr(agent_storage, "connect", Connection)
    ref = store.put_graph_runtime(run.run_id, {"events": ["old"], "phase": "new"}, created_at="new")
    assert not pending[0]
    assert store.load_graph_runtime(ref["artifact_id"]) == {"events": ["old"], "phase": "new"}


def test_manifest_checksum_and_read_snapshot_survive_concurrent_gc(graph_store, monkeypatch):
    store, manager, run = graph_store
    old = store.put_graph_runtime(run.run_id, {"events": ["old"]}, created_at="first")
    current = store.put_graph_runtime(run.run_id, {"events": ["current"]}, created_at="current")
    from app.core import agent_storage
    original = agent_storage.connect
    reading, resume, result, errors = threading.Event(), threading.Event(), [], []

    def traced(path):
        conn = original(path)
        if threading.current_thread().name == "checkpoint-reader":
            def trace(sql):
                if sql.startswith("SELECT artifact_id,content_hash,payload"):
                    reading.set()
                    assert resume.wait(3)
            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(agent_storage, "connect", traced)

    def read():
        try:
            result.append(store.load_graph_runtime(old["artifact_id"]))
        except Exception as error:  # noqa: BLE001 - propagate thread failures through the test assertion.
            errors.append(error)

    reader = threading.Thread(target=read, name="checkpoint-reader")
    reader.start()
    try:
        assert reading.wait(3)
        manager.complete_run(run.run_id, result_snapshot={"answer": "done"})
        store.prune_graph_runtime_artifacts(run.run_id, keep_artifact_id=current["artifact_id"])
    finally:
        resume.set(); reader.join(3)
    assert not reader.is_alive() and not errors
    assert result == [{"events": ["old"]}]
    assert store.load_graph_runtime(old["artifact_id"]) is None
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE agent_run_artifacts SET payload=? WHERE artifact_id=?",
                     ('{"graph_runtime_format":2,"fields":{}}', current["artifact_id"]))
    with pytest.raises(ValueError, match="manifest checksum"):
        store.load_graph_runtime(current["artifact_id"])
