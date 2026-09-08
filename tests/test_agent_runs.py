from app.api.schemas import AgentRunResponse
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_tool_graph import AgentToolLifecycleGraph
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewRequest
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
