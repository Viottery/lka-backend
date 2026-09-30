from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.api.schemas import AgentRunResponse
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_tool_graph import AgentToolLifecycleGraph
from app.core.multi_agent import Plan, PlanStatus, PlanStep, PlanStepStatus
from app.core.safety import (
    SafetyReviewDecision,
    SafetyReviewMode,
    SafetyReviewQueueConflict,
    SafetyReviewRequest,
)
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


def test_agent_run_manager_tracks_status_and_event_sequence():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(
        session_id="session_run_manager",
        user_input="test run",
        trace_id="trace_run_manager",
    )

    assert run.status == AgentRunStatus.QUEUED

    manager.mark_running(run.run_id)
    manager.append_event(run.run_id, "run_started", "started", stage="run")
    manager.append_event(run.run_id, "progress", "working", stage="route")
    manager.complete_run(
        run.run_id,
        result_snapshot={"answer": "done"},
        log_path="data/agent_logs/trace_run_manager.md",
    )

    completed = manager.get_run(run.run_id)
    assert completed is not None
    assert completed.status == AgentRunStatus.COMPLETED
    assert completed.result_snapshot == {"answer": "done"}
    assert completed.log_path == "data/agent_logs/trace_run_manager.md"

    events = manager.list_events(run.run_id)
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_id for event in events] == [
        f"{run.run_id}_event_000001",
        f"{run.run_id}_event_000002",
    ]


def test_agent_run_manager_records_failure_and_cancel_request():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(
        session_id="session_run_failure",
        user_input="test failure",
        trace_id="trace_run_failure",
    )

    manager.mark_running(run.run_id)
    manager.request_cancel(run.run_id, reason="user asked")

    assert manager.is_cancel_requested(run.run_id)
    assert manager.cancel_reason(run.run_id) == "user asked"
    assert manager.list_events(run.run_id)[0].type == "run_cancel_requested"

    manager.fail_run(run.run_id, error_type="RuntimeError", error="boom")
    failed = manager.get_run(run.run_id)
    assert failed is not None
    assert failed.status == AgentRunStatus.FAILED
    assert failed.error_type == "RuntimeError"
    assert failed.error == "boom"


def test_agent_run_manager_cancel_run_immediately_sets_terminal_status():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(
        session_id="session_cancel_terminal",
        user_input="cancel terminal run",
        trace_id="trace_cancel_terminal",
    )

    cancelled = manager.cancel_run(run.run_id, reason="user cancelled")

    assert cancelled.status == AgentRunStatus.CANCELLED
    assert manager.is_cancel_requested(run.run_id) is True
    assert [event.type for event in manager.list_events(run.run_id)] == [
        "run_cancel_requested",
        "run_cancelled",
    ]


def test_child_runs_preserve_attempts_events_and_parent_tree_after_restart(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    parent = manager.create_run(
        session_id="session_child_tree", user_input="parent", trace_id="trace_child_tree"
    )
    first = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan_1",
        step_id="research",
        attempt=1,
        user_input="research task",
    )
    manager.mark_child_running(first.run_id)
    manager.fail_child_run(first.run_id, error_type="provider", error="temporary failure")
    retry = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan_1",
        step_id="research",
        attempt=2,
        user_input="research retry",
    )
    manager.mark_child_running(retry.run_id)
    manager.complete_child_run(retry.run_id, result_snapshot={"summary": "done"})

    restored = InMemoryAgentRunManager(durable_store=store)
    children = restored.child_tree(parent.run_id)

    assert [(child.step_id, child.attempt, child.status) for child in children] == [
        ("research", 1, AgentRunStatus.FAILED),
        ("research", 2, AgentRunStatus.COMPLETED),
    ]
    events = restored.list_events(first.run_id)
    assert [event.type for event in events] == [
        "subtask_created",
        "subtask_queued",
        "subtask_started",
        "subtask_failed",
    ]
    assert all(event.parent_run_id == parent.run_id for event in events)
    assert all(event.plan_id == "plan_1" and event.step_id == "research" for event in events)


