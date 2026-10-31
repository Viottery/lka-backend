from __future__ import annotations

import threading
import time

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.safety import (
    SafetyReviewDecision,
    SafetyReviewMode,
    SafetyReviewRequest,
    SafetyReviewStatus,
)


def _review(manager, index, *, mode=SafetyReviewMode.MANUAL):
    run = manager.create_run(
        session_id=f"safety-cache-session-{index}",
        user_input=f"Review request {index}",
        trace_id=f"safety-cache-trace-{index}",
    )
    manager.mark_running(run.run_id)
    review = manager.create_safety_review(SafetyReviewRequest(
        review_id=f"safety-cache-review-{index}",
        run_id=run.run_id,
        session_id=run.session_id,
        trace_id=run.trace_id,
        invocation_id=f"safety-cache-call-{index}",
        tool_name="filesystem.edit_file",
        tool_input={"path": f"file-{index}.txt", "content": "synthetic"},
        mode=mode,
        reason="Test review",
        created_at=run.created_at,
    ))
    return run, review


def _decide(manager, review_id, decision=SafetyReviewDecision.REJECT):
    return manager.decide_safety_review_with_transition(
        review_id=review_id,
        decision=decision,
        decided_by="test",
        reason="synthetic decision",
    )


def test_durable_decided_cache_is_bounded_and_history_reads_through(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "safety-cache.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store, safety_review_cache_limit=2)
    save_calls = []
    save_review = store.save_review

    def counted_save_review(*args, **kwargs):
        save_calls.append(1)
        return save_review(*args, **kwargs)

    store.save_review = counted_save_review
    reviews = []
    for index in range(5):
        _run, review = _review(manager, index)
        _decide(manager, review.review_id)
        reviews.append(review)

    assert len(manager.safety_reviews._decided_lru) == 2
    assert len(manager.safety_reviews._reviews) == 2
    assert sum(map(len, manager.safety_reviews._run_reviews.values())) == 2

    writes_before_reads = len(save_calls)
    for review in reviews:
        listed = manager.list_safety_reviews(review.run_id)
        assert [item.review_id for item in listed] == [review.review_id]
        assert any(
            event.type == "safety_review_decided"
            for event in manager.list_events(review.run_id)
        )
    assert len(manager.safety_reviews._reviews) == 2

    old = manager.get_safety_review(reviews[0].review_id)
    assert old is not None and old.status == SafetyReviewStatus.REJECTED
    assert len(manager.safety_reviews._reviews) == 2
    assert len(save_calls) == writes_before_reads
    manager.close()


def test_pending_reviews_and_active_waiters_survive_decided_eviction(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "safety-waiter-cache.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store, safety_review_cache_limit=1)
    _run_a, review_a = _review(manager, 1, mode=SafetyReviewMode.MANUAL)
    listed_pending = manager.list_pending_safety_reviews()
    assert [item.review_id for item in listed_pending] == [review_a.review_id]
    assert manager.safety_reviews.get(review_a.review_id).status == SafetyReviewStatus.PENDING
    reader = InMemoryAgentRunManager(durable_store=store, safety_review_cache_limit=1)
    assert [item.review_id for item in reader.list_pending_safety_reviews()] == [review_a.review_id]
    assert reader.safety_reviews.get(review_a.review_id) is None
    reader.close()

    started = threading.Event()
    received = []

    def wait_for_review():
        started.set()
        received.append(manager.safety_reviews.wait_for_decision(
            review_a.review_id, timeout_seconds=0.02
        ))

    waiter = threading.Thread(target=wait_for_review)
    waiter.start()
    assert started.wait(2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with manager.safety_reviews._condition:
            if manager.safety_reviews._active_waiters.get(review_a.review_id):
                break
        time.sleep(0.005)
    else:
        raise AssertionError("review waiter did not register")

    # Keep the waiter from consuming its notification until overflow and the
    # cache-miss replay have both exercised the pinning behavior.
    with manager.safety_reviews._condition:
        _decide(manager, review_a.review_id, SafetyReviewDecision.APPROVE)
        _run_b, review_b = _review(manager, 2)
        _decide(manager, review_b.review_id)
        assert manager.safety_reviews.get(review_a.review_id) is not None
        assert manager.safety_reviews.get(review_b.review_id) is None
        replayed = manager.decide_safety_review(
            review_id=review_b.review_id,
            decision=SafetyReviewDecision.APPROVE,
            decided_by="retry",
            reason="idempotent replay",
        )
        assert replayed.status == SafetyReviewStatus.REJECTED

    waiter.join(2)
    assert not waiter.is_alive()
    assert received[0].status == SafetyReviewStatus.APPROVED
    assert review_a.review_id not in manager.safety_reviews._active_waiters
    manager.close()


def test_concurrent_durable_decisions_transition_once(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "safety-concurrent-cache.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store, safety_review_cache_limit=1)
    _run, review = _review(manager, 3, mode=SafetyReviewMode.MANUAL)
    barrier = threading.Barrier(3)
    outcomes = []

    def decide(value):
        barrier.wait()
        outcomes.append(_decide(manager, review.review_id, value)[1])

    threads = [
        threading.Thread(target=decide, args=(SafetyReviewDecision.APPROVE,)),
        threading.Thread(target=decide, args=(SafetyReviewDecision.REJECT,)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(3)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == [False, True]
    persisted = manager.get_safety_review(review.review_id)
    assert persisted is not None
    assert persisted.status in {SafetyReviewStatus.APPROVED, SafetyReviewStatus.REJECTED}
    manager.close()


def test_legacy_decision_hydrates_review_from_durable_storage(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "safety-legacy-hydration.sqlite3")
    writer = InMemoryAgentRunManager(durable_store=store, safety_review_cache_limit=1)
    _run, review = _review(writer, 4)

    reader = InMemoryAgentRunManager(durable_store=store, safety_review_cache_limit=1)
    assert reader.safety_reviews.get(review.review_id) is None
    decided = reader.decide_safety_review(
        review_id=review.review_id,
        decision=SafetyReviewDecision.APPROVE,
        decided_by="test",
        reason="legacy path",
    )
    assert decided.status == SafetyReviewStatus.APPROVED
    persisted = reader.get_safety_review(review.review_id)
    assert persisted is not None and persisted.status == SafetyReviewStatus.APPROVED
    reader.close()
    writer.close()


def test_non_durable_review_history_remains_unbounded():
    manager = InMemoryAgentRunManager(safety_review_cache_limit=1)
    reviews = []
    for index in range(3):
        run, review = _review(manager, index)
        manager.safety_reviews.decide(
            review_id=review.review_id,
            decision=SafetyReviewDecision.REJECT,
            decided_by="test",
            reason="synthetic decision",
            decided_at=run.created_at,
        )
        reviews.append(review)
    assert len(manager.safety_reviews._reviews) == 3
    assert len(manager.list_safety_reviews(reviews[0].run_id)) == 1
