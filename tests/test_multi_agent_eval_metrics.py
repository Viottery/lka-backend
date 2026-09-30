from evals.lka_evals.multi_agent_metrics import evaluate_multi_agent_trace


def _run(run_id, *, parent=None, status="queued", created_at="2026-01-01T00:00:00Z", **extra):
    return {
        "run_id": run_id,
        "parent_run_id": parent,
        "status": status,
        "created_at": created_at,
        **extra,
    }


def _event(run_id, event_type, timestamp, *, child=None, step=None, plan="plan-1", payload=None):
    return {
        "run_id": run_id,
        "type": event_type,
        "created_at": timestamp,
        "child_run_id": child,
        "step_id": step,
        "plan_id": plan,
        "payload": payload or {},
    }


def _complete_trace():
    parent = _run(
        "parent",
        created_at="2026-01-01T00:00:00Z",
        metadata={
            "multi_agent_plan": {
                "plan_id": "plan-1",
                "steps": [
                    {"step_id": "research", "depends_on": []},
                    {"step_id": "report", "depends_on": ["research"]},
                ],
            }
        },
    )
    runs = [
        parent,
        _run("research-1", parent="parent", status="failed", plan_id="plan-1", step_id="research", attempt=1),
        _run("research-2", parent="parent", status="completed", plan_id="plan-1", step_id="research", attempt=2),
        _run("report-1", parent="parent", status="completed", plan_id="plan-1", step_id="report", attempt=1),
    ]
    events = [
        _event("parent", "subtask_created", "2026-01-01T00:00:02Z", child="research-1"),
        _event("parent", "subtask_created", "2026-01-01T00:00:03Z", child="research-2"),
        _event("parent", "subtask_created", "2026-01-01T00:00:04Z", child="report-1"),
        _event("research-1", "subtask_started", "2026-01-01T00:00:05Z", child="research-1", step="research"),
        _event("research-1", "subtask_failed", "2026-01-01T00:00:06Z", child="research-1", step="research"),
        _event("research-2", "subtask_started", "2026-01-01T00:00:10Z", child="research-2", step="research"),
        _event("research-2", "subtask_completed", "2026-01-01T00:00:15Z", child="research-2", step="research"),
        _event("report-1", "subtask_started", "2026-01-01T00:00:16Z", child="report-1", step="report"),
        _event("report-1", "subtask_completed", "2026-01-01T00:00:20Z", child="report-1", step="report"),
        _event(
            "report-1",
            "safety_review_required",
            "2026-01-01T00:00:17Z",
            payload={"review": {"review_id": "review-1", "mode": "manual"}},
        ),
        _event(
            "report-1",
            "safety_review_decided",
            "2026-01-01T00:00:18Z",
            payload={"review": {"review_id": "review-1", "status": "approved"}},
        ),
    ]
    return runs, events


def test_multi_agent_trace_metrics_reconstruct_attempts_dependencies_approvals_and_latency():
    runs, events = _complete_trace()
    metrics = evaluate_multi_agent_trace(runs, events)

    assert metrics["parent_child_attempts"].value == 3
    assert metrics["parent_child_attempts"].details["parent_child_counts"] == {"parent": 3}
    assert metrics["parent_child_attempts"].details["by_parent_plan_step"]["parent/plan-1/research"]["attempts"] == [1, 2]
    assert metrics["dependency_correctness"].value is True
    assert metrics["terminal_inconsistency_count"].value == 0
    assert metrics["approval_wait_count"].value == 1
    assert metrics["approval_decision_count"].value == 1
    assert metrics["time_to_first_child_event_ms"].value == 2000.0


def test_dependency_and_terminal_metrics_detect_bad_order_and_contradictory_lifecycle():
    runs, events = _complete_trace()
    report_start = next(event for event in events if event["type"] == "subtask_started" and event["run_id"] == "report-1")
    report_start["created_at"] = "2026-01-01T00:00:14Z"
    failed_child = next(run for run in runs if run["run_id"] == "research-1")
    failed_child["status"] = "completed"

    metrics = evaluate_multi_agent_trace(runs, events)

    assert metrics["dependency_correctness"].value is False
    assert metrics["terminal_inconsistency_count"].value == 1


def test_metrics_mark_absent_trace_data_or_required_sections_unavailable():
    unavailable = evaluate_multi_agent_trace(None, None)
    assert unavailable["parent_child_attempts"].available is False
    assert unavailable["parent_child_attempts"].value is None
    assert unavailable["approval_decision_count"].available is False
    assert unavailable["approval_decision_count"].to_dict()["reason"]

    partial = evaluate_multi_agent_trace([_run("parent")], [])
    assert partial["dependency_correctness"].available is False
    assert partial["terminal_inconsistency_count"].available is True
    assert partial["approval_wait_count"].available is True
    assert partial["time_to_first_child_event_ms"].available is False


def test_terminal_inconsistency_detects_parent_completion_with_active_child():
    parent = _run("parent", status="completed", child_run_ids=["child"])
    child = _run("child", parent="parent", status="queued", plan_id="plan-1", step_id="work", attempt=1)

    metric = evaluate_multi_agent_trace([parent, child], [])["terminal_inconsistency_count"]

    assert metric.value == 1
    assert metric.details["runs"][0]["reason"] == "terminal_parent_with_active_child"