def test_coordinator_can_create_children_under_its_persisted_nested_plan(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "nested-plan-ownership.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    root = manager.create_run(session_id="nested_plan_session", user_input="root")
    manager.mark_running(root.run_id)
    coordinator = manager.create_child_run(
        parent_run_id=root.run_id,
        plan_id="outer_plan",
        step_id="coordinate",
        attempt=1,
        user_input="coordinator",
    )
    manager.mark_child_running(coordinator.run_id)

    def nested_plan(plan_id: str) -> Plan:
        return Plan(
            correlation_id=coordinator.trace_id,
            plan_id=plan_id,
            parent_run_id=coordinator.run_id,
            session_id=coordinator.session_id,
            objective="nested objective",
            steps=(PlanStep(
                correlation_id=coordinator.trace_id,
                step_id="root_coordinator",
                objective="nested objective",
                output_contract="Coordinate nested work.",
                status=PlanStepStatus.RUNNING,
            ),),
            status=PlanStatus.RUNNING,
        )

    manager.record_multi_agent_plan(
        coordinator.run_id,
        event_type="plan_created",
        payload={"plan_id": "nested_plan"},
        plan=nested_plan("nested_plan").model_dump(mode="json"),
    )
    before_ids = manager.get_run(coordinator.run_id).child_run_ids

    with pytest.raises(ValueError, match="active nested Plan"):
        manager.create_child_run(
            parent_run_id=coordinator.run_id,
            plan_id="not_the_nested_plan",
            step_id="unauthorized",
            attempt=1,
            user_input="unauthorized plan",
        )
    assert manager.get_run(coordinator.run_id).child_run_ids == before_ids
    assert manager.child_tree(coordinator.run_id) == []

    # A second manager has a stale but once-valid Plan A. The SQLite write must
    # re-check the persisted parent plan, reject the stale attempt, and avoid a
    # ghost child in the second manager's cache.
    stale_manager = InMemoryAgentRunManager(
        durable_store=SqliteAgentRunStore(store.db_path)
    )
    manager.record_multi_agent_plan(
        coordinator.run_id,
        event_type="nested_plan_revised",
        payload={"plan_id": "replacement_plan"},
        plan=nested_plan("replacement_plan").model_dump(mode="json"),
    )
    with pytest.raises(ValueError, match="active nested Plan"):
        stale_manager.create_child_run(
            parent_run_id=coordinator.run_id,
            plan_id="nested_plan",
            step_id="stale_plan_child",
            attempt=1,
            user_input="stale plan child",
        )
    assert stale_manager.child_tree(coordinator.run_id) == []

    leaf = manager.create_child_run(
        parent_run_id=coordinator.run_id,
        plan_id="replacement_plan",
        step_id="leaf",
        attempt=1,
        user_input="leaf task",
    )
    assert manager.get_run(coordinator.run_id).plan_id == "outer_plan"
    assert leaf.plan_id == "replacement_plan"
    restored = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    restored_coordinator = restored.get_run(coordinator.run_id)
    assert restored_coordinator is not None
    assert restored_coordinator.plan_id == "outer_plan"
    assert [item.plan_id for item in restored.child_tree(coordinator.run_id)] == [
        "replacement_plan"
    ]
    stale_manager.close()
    restored.close()
    manager.close()


def test_parent_cancellation_propagates_only_to_its_child_tree():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_parent_cancel", user_input="parent")
    other = manager.create_run(session_id="session_other", user_input="other")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan_cancel",
        step_id="child",
        attempt=1,
        user_input="child",
    )
    sibling = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan_cancel",
        step_id="sibling",
        attempt=1,
        user_input="sibling",
    )

    manager.cancel_run(child.run_id, reason="cancel one child")
    assert manager.get_run(sibling.run_id).status == AgentRunStatus.QUEUED
    manager.cancel_run(parent.run_id, reason="cancel parent")

    assert manager.get_run(parent.run_id).status == AgentRunStatus.CANCELLED
    assert manager.get_run(child.run_id).status == AgentRunStatus.CANCELLED
    assert manager.get_run(sibling.run_id).status == AgentRunStatus.CANCELLED
    assert manager.get_run(other.run_id).status == AgentRunStatus.QUEUED


