from __future__ import annotations

from types import SimpleNamespace

from app.core.agent_graph import AgentGraphRunner
from app.core.agent_turn import AgentTurnProgressEvent, AgentTurnWorkingSet


def test_langgraph_fork_shape_gets_one_repair_before_persistent_rejection() -> None:
    durable_events = []
    handled = []
    loop = SimpleNamespace(
        run_manager=SimpleNamespace(
            append_event=lambda *args, **kwargs: durable_events.append((args, kwargs))
        ),
        _raise_if_cancel_requested=lambda: None,
        _upgrade_fast_path_for_multi_agent=lambda _run_id: None,
        _append_progress=lambda progress, **values: progress.append(
            AgentTurnProgressEvent(
                event_index=len(progress) + 1, created_at="2026-10-01T00:00:00Z", **values
            )
        ),
        _handle_fork_subtasks_decision=lambda **kwargs: (
            handled.append(kwargs)
            or {"status": "rejected", "operation_id": "fork_1", "message": "Still invalid."}
        ),
        _planner_progress_summary=lambda outcome: {"status": outcome["status"]},
    )
    runner = AgentGraphRunner.__new__(AgentGraphRunner)
    runner.turn_loop = loop
    ws = AgentTurnWorkingSet(
        run_id="parent",
        session_id="session",
        trace_id="trace",
        user_input="Compare options.",
        pending_decision={
            "action": "fork_subtasks_invalid",
            "operation": {"type": "fork_subtasks", "operation_id": "fork_1"},
            "reason": "subtasks.0.output_contract: expected string",
        },
    )
    data = {"observations": [], "progress_events": []}
    runner._data = lambda _state: (ws, data)
    runner._save = lambda _state, _ws, saved_data, phase, status="running": {
        "phase": phase,
        "status": status,
        "data": saved_data,
    }

    first = runner._validate({"run_id": "parent"})
    assert first["phase"] == "fork_subtasks_rejected"
    assert data["fork_format_repair_used"] is True
    assert data["observations"][-1]["action"] == "fork_subtasks_schema_feedback"
    assert data["observations"][-1]["status"] == "retry_once"
    assert len(durable_events) == 1
    assert durable_events[0][0][1] == "fork_subtasks_schema_retry_requested"
    assert handled == []

    second = runner._validate({"run_id": "parent"})
    assert second["phase"] == "fork_subtasks_rejected"
    assert len(handled) == 1
    assert len(durable_events) == 1


def test_langgraph_fork_repair_feedback_is_consumed_after_next_decision() -> None:
    seen_observations = []
    loop = SimpleNamespace(
        max_decision_steps=4,
        _raise_if_cancel_requested=lambda: None,
        _decide_next_action=lambda **kwargs: (
            seen_observations.extend(kwargs["observations"])
            or {"action": "final_answer", "reason": "Evidence is sufficient."}
        ),
        _record_decision=lambda *args, **kwargs: None,
    )
    runner = AgentGraphRunner.__new__(AgentGraphRunner)
    runner.turn_loop = loop
    ws = AgentTurnWorkingSet(
        run_id="parent",
        session_id="session",
        trace_id="trace",
        user_input="Compare options.",
    )
    feedback = {"action": "fork_subtasks_schema_feedback", "status": "retry_once"}
    data = {
        "observations": [feedback],
        "llm_events": [],
        "decision_events": [],
        "context_window": {},
        "package_catalog": [],
        "expanded_tools": [],
    }
    runner._data = lambda _state: (ws, data)
    runner._save = lambda _state, _ws, saved_data, phase, status="running": {
        "phase": phase,
        "status": status,
        "data": saved_data,
    }

    decision = runner._decide({"run_id": "parent"})

    assert decision["phase"] == "decision_made"
    assert seen_observations[0] is feedback
    assert feedback["status"] == "consumed"
    assert ws.step_index == 1
