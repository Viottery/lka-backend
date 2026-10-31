"""SSE paging must drain every page, including a late terminal tail."""
import asyncio
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.api.routes.agent import _agent_turn_event_stream
from app.core.agent_runs import AgentRunEvent, AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore


@pytest.mark.parametrize("late_tail", [False, True])
def test_terminal_stream_drains_more_than_three_pages_in_sequence(late_tail):
    events = [
        AgentRunEvent(
            event_id=f"event-{i}", run_id="run", sequence=i, type="tool_completed",
            message="done", created_at="2026-10-28T00:00:00Z",
            payload={"tool_name": "example.read", "status": "completed", "body": "PRIVATE"},
        )
        for i in range(1, 402)
    ]
    reads = []

    def list_events(run_id, *, after_sequence, limit):
        assert run_id == "run"
        assert limit == 128
        reads.append(after_sequence)
        if late_tail and len(reads) == 1:
            return []
        return [e for e in events if e.sequence > after_sequence][:limit]

    async def connected():
        return False

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(
            agent_run_manager=SimpleNamespace(
                list_events=list_events,
                get_run=lambda _: SimpleNamespace(status=AgentRunStatus.COMPLETED),
            ),
        ))),
        is_disconnected=connected,
    )

    async def consume():
        return [frame async for frame in _agent_turn_event_stream(request=request, run_id="run")]

    frames = asyncio.run(consume())
    assert len(frames) == len(events)
    assert all(f'"sequence": {i}' in frame or f'"sequence":{i}' in frame
               for i, frame in enumerate(frames, 1))
    assert all("PRIVATE" not in frame for frame in frames)
    assert reads[:2] == ([0, 0] if late_tail else [0, 128])


def test_terminal_sse_waits_only_for_its_run_not_an_unrelated_live_writer(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "independent-streams.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    completed = manager.create_run(session_id="done", user_input="done")
    manager.mark_running(completed.run_id)
    manager.complete_run(completed.run_id)
    live = manager.create_run(session_id="live", user_input="live")
    manager.mark_running(live.run_id)
    started, release, flushed = Event(), Event(), Event()
    save = store.save_events

    def blocked_save(events):
        started.set()
        assert release.wait(5)
        return save(events)

    store.save_events = blocked_save
    manager.append_event(live.run_id, "llm_delta", "x",
                         payload={"delta": "x", "llm_call_id": "live", "content_snapshot": "x"})
    assert started.wait(2)

    async def connected():
        return False

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(agent_run_manager=manager))),
        is_disconnected=connected,
    )

    async def consume():
        return [frame async for frame in _agent_turn_event_stream(
            request=request, run_id=completed.run_id,
        )]

    flusher = Thread(target=lambda: (manager.flush_run_events(live.run_id), flushed.set()))
    flusher.start()
    try:
        assert not flushed.wait(0.05)
        # The completed stream must close while the unrelated save is blocked.
        asyncio.run(asyncio.wait_for(consume(), timeout=1))
        assert not flushed.is_set()
    finally:
        release.set()
        flusher.join(2)
        manager.close()
    assert flushed.is_set()
    assert not manager._pending_run_events


def test_failed_writer_wakes_run_waiter_and_retains_failure(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "failed-stream.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="failed", user_input="failed")

    def failed_save(_):
        raise RuntimeError("injected persistence failure")

    store.save_events = failed_save
    manager.append_event(run.run_id, "llm_delta", "x", payload={"delta": "x"})
    try:
        with pytest.raises(RuntimeError, match="Agent event persistence failed"):
            manager.flush_run_events(run.run_id)
        assert not manager._pending_run_events
        assert isinstance(manager._event_writer_error, RuntimeError)
    finally:
        with pytest.raises(RuntimeError, match="Agent event persistence failed"):
            manager.close()
        assert not manager._event_writer.is_alive()


def test_close_rejects_producers_before_enqueuing_stop_sentinel(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "close-admission.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="close", user_input="close")
    started, release, closing = Event(), Event(), Event()
    save = store.save_events

    def blocked_save(events):
        started.set()
        assert release.wait(5)
        return save(events)

    store.save_events = blocked_save
    manager.append_event(run.run_id, "llm_delta", "a", payload={"delta": "a"})
    assert started.wait(2)
    flush = manager.flush_events

    def announce_flush():
        closing.set()
        flush()

    manager.flush_events = announce_flush
    errors = []

    def append_after_close_started():
        try:
            manager.append_event(run.run_id, "llm_delta", "b", payload={"delta": "b"})
        except RuntimeError as exc:
            errors.append(str(exc))

    closer = Thread(target=manager.close)
    producer = Thread(target=append_after_close_started)
    closer.start()
    try:
        assert closing.wait(2)
        producer.start()
    finally:
        release.set()
        closer.join(2)
        if producer.ident is not None:
            producer.join(2)
    assert errors == ["Agent event writer is closed."]
    assert not closer.is_alive() and not producer.is_alive()
    assert not manager._event_writer.is_alive()
    assert not manager._pending_run_events
    assert manager._pending_delta_events.unfinished_tasks == 0