def test_parent_cancellation_tree_and_events_commit_atomically(tmp_path, monkeypatch):
    store = SqliteAgentRunStore(tmp_path / "cancel-tree.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    parent = manager.create_run(session_id="atomic_cancel", user_input="parent")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="atomic_plan",
        step_id="child",
        attempt=1,
        user_input="child",
    )
    before_parent_events = manager.list_events(parent.run_id)
    before_child_events = manager.list_events(child.run_id)
    original = store._event_for_storage
    calls = 0

    def fail_mid_transaction(event):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated SQLite serialization failure")
        return original(event)

    monkeypatch.setattr(store, "_event_for_storage", fail_mid_transaction)
    with pytest.raises(RuntimeError, match="simulated SQLite"):
        manager.cancel_run(parent.run_id, reason="stop")
    assert manager.get_run(parent.run_id).status == AgentRunStatus.QUEUED
    assert manager.get_run(child.run_id).status == AgentRunStatus.QUEUED
    assert manager.list_events(parent.run_id) == before_parent_events
    assert manager.list_events(child.run_id) == before_child_events
    assert store.load_run(parent.run_id)["status"] == "queued"
    assert store.load_run(child.run_id)["status"] == "queued"

    monkeypatch.setattr(store, "_event_for_storage", original)
    manager.cancel_run(parent.run_id, reason="stop")
    restored = InMemoryAgentRunManager(durable_store=store)
    assert restored.get_run(parent.run_id).status == AgentRunStatus.CANCELLED
    assert restored.get_run(child.run_id).status == AgentRunStatus.CANCELLED
    assert [event.type for event in restored.list_events(child.run_id)][-3:] == [
        "run_cancel_requested", "run_cancelled", "subtask_cancelled",
    ]
    manager.close()
    restored.close()


def test_child_timeout_status_and_cancel_request_commit_atomically(tmp_path, monkeypatch):
    store = SqliteAgentRunStore(tmp_path / "timeout-child.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    parent = manager.create_run(session_id="atomic_timeout", user_input="parent")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="atomic_plan",
        step_id="child",
        attempt=1,
        user_input="child",
    )
    original = store._event_for_storage

    def fail_mid_transaction(event):
        if event["type"] == "subtask_failed":
            raise RuntimeError("simulated SQLite serialization failure")
        return original(event)

    monkeypatch.setattr(store, "_event_for_storage", fail_mid_transaction)
    with pytest.raises(RuntimeError, match="simulated SQLite"):
        manager.timeout_child_run(child.run_id, error="deadline")
    assert manager.get_run(child.run_id).status == AgentRunStatus.QUEUED
    assert manager.is_cancel_requested(child.run_id) is False
    assert store.load_run(child.run_id)["status"] == "queued"

    monkeypatch.setattr(store, "_event_for_storage", original)
    manager.timeout_child_run(child.run_id, error="deadline")
    restored = InMemoryAgentRunManager(durable_store=store)
    assert restored.get_run(child.run_id).status == AgentRunStatus.TIMED_OUT
    assert restored.is_cancel_requested(child.run_id) is True
    assert [event.type for event in restored.list_events(child.run_id)][-2:] == [
        "run_cancel_requested", "subtask_failed",
    ]
    manager.close()
    restored.close()


def test_child_terminal_races_do_not_publish_contradictory_events_or_create_attempts():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_child_race", user_input="parent")
    child = manager.create_child_run(
        parent_run_id=parent.run_id, plan_id="plan_race", step_id="work", attempt=1, user_input="work"
    )
    manager.cancel_run(child.run_id, reason="cancelled")
    manager.mark_child_running(child.run_id)
    manager.complete_child_run(child.run_id)
    assert [event.type for event in manager.list_events(child.run_id)] == [
        "subtask_created", "subtask_queued", "run_cancel_requested", "run_cancelled", "subtask_cancelled"
    ]
    with pytest.raises(ValueError, match="already exists"):
        manager.create_child_run(
            parent_run_id=parent.run_id, plan_id="plan_race", step_id="work", attempt=1, user_input="duplicate"
        )


def test_child_timeout_requests_cooperative_cancellation_and_parent_cannot_complete_early():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_child_timeout", user_input="parent")
    child = manager.create_child_run(
        parent_run_id=parent.run_id, plan_id="plan_timeout", step_id="work", attempt=1, user_input="work"
    )
    with pytest.raises(ValueError, match="active child"):
        manager.complete_run(parent.run_id)
    timed_out = manager.timeout_child_run(child.run_id, error="deadline")
    assert timed_out.status == AgentRunStatus.TIMED_OUT
    assert manager.is_cancel_requested(child.run_id)


