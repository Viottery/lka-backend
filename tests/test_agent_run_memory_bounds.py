from threading import Event

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewRequest


def _create_run(manager, index):
    return manager.create_run(
        session_id=f"bounded-run-session-{index}",
        user_input="test event cache",
        trace_id=f"bounded-run-trace-{index}",
    )


def _delta(manager, run_id, call_id, snapshot, delta="x"):
    snapshot += delta
    manager.append_event(
        run_id,
        "llm_delta",
        delta,
        stage="answer",
        payload={"llm_call_id": call_id, "delta": delta, "content_snapshot": snapshot},
    )
    return snapshot


def test_durable_stream_cache_keeps_deltas_without_repeated_prefixes(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "bounded-stream.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store, event_cache_limit=24)
    run = _create_run(manager, 1)
    manager.mark_running(run.run_id)
    snapshot = ""
    for _ in range(240):
        snapshot = _delta(manager, run.run_id, "stream-a", snapshot)

    cached = manager._events[run.run_id]
    assert len(cached) <= 24
    assert all("content_snapshot" not in event.payload for event in cached if event.type == "llm_delta")
    assert sum(len(event.payload.get("content_snapshot", "")) for event in cached) == 0
    manager.flush_events()

    page = manager.list_events(run.run_id, after_sequence=119, limit=24)
    assert [event.sequence for event in page] == list(range(120, 144))
    assert [event.payload["content_snapshot"] for event in page] == ["x" * n for n in range(120, 144)]
    manager.close()


def test_stream_snapshots_survive_paged_interleaved_calls_and_cold_hydration(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "paged-stream.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store, event_cache_limit=8)
    run = _create_run(first, 2)
    first.mark_running(run.run_id)
    snapshots = {"a": "", "b": ""}
    counts = {"a": 0, "b": 0}
    call_ids = []
    for index in range(70):
        call_id = "a" if index % 2 == 0 else "b"
        call_ids.append(call_id)
        counts[call_id] += 1
        snapshots[call_id] = _delta(first, run.run_id, call_id, snapshots[call_id])
    first.flush_events()

    reader = InMemoryAgentRunManager(durable_store=store, event_cache_limit=8)
    assert reader.get_run(run.run_id) is not None
    assert len(reader._events[run.run_id]) <= 8
    first_page = reader.list_events(run.run_id, after_sequence=0, limit=32)
    second_page = reader.list_events(run.run_id, after_sequence=32, limit=32)
    third_page = reader.list_events(run.run_id, after_sequence=64, limit=32)
    combined = first_page + second_page + third_page
    assert [event.sequence for event in combined] == list(range(1, 71))
    expected = []
    counts = {"a": 0, "b": 0}
    for call_id in call_ids:
        counts[call_id] += 1
        expected.append("x" * counts[call_id])
    assert [event.payload["content_snapshot"] for event in combined] == expected
    rewind = reader.list_events(run.run_id, after_sequence=0, limit=8)
    assert [event.sequence for event in rewind] == list(range(1, 9))
    assert [event.payload["content_snapshot"] for event in rewind] == expected[:8]
    jumped = reader.list_events(run.run_id, after_sequence=24, limit=12)
    assert [event.sequence for event in jumped] == list(range(25, 37))
    assert [event.payload["content_snapshot"] for event in jumped] == expected[24:36]
    first.close()
    reader.close()


def test_cache_eviction_keeps_durable_history_and_review_controls(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "terminal-cache.sqlite3")
    manager = InMemoryAgentRunManager(
        durable_store=store, terminal_cache_limit=1, event_cache_limit=4,
    )
    first = _create_run(manager, 3)
    manager.mark_running(first.run_id)
    manager.append_event(first.run_id, "progress", "before review")
    review = manager.create_safety_review(SafetyReviewRequest(
        review_id="bounded-cache-review",
        run_id=first.run_id,
        session_id=first.session_id,
        trace_id=first.trace_id,
        invocation_id="bounded-cache-invocation",
        tool_name="filesystem.edit_file",
        mode=SafetyReviewMode.MANUAL,
        reason="Write requires confirmation.",
        created_at=first.created_at,
    ))
    assert manager.get_run(first.run_id).status == AgentRunStatus.WAITING_CONFIRMATION
    manager.decide_safety_review(
        review_id=review.review_id, decision=SafetyReviewDecision.APPROVE,
        decided_by="test", reason="approved",
    )
    manager.append_event(first.run_id, "progress", "after review")
    manager.complete_run(first.run_id, result_snapshot={"answer": "done"})

    second = _create_run(manager, 4)
    manager.mark_running(second.run_id)
    manager.complete_run(second.run_id, result_snapshot={"answer": "second"})
    assert first.run_id not in manager._runs
    assert len(manager._runs) == 1

    restored = manager.get_run(first.run_id)
    assert restored is not None and restored.status == AgentRunStatus.COMPLETED
    assert manager.get_safety_review(review.review_id).status.value == "approved"
    manager.append_event(first.run_id, "audit", "append after cold restore")
    sequences = [event.sequence for event in manager.list_events(first.run_id)]
    assert sequences == list(range(1, len(sequences) + 1))
    assert manager.list_events(first.run_id)[-1].message == "append after cold restore"
    manager.close()


def test_aggregation_dedup_survives_event_tail_eviction(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "aggregate-idempotency.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store, event_cache_limit=2)
    run = _create_run(manager, 6)
    manager.mark_running(run.run_id)
    arguments = {
        "plan": {"plan_id": "plan-1"},
        "aggregate": {"status": "complete", "task_results": [], "conflicts": []},
        "verification": {"status": "verified"},
        "replan_required": False,
    }
    original = manager.record_multi_agent_aggregation(run.run_id, **arguments)
    snapshot = ""
    for _ in range(12):
        snapshot = _delta(manager, run.run_id, "post-aggregate", snapshot)
    duplicate = manager.record_multi_agent_aggregation(run.run_id, **arguments)
    assert duplicate.event_id == original.event_id
    assert sum(event.type == "multi_agent_aggregated"
               for event in manager.list_events(run.run_id)) == 1
    manager.close()


def test_hot_event_pages_and_latest_poll_do_not_query_sqlite(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "hot-event-pages.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store, event_cache_limit=16)
    run = _create_run(manager, 7)
    manager.mark_running(run.run_id)
    snapshot = ""
    for _ in range(12):
        snapshot = _delta(manager, run.run_id, "hot-stream", snapshot)
    manager.flush_events()

    reads = 0
    list_events = store.list_events
    iter_events = store.iter_events

    def count_reads(*args, **kwargs):
        nonlocal reads
        reads += 1
        return list_events(*args, **kwargs)

    store.list_events = count_reads

    def count_prefix_reads(*args, **kwargs):
        nonlocal reads
        reads += 1
        return iter_events(*args, **kwargs)

    store.iter_events = count_prefix_reads
    tail = manager._events[run.run_id]
    page = manager.list_events(run.run_id, after_sequence=tail[-3].sequence - 1, limit=2)
    assert [event.sequence for event in page] == [tail[-3].sequence, tail[-2].sequence]
    assert manager.list_events(run.run_id, after_sequence=tail[-1].sequence, limit=16) == []
    assert manager.list_events(run.run_id, after_sequence=tail[-1].sequence, limit=16) == []
    previous_latest = tail[-1].sequence
    snapshot = _delta(manager, run.run_id, "hot-stream", snapshot, delta="y")
    newest = manager.list_events(run.run_id, after_sequence=previous_latest, limit=16)
    assert newest[-1].payload["content_snapshot"] == snapshot
    snapshot = _delta(manager, run.run_id, "hot-stream", snapshot, delta="z")
    following = manager.list_events(run.run_id, after_sequence=newest[-1].sequence, limit=16)
    assert following[-1].payload["content_snapshot"] == snapshot
    assert reads == 0
    manager.close()


def test_hot_cache_gap_falls_back_to_durable_contiguous_prefix(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "sparse-refresh.sqlite3")
    primary = InMemoryAgentRunManager(durable_store=store, event_cache_limit=16)
    run = _create_run(primary, 8)
    for index in range(10):
        primary.append_event(run.run_id, "progress", f"primary-{index}")
    primary.flush_events()

    remote = InMemoryAgentRunManager(durable_store=store, event_cache_limit=64)
    assert remote.get_run(run.run_id) is not None
    latest = None
    for index in range(40):
        latest = remote.append_event(run.run_id, "progress", f"remote-{index}")
    remote.flush_events()
    primary.refresh_after_transaction(
        store.load_run(run.run_id), committed_events=[latest.model_dump(mode="json")],
    )

    page = primary.list_events(run.run_id, after_sequence=10, limit=5)
    assert [event.sequence for event in page] == [11, 12, 13, 14, 15]
    assert [event.message for event in page] == [f"remote-{index}" for index in range(5)]
    primary.close()
    remote.close()


def test_control_only_page_does_not_advance_stream_snapshot_cursor(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "control-page-snapshot.sqlite3")
    writer = InMemoryAgentRunManager(durable_store=store, event_cache_limit=8)
    run = _create_run(writer, 9)
    snapshot = ""
    snapshot = _delta(writer, run.run_id, "skipped-call", snapshot)
    snapshot = _delta(writer, run.run_id, "skipped-call", snapshot)
    writer.append_event(run.run_id, "progress", "between stream deltas")
    snapshot = _delta(writer, run.run_id, "skipped-call", snapshot)
    writer.flush_events()

    reader = InMemoryAgentRunManager(durable_store=store, event_cache_limit=8)
    assert reader.get_run(run.run_id) is not None
    first = reader.list_events(run.run_id, after_sequence=0, limit=1)
    control = reader.list_events(run.run_id, after_sequence=2, limit=1)
    final = reader.list_events(run.run_id, after_sequence=3, limit=1)
    assert first[0].payload["content_snapshot"] == "x"
    assert control[0].type == "progress"
    assert final[0].payload["content_snapshot"] == "xxx"
    writer.close()
    reader.close()


def test_snapshot_prefix_merges_pending_and_later_durable_deltas_in_sequence(tmp_path):
    db_path = tmp_path / "interleaved-pending-prefix.sqlite3"
    primary_store = SqliteAgentRunStore(db_path)
    primary = InMemoryAgentRunManager(durable_store=primary_store, event_cache_limit=16)
    run = _create_run(primary, 10)
    snapshot = _delta(primary, run.run_id, "shared-call", "", delta="A")
    primary.flush_events()

    started, release = Event(), Event()
    save_events = primary_store.save_events

    def blocked_save(events):
        if all(event.get("type") == "llm_delta" for event in events):
            started.set()
            assert release.wait(timeout=5)
        return save_events(events)

    primary_store.save_events = blocked_save
    snapshot = _delta(primary, run.run_id, "shared-call", snapshot, delta="B")
    assert started.wait(timeout=2)
    primary.append_event(run.run_id, "progress", "synchronous control event")

    remote_store = SqliteAgentRunStore(db_path)
    remote = InMemoryAgentRunManager(durable_store=remote_store, event_cache_limit=16)
    assert remote.get_run(run.run_id) is not None
    remote_event = remote.append_event(
        run.run_id, "llm_delta", "C", stage="answer",
        payload={"llm_call_id": "shared-call", "delta": "C", "content_snapshot": "C"},
    )
    remote.flush_events()
    primary.refresh_after_transaction(
        remote_store.load_run(run.run_id), committed_events=[remote_event.model_dump(mode="json")],
    )

    snapshot = _delta(primary, run.run_id, "shared-call", snapshot + "C", delta="D")
    page = primary.list_events(run.run_id, after_sequence=4, limit=1)
    assert [event.sequence for event in page] == [5]
    assert page[0].payload["content_snapshot"] == "ABCD"

    release.set()
    primary.flush_events()
    primary.close()
    remote.close()


def test_sse_pages_do_not_skip_pending_deltas_beyond_cache_tail(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "pending-deltas.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store, event_cache_limit=16)
    run = _create_run(manager, 5)
    manager.mark_running(run.run_id)
    started, release = Event(), Event()
    save_events = store.save_events

    def blocked_save(events):
        started.set()
        assert release.wait(timeout=5)
        return save_events(events)

    store.save_events = blocked_save
    snapshot = ""
    for _ in range(120):
        snapshot = _delta(manager, run.run_id, "blocked-stream", snapshot)
    assert started.wait(timeout=2)
    # The durable log is behind a cache tail that no longer contains seq 1.
    # The manager must wait for that gap instead of returning a later sequence.
    assert manager.list_events(run.run_id, after_sequence=0, limit=32) == []
    release.set()
    manager.flush_events()

    collected = []
    after = 0
    while True:
        page = manager.list_events(run.run_id, after_sequence=after, limit=32)
        if not page:
            break
        collected.extend(event.sequence for event in page)
        after = page[-1].sequence
    assert collected == list(range(1, 121))
    manager.close()
