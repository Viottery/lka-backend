"""Token batches retain complete ordered history and flush before completion."""

import json

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore


def test_burst_deltas_batch_without_prefix_copies_and_flush_is_durable(tmp_path, monkeypatch):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="batch", user_input="synthetic")
    save, batches = store.save_events, []

    def counted(events):
        batches.append(events)
        save(events)

    monkeypatch.setattr(store, "save_events", counted)
    try:
        for i in range(150):
            manager.append_event(run.run_id, "llm_delta", "token", payload={
                "delta": "x", "content_snapshot": "x" * (i + 1), "llm_call_id": "synthetic"})
        manager.flush_events()
        assert 1 < len(batches) < 10
        assert sum(map(len, batches)) == 150
        assert all(len(batch) <= 64 for batch in batches)
        assert all("content_snapshot" not in event["payload"] for batch in batches for event in batch)
        assert [event["sequence"] for event in store.list_events(run.run_id)] == list(range(1, 151))
    finally:
        manager.close()


def test_delta_byte_budget_keeps_oversized_single_event_and_carry(tmp_path, monkeypatch):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="bytes", user_input="synthetic")
    save, batches = store.save_events, []

    def counted(events):
        batches.append(events); save(events)

    monkeypatch.setattr(store, "save_events", counted)
    values = ["x" * 150000, "y" * 150000, "z" * 300000, "end"]
    try:
        for value in values:
            manager.append_event(run.run_id, "llm_delta", "token", payload={"delta": value})
        manager.close()
        assert all(len(batch) == 1 or len(json.dumps(batch).encode()) < 262144 for batch in batches)
        assert [event["payload"]["delta"] for event in store.list_events(run.run_id)] == values
    finally:
        manager.close()