def test_completion_claim_and_cancel_have_one_winner():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(
        session_id="session_completion_claim",
        user_input="completion claim",
        trace_id="trace_completion_claim",
    )
    manager.mark_running(run.run_id)

    assert manager.claim_completion(run.run_id) is True
    # The completion reservation wins atomically; cancellation must not publish
    # a contradictory cancellation request/event or terminal state.
    manager.request_cancel(run.run_id, reason="direct racing cancellation")
    still_running = manager.cancel_run(run.run_id, reason="racing cancellation")
    assert still_running.status == AgentRunStatus.RUNNING
    assert manager.is_cancel_requested(run.run_id) is False
    assert manager.list_events(run.run_id) == []

    completed = manager.complete_run(run.run_id, result_snapshot={"answer": "done"})
    assert completed.status == AgentRunStatus.COMPLETED


def test_failed_completion_claim_persistence_does_not_block_cancellation(tmp_path, monkeypatch):
    store = SqliteAgentRunStore(tmp_path / "completion-claim.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="claim_failure", user_input="claim")
    manager.mark_running(run.run_id)
    original_save = store.save_run

    def fail_claim(record, *, updated_at):
        if record.get("metadata", {}).get("completion_claimed") is True:
            raise RuntimeError("simulated completion claim persistence failure")
        return original_save(record, updated_at=updated_at)

    monkeypatch.setattr(store, "save_run", fail_claim)
    with pytest.raises(RuntimeError, match="completion claim persistence failure"):
        manager.claim_completion(run.run_id)
    assert manager.get_run(run.run_id).metadata.get("completion_claimed") is not True
    assert store.load_run(run.run_id)["metadata"].get("completion_claimed") is not True

    assert manager.cancel_run(run.run_id, reason="user cancelled").status == AgentRunStatus.CANCELLED
    manager.close()


def test_cancel_wins_when_completion_has_not_been_claimed():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(
        session_id="session_cancel_wins",
        user_input="cancel first",
        trace_id="trace_cancel_wins",
    )
    manager.mark_running(run.run_id)

    manager.cancel_run(run.run_id, reason="cancel first")
    assert manager.claim_completion(run.run_id) is False
    assert manager.complete_run(run.run_id).status == AgentRunStatus.CANCELLED


def test_public_agent_run_response_hides_internal_result_snapshot():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(
        session_id="session_public_run", user_input="public run", trace_id="trace_public_run"
    )
    manager.complete_run(
        run.run_id,
        result_snapshot={
            "answer": "done",
            "result_artifact_ref": {"artifact_id": "internal_artifact"},
        },
    )

    response = AgentRunResponse.from_record(manager.get_run(run.run_id))

    assert response.has_result is True
    assert "result_snapshot" not in response.model_dump()
    assert "internal_artifact" not in response.model_dump_json()


def test_manual_safety_review_pauses_run_before_review_is_visible():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(
        session_id="session_manual_review",
        user_input="manual review",
        trace_id="trace_manual_review",
    )
    manager.mark_running(run.run_id)

    review = manager.create_safety_review(
        SafetyReviewRequest(
            review_id="review_manual_visibility",
            run_id=run.run_id,
            session_id=run.session_id,
            trace_id=run.trace_id,
            invocation_id="invocation_manual_visibility",
            tool_name="filesystem.edit_file",
            mode=SafetyReviewMode.MANUAL,
            reason="Write requires confirmation.",
            created_at=run.created_at,
        )
    )

    assert manager.list_safety_reviews(run.run_id) == [review]
    paused = manager.get_run(run.run_id)
    assert paused is not None
    assert paused.status == AgentRunStatus.WAITING_CONFIRMATION
    assert paused.metadata["confirmation_id"] == review.review_id


def test_agent_run_manager_rehydrates_events_and_safety_reviews_from_sqlite(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store)
    run = first.create_run(
        session_id="session_durable_run",
        user_input="durable run",
        trace_id="trace_durable_run",
    )
    first.append_event(run.run_id, "run_started", "started", stage="run")
    review = first.create_safety_review(
        SafetyReviewRequest(
            review_id="review_durable_run",
            run_id=run.run_id,
            session_id=run.session_id,
            trace_id=run.trace_id,
            invocation_id="invocation_durable_run",
            tool_name="filesystem.edit_file",
            mode=SafetyReviewMode.MANUAL,
            reason="Write requires confirmation.",
            created_at=run.created_at,
        )
    )
    first.decide_safety_review(
        review_id=review.review_id,
        decision=SafetyReviewDecision.APPROVE,
        decided_by="test",
        reason="approved",
    )

    second = InMemoryAgentRunManager(durable_store=store)

    restored = second.get_run(run.run_id)
    assert restored is not None
    assert restored.run_id == run.run_id
    assert [event.type for event in second.list_events(run.run_id)] == [
        "run_started",
        "safety_review_decided",
    ]
    restored_review = second.get_safety_review(review.review_id)
    assert restored_review is not None
    assert restored_review.status.value == "approved"


def test_agent_run_manager_can_decide_rehydrated_review_without_preloading_run(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store)
    run = first.create_run(
        session_id="session_rehydrated_review",
        user_input="durable review",
        trace_id="trace_rehydrated_review",
    )
    review = first.create_safety_review(
        SafetyReviewRequest(
            review_id="review_rehydrated_directly",
            run_id=run.run_id,
            session_id=run.session_id,
            trace_id=run.trace_id,
            invocation_id="invocation_rehydrated_review",
            tool_name="filesystem.edit_file",
            mode=SafetyReviewMode.MANUAL,
            reason="Write requires confirmation.",
            created_at=run.created_at,
        )
    )

    second = InMemoryAgentRunManager(durable_store=store)
    assert second.get_safety_review(review.review_id) is not None
    decided = second.decide_safety_review(
        review_id=review.review_id,
        decision=SafetyReviewDecision.APPROVE,
        decided_by="test",
        reason="approved after restart",
    )

    assert decided.status.value == "approved"
    assert second.get_run(run.run_id) is not None
    assert second.list_events(run.run_id)[-1].type == "safety_review_decided"


def test_manual_review_queue_is_global_fifo_and_only_head_can_be_decided(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "approval-queue.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store)
    runs = [
        first.create_run(session_id=f"queue_session_{index}", user_input="review")
        for index in range(2)
    ]
    for run in runs:
        first.mark_running(run.run_id)
    reviews = [
        first.create_safety_review(SafetyReviewRequest(
            review_id=f"queue_review_{index}",
            run_id=run.run_id,
            session_id=run.session_id,
            trace_id=run.trace_id,
            invocation_id=f"queue_invocation_{index}",
            tool_name="filesystem.edit_file",
            mode=SafetyReviewMode.MANUAL,
            reason="Write requires confirmation.",
            created_at=f"2026-01-01T00:00:0{index}+00:00",
        ))
        for index, run in enumerate(runs)
    ]
    second = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))

    assert [item.review_id for item in second.list_pending_safety_reviews()] == [
        review.review_id for review in reviews
    ]
    with pytest.raises(ValueError, match="first pending"):
        second.decide_safety_review_with_transition(
            review_id=reviews[1].review_id,
            decision=SafetyReviewDecision.APPROVE,
            decided_by="test",
            reason="out of order",
        )

    decided, transitioned = first.decide_safety_review_with_transition(
        review_id=reviews[0].review_id,
        decision=SafetyReviewDecision.APPROVE,
        decided_by="test",
        reason="approved",
    )
    assert transitioned is True
    replay, replay_transitioned = second.decide_safety_review_with_transition(
        review_id=reviews[0].review_id,
        decision=SafetyReviewDecision.APPROVE,
        decided_by="test",
        reason="approved",
    )
    assert replay.status == decided.status
    assert replay_transitioned is False
    assert [event.type for event in first.list_events(runs[0].run_id)].count(
        "safety_review_decided"
    ) == 1  # The replaying manager does not synthesize a duplicate audit event.

    # Cancelling a pending head removes it from the actionable FIFO.
    first.cancel_run(runs[1].run_id, reason="cancelled before decision")
    assert first.list_pending_safety_reviews() == []


