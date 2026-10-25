"""Persistence retries never repeat an Agent tool effect."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

import app.core.agent_storage as storage
from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_tool_graph import AgentToolLifecycleGraph
from app.core.session_progress_cache import SessionProgressCacheService
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult, ToolSpec
from app.storage.sqlite_write_queue import SqliteWriteQueueError


@pytest.fixture
def store(tmp_path):
    return SqliteAgentRunStore(tmp_path / "lka.sqlite3")


def claim(store, **changes):
    args = {"invocation_id": "inv", "run_id": "run", "tool_name": "test.write",
                "tool_input": {"value": 1}, "claimed_at": "now"}
    return store.claim_tool_invocation(**(args | changes))


def short_connections(monkeypatch, store, on_busy=None):
    attempts = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            attempts.append(sql)
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError:
                if on_busy:
                    on_busy()
                raise

    def connect(path):
        conn = sqlite3.connect(path, timeout=0.01, factory=Connection)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(storage, "connect", connect)
    return attempts


def test_concurrent_claim_is_atomic_and_scope_bound(store):
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        return claim(store)["status"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: attempt(), range(2))) == ["claimed", "uncertain"]
    for changes in ({"run_id": "other"}, {"tool_name": "other.write"}, {"tool_input": {"value": 2}}):
        assert claim(store, **changes) == {"status": "conflict"}


def test_receipt_completion_accepts_only_identical_canonical_result(store):
    claim(store)
    result = {"output": {"b": 2, "a": 1}, "status": "completed"}
    store.complete_tool_invocation(invocation_id="inv", result=result, completed_at="first")
    store.complete_tool_invocation(invocation_id="inv", result=dict(reversed(list(result.items()))), completed_at="second")
    with pytest.raises(RuntimeError, match="not claimable"):
        store.complete_tool_invocation(invocation_id="inv", result={"status": "failed"}, completed_at="third")
    with closing(storage.connect(store.db_path)) as conn:
        assert conn.execute("SELECT updated_at FROM agent_tool_invocations").fetchone()[0] == "first"
    assert claim(store)["result"] == result


@pytest.mark.parametrize("release", [True, False])
def test_receipt_contention_never_repeats_tool_effect(store, monkeypatch, release):
    blocker = sqlite3.connect(store.db_path)
    calls = []

    class Tool:
        spec = ToolSpec(name="test.write", type="test", description="Counter", read_only=False)

        def invoke(self, *, invocation, context):
            calls.append(True)
            blocker.execute("BEGIN IMMEDIATE")
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="completed", output={"calls": len(calls)})

    registry = ToolRegistry()
    registry.register_tool(Tool())
    graph = AgentToolLifecycleGraph(tool_executor=ToolExecutor(registry),
        review_tool_call=lambda *_: (None, None), append_progress=lambda *_: None,
        raise_if_cancel_requested=lambda: None, artifact_store=store)
    attempts = short_connections(monkeypatch, store, blocker.rollback if release else None)
    request = {"invocation_id": "inv", "run_id": "run", "tool_name": "test.write",
                   "tool_input": {"value": 1}, "context": ToolContext(session_id="session", safety_review_approved=True)}
    try:
        if release:
            assert graph.run(**request).status == "completed"
        else:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                graph.run(**request)
        blocker.rollback()
        assert graph.run(**request).status == ("completed" if release else "failed")
        assert len(calls) == 1
        assert claim(store)["status"] == ("completed" if release else "uncertain")
        assert sum(sql == "BEGIN IMMEDIATE" for sql in attempts) >= 3
    finally:
        blocker.rollback()
        blocker.close()


def test_artifact_contention_retries_fresh_connection_and_exhaustion_surfaces(store, monkeypatch):
    with closing(sqlite3.connect(store.db_path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        short_connections(monkeypatch, store, blocker.rollback)
        store.put_artifact(artifact_id="artifact", run_id="run", kind="tool_result",
                           payload={"value": 1}, summary="test", created_at="now")
        assert store.load_artifact("artifact") == {"value": 1}
        blocker.execute("BEGIN IMMEDIATE")
        short_connections(monkeypatch, store)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.put_artifact(artifact_id="artifact", run_id="run", kind="tool_result",
                               payload={"value": 2}, summary="test", created_at="later")
        blocker.rollback()
        assert store.load_artifact("artifact") == {"value": 1}


def test_failed_event_is_not_published_or_assigned_a_sequence(store, monkeypatch):
    manager = InMemoryAgentRunManager(durable_store=store)
    try:
        run = manager.create_run(session_id="session", user_input="test")
        before = manager.list_events(run.run_id)
        with closing(sqlite3.connect(store.db_path)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            short_connections(monkeypatch, store)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                manager.append_event(run.run_id, "tool_completed", "done")
            assert manager.list_events(run.run_id) == before
            blocker.rollback()
        event = manager.append_event(run.run_id, "tool_completed", "done")
        assert event.sequence == len(before) + 1
        assert store.list_events(run.run_id)[-1]["sequence"] == event.sequence
    finally:
        manager.close()


def test_busy_derived_index_is_deferred_with_warning_and_other_errors_surface(store, monkeypatch, caplog):
    # Exercise the public record boundary with a real locked artifact write;
    # authorization/index construction are covered by test_session_progress_cache.
    service = object.__new__(SessionProgressCacheService)
    def write(**_):
        store.put_artifact(artifact_id="index", run_id="run", kind="session_progress",
                           payload={"operations": []}, summary="index", created_at="now")
    monkeypatch.setattr(service, "_record", write)
    with closing(sqlite3.connect(store.db_path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        short_connections(monkeypatch, store)
        service.record(context=ToolContext(session_id="session", run_id="run"), tool_events=[])
        assert "indexing deferred" in caplog.text
        assert store.load_artifact("index") is None
        blocker.rollback()
    service.record(context=ToolContext(session_id="session", run_id="run"), tool_events=[])
    assert store.load_artifact("index") == {"operations": []}
    def bug(**_):
        raise sqlite3.OperationalError("syntax error")
    monkeypatch.setattr(service, "_record", bug)
    with pytest.raises(sqlite3.OperationalError, match="syntax"):
        service.record(context=ToolContext(session_id="session", run_id="run"), tool_events=[])


def test_write_queue_wait_failure_defers_derived_index(store, monkeypatch, caplog):
    service = object.__new__(SessionProgressCacheService)

    def queue_timeout(**_):
        raise SqliteWriteQueueError("SQLite write queue wait timed out")

    monkeypatch.setattr(service, "_record", queue_timeout)
    service.record(context=ToolContext(session_id="session", run_id="run"), tool_events=[])
    assert "indexing deferred" in caplog.text
