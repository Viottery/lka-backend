from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager


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