def test_agent_run_manager_persists_cancel_request_and_flushes_token_events(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store)
    run = first.create_run(
        session_id="session_cancel_persistence",
        user_input="cancel durable run",
        trace_id="trace_cancel_persistence",
    )
    first.append_event(run.run_id, "llm_delta", "token", stage="decision")
    first.request_cancel(run.run_id, reason="client disconnected")
    first.close()

    second = InMemoryAgentRunManager(durable_store=store)
    assert second.get_run(run.run_id) is not None
    assert second.is_cancel_requested(run.run_id) is True
    assert second.cancel_reason(run.run_id) == "client disconnected"
    assert [event.type for event in second.list_events(run.run_id)] == [
        "llm_delta",
        "run_cancel_requested",
    ]
    second.close()


def test_user_continuation_is_durable_atomic_private_and_idempotent(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "user-continuation.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store)
    run = first.create_run(
        session_id="session_user_continuation",
        user_input="resume after clarification",
        trace_id="trace_user_continuation",
    )
    first.mark_running(run.run_id)
    waiting = first.mark_waiting_user(
        run.run_id,
        question_id="question_clarify_1",
        patch_id="patch_1",
        question="Which report should be included?",
    )
    assert waiting.status == AgentRunStatus.WAITING_USER
    assert first.list_events(run.run_id)[-1].type == "multi_agent_user_question"

    restored = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    accepted, question_id, replayed = restored.continue_user_question(
        run_id=run.run_id,
        command_id="command_continue_1",
        answer="The quarterly report.",
    )
    assert accepted.status == AgentRunStatus.RUNNING
    assert question_id == "question_clarify_1"
    assert replayed is False
    assert accepted.metadata["pending_user_answer_command_id"] == "command_continue_1"
    assert "The quarterly report." not in str(accepted.model_dump(mode="json"))
    assert [event.type for event in restored.list_events(run.run_id)].count(
        "multi_agent_user_question"
    ) == 1
    answer_event = restored.list_events(run.run_id)[-1]
    assert answer_event.type == "multi_agent_user_answer_received"
    assert answer_event.payload == {
        "command_id": "command_continue_1",
        "question_id": "question_clarify_1",
    }
    assert "The quarterly report." not in str(answer_event.model_dump(mode="json"))
    assert restored.get_user_continuation(run.run_id, "command_continue_1") == "The quarterly report."

    replay_manager = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    replay, replay_question_id, replayed = replay_manager.continue_user_question(
        run_id=run.run_id,
        command_id="command_continue_1",
        answer="The quarterly report.",
    )
    assert replayed is True
    assert replay_question_id == question_id
    assert replay.status == AgentRunStatus.RUNNING
    assert [event.type for event in replay_manager.list_events(run.run_id)].count(
        "multi_agent_user_answer_received"
    ) == 1
    with pytest.raises(ValueError, match="different continuation"):
        replay_manager.continue_user_question(
            run_id=run.run_id,
            command_id="command_continue_1",
            answer="A different answer.",
        )
    with pytest.raises(ValueError, match="not waiting"):
        replay_manager.continue_user_question(
            run_id=run.run_id,
            command_id="command_continue_2",
            answer="Another answer.",
        )
    first.close()
    restored.close()
    replay_manager.close()


def test_concurrent_user_continuation_command_is_committed_once(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "user-continuation-race.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store)
    run = first.create_run(session_id="session_continue_race", user_input="clarify")
    first.mark_running(run.run_id)
    first.mark_waiting_user(run.run_id, question_id="question_race", question="Pick one.")
    second = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    barrier = Barrier(2)

    def submit(manager):
        barrier.wait()
        return manager.continue_user_question(
            run_id=run.run_id,
            command_id="command_race",
            answer="Option A.",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, (first, second)))
    assert sorted(result[2] for result in results) == [False, True]
    assert first.get_user_continuation(run.run_id, "command_race") == "Option A."
    assert [event["type"] for event in store.list_events(run.run_id)].count(
        "multi_agent_user_answer_received"
    ) == 1
    first.close()
    second.close()


def test_user_question_rejects_invalid_state_and_empty_content():
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="session_user_question", user_input="ask")
    with pytest.raises(ValueError, match="running Agent run"):
        manager.mark_waiting_user(run.run_id, question="Need clarification?")
    manager.mark_running(run.run_id)
    with pytest.raises(ValueError, match="question must not be empty"):
        manager.mark_waiting_user(run.run_id, question="  ")
    manager.mark_waiting_user(run.run_id, question="Choose a report.")
    with pytest.raises(ValueError, match="answer must not be empty"):
        manager.continue_user_question(run_id=run.run_id, command_id="cmd-empty", answer="  ")
    manager.cancel_run(run.run_id)
    assert manager.get_run(run.run_id).status == AgentRunStatus.CANCELLED


def test_waiting_user_parent_cannot_spawn_children():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session_waiting_parent", user_input="parent")
    manager.mark_running(parent.run_id)
    manager.mark_waiting_user(parent.run_id, question="Which scope should I use?")
    with pytest.raises(ValueError, match="waiting for a user response"):
        manager.create_child_run(
            parent_run_id=parent.run_id,
            plan_id="plan_waiting_parent",
            step_id="step",
            attempt=1,
            user_input="child",
        )


def test_waiting_user_run_is_not_an_actionable_safety_review(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "review-vs-user-question.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="session_review_vs_user", user_input="clarify")
    manager.mark_running(run.run_id)
    review = manager.create_safety_review(
        SafetyReviewRequest(
            review_id="review_not_user_question",
            run_id=run.run_id,
            session_id=run.session_id,
            trace_id=run.trace_id,
            invocation_id="invocation_review_not_user_question",
            tool_name="filesystem.edit_file",
            mode=SafetyReviewMode.MANUAL,
            reason="Write requires confirmation.",
            created_at=run.created_at,
        )
    )
    manager.resume_running(run.run_id)
    manager.mark_waiting_user(run.run_id, question="Which output format do you prefer?")

    assert manager.list_pending_safety_reviews() == []
    with pytest.raises(SafetyReviewQueueConflict, match="inactive Agent run"):
        manager.decide_safety_review_with_transition(
            review_id=review.review_id,
            decision=SafetyReviewDecision.APPROVE,
            decided_by="test",
            reason="should not enter approval queue",
        )


def test_parent_waiting_for_child_user_clears_stale_question_and_resumes_only_after_children_finish(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "parent-child-user-wait.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    parent = manager.create_run(session_id="session_parent_child_user_wait", user_input="parent")
    manager.mark_running(parent.run_id)
    manager.mark_waiting_user(
        parent.run_id,
        question_id="parent_question_old",
        question="Old question already answered.",
    )
    manager.continue_user_question(
        run_id=parent.run_id,
        command_id="parent_answer_old",
        answer="Answer already consumed.",
    )
    children = [
        manager.create_child_run(
            parent_run_id=parent.run_id,
            plan_id="plan_parent_child_user_wait",
            step_id=step_id,
            attempt=1,
            user_input=step_id,
        )
        for step_id in ("child_a", "child_b")
    ]
    for child in children:
        manager.mark_child_running(child.run_id)
        manager.mark_waiting_user(child.run_id, question=f"Question for {child.step_id}?")

    parked = manager.mark_waiting_for_child_user(
        parent.run_id,
        [child.run_id for child in children],
    )
    assert parked.status == AgentRunStatus.WAITING_USER
    assert parked.metadata["waiting_child_user_run_ids"] == [child.run_id for child in children]
    assert "pending_user_question" not in parked.metadata
    assert "pending_user_answer_command_id" not in parked.metadata
    assert manager.list_events(parent.run_id)[-1].payload == {
        "child_run_ids": [child.run_id for child in children]
    }
    with pytest.raises(ValueError, match="not waiting"):
        manager.continue_user_question(
            run_id=parent.run_id,
            command_id="parent_wrong_continue",
            answer="Parent has no question.",
        )
    with pytest.raises(ValueError, match="WAITING_USER"):
        manager.resume_running(parent.run_id)
    assert manager.resume_after_child_user(parent.run_id).status == AgentRunStatus.WAITING_USER

    for child in children:
        manager.continue_user_question(
            run_id=child.run_id,
            command_id=f"answer_{child.step_id}",
            answer=f"Answer for {child.step_id}.",
        )
    # Children that have merely accepted answers are still active; they must
    # finish before the parent scheduler is allowed to run again.
    assert manager.resume_after_child_user(parent.run_id).status == AgentRunStatus.WAITING_USER
    for child in children:
        manager.complete_child_run(child.run_id, result_snapshot={"status": "done"})
    resumed = manager.resume_after_child_user(parent.run_id)
    assert resumed.status == AgentRunStatus.RUNNING
    assert "waiting_child_user_run_ids" not in resumed.metadata
    assert manager.list_events(parent.run_id)[-1].type == "multi_agent_children_user_resumed"

    restored = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    assert restored.get_run(parent.run_id).status == AgentRunStatus.RUNNING
    assert [event.type for event in restored.list_events(parent.run_id)].count(
        "multi_agent_children_user_resumed"
    ) == 1
    manager.close()
    restored.close()


def test_agent_run_manager_rebuilds_stream_snapshot_from_compact_delta_storage(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store)
    run = first.create_run(
        session_id="session_compact_delta",
        user_input="compact token storage",
        trace_id="trace_compact_delta",
    )
    first.append_event(
        run.run_id,
        "llm_delta",
        "first",
        stage="answer",
        payload={
            "llm_call_id": "call_compact_delta",
            "delta": "first",
            "content_snapshot": "first",
        },
    )
    first.append_event(
        run.run_id,
        "llm_delta",
        " second",
        stage="answer",
        payload={
            "llm_call_id": "call_compact_delta",
            "delta": " second",
            "content_snapshot": "first second",
        },
    )
    first.close()

    stored = store.list_events(run.run_id)
    assert all("content_snapshot" not in event["payload"] for event in stored)
    assert all(event["payload"]["content_snapshot_omitted"] is True for event in stored)

    second = InMemoryAgentRunManager(durable_store=store)
    restored = second.get_run(run.run_id)
    assert restored is not None
    assert [event.payload["content_snapshot"] for event in second.list_events(run.run_id)] == [
        "first",
        "first second",
    ]
    second.close()


def test_sqlite_run_store_claims_tool_invocation_without_replaying_effect(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    first_claim = store.claim_tool_invocation(
        invocation_id="tool_invocation_claim",
        run_id="run_claim",
        tool_name="filesystem.edit_file",
        tool_input={"path": "a.txt", "replacement": "new"},
        claimed_at="2026-09-05T00:00:00+00:00",
    )
    assert first_claim == {"status": "claimed"}
    assert store.claim_tool_invocation(
        invocation_id="tool_invocation_claim",
        run_id="run_claim",
        tool_name="filesystem.edit_file",
        tool_input={"path": "a.txt", "replacement": "new"},
        claimed_at="2026-09-05T00:00:01+00:00",
    ) == {"status": "uncertain"}

    result = ToolResult(
        invocation_id="tool_invocation_claim",
        tool_name="filesystem.edit_file",
        status="completed",
        output={"changed": True},
    )
    store.complete_tool_invocation(
        invocation_id="tool_invocation_claim",
        result=result.model_dump(mode="json"),
        completed_at="2026-09-05T00:00:02+00:00",
    )

    recovered = store.claim_tool_invocation(
        invocation_id="tool_invocation_claim",
        run_id="run_claim",
        tool_name="filesystem.edit_file",
        tool_input={"path": "a.txt", "replacement": "new"},
        claimed_at="2026-09-05T00:00:03+00:00",
    )
    assert recovered["status"] == "completed"
    assert recovered["result"] == result.model_dump(mode="json")


def test_tool_lifecycle_graph_recovers_completed_claim_without_reexecuting(tmp_path):
    class CountingTool:
        spec = ToolSpec(
            name="test.write",
            type="test",
            description="Test write tool.",
            read_only=True,
        )

        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
            self.calls += 1
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=invocation.tool.name,
                status="completed",
                output={"calls": self.calls},
            )

    store = SqliteAgentRunStore(tmp_path / "lka.sqlite3")
    registry = ToolRegistry()
    tool = CountingTool()
    registry.register_tool(tool)
    progress: list[tuple[str, str]] = []
    graph = AgentToolLifecycleGraph(
        tool_executor=ToolExecutor(registry),
        review_tool_call=lambda *_args: (None, None),
        append_progress=lambda event_type, status, _message, _metadata: progress.append(
            (event_type, status)
        ),
        raise_if_cancel_requested=lambda: None,
        artifact_store=store,
    )
    request = {
        "invocation_id": "tool_invocation_graph_claim",
        "run_id": "run_graph_claim",
        "tool_name": "test.write",
        "tool_input": {"value": "one"},
        "context": ToolContext(session_id="session_graph_claim"),
    }

    first = graph.run(**request)
    second = graph.run(**request)

    assert first.output == {"calls": 1}
    assert second.output == {"calls": 1}
    assert tool.calls == 1
    assert progress == [
        ("tool_started", "running"),
        ("tool_completed", "completed"),
        ("tool_recovered", "completed"),
        ("tool_completed", "completed"),
    ]
