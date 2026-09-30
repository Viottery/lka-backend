"""Pure metrics for durable multi-Agent run and event JSON snapshots.

The evaluator consumes data already supplied by the caller. It performs no
runtime I/O, persistence, subject execution, or mutation of input records.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class TraceMetric:
    """A metric value, with unavailable distinguished from zero/false."""

    value: int | float | bool | None
    available: bool
    reason: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_multi_agent_trace(
    runs: Sequence[Mapping[str, Any]] | None,
    events: Sequence[Mapping[str, Any]] | None,
) -> dict[str, TraceMetric]:
    """Reconstruct lifecycle metrics from run records and a flat event stream.

    ``None`` means the data source was not supplied; an empty sequence means
    it was supplied and contained no records. Event fields follow the durable
    ``AgentRunEvent`` JSON shape, while runs follow ``AgentRunRecord`` JSON.
    """

    run_rows = [row for row in (runs or ()) if isinstance(row, Mapping)]
    child_runs = [row for row in run_rows if row.get("parent_run_id")]
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    by_run_id = {
        str(row["run_id"]): row
        for row in run_rows
        if isinstance(row.get("run_id"), str) and row.get("run_id")
    }
    parent_counts: dict[str, int] = defaultdict(int)
    for child in child_runs:
        parent_id = str(child.get("parent_run_id"))
        parent_counts[parent_id] += 1
        grouped[
            (
                parent_id,
                str(child.get("plan_id") or ""),
                str(child.get("step_id") or ""),
            )
        ].append(child)
    attempt_details = {
        "parent_child_counts": dict(sorted(parent_counts.items())),
        "by_parent_plan_step": {
            "/".join(key): {
                "count": len(items),
                "attempts": sorted(
                    int(item["attempt"])
                    for item in items
                    if isinstance(item.get("attempt"), int)
                ),
                "child_run_ids": sorted(str(item.get("run_id", "")) for item in items),
            }
            for key, items in sorted(grouped.items())
        },
    }
    metrics: dict[str, TraceMetric] = {
        "parent_child_attempts": TraceMetric(
            value=len(child_runs) if runs is not None else None,
            available=runs is not None,
            reason=None if runs is not None else "run records were not supplied",
            details=attempt_details if runs is not None else None,
        )
    }

    metrics["trace_terminal_completeness"] = _trace_terminal_completeness(
        run_rows, runs_available=runs is not None
    )

    event_rows = [row for row in (events or ()) if isinstance(row, Mapping)]
    metrics["dependency_correctness"] = _dependency_correctness(
        run_rows, child_runs, event_rows, runs_available=runs is not None, events_available=events is not None
    )
    metrics["terminal_inconsistency_count"] = _terminal_inconsistencies(
        run_rows,
        child_runs,
        event_rows,
        runs_available=runs is not None,
        events_available=events is not None,
    )
    metrics["approval_wait_count"] = _approval_wait_count(
        run_rows, event_rows, runs_available=runs is not None, events_available=events is not None
    )
    metrics["approval_decision_count"] = _approval_decision_count(
        event_rows, events_available=events is not None
    )
    metrics["time_to_first_child_event_ms"] = _time_to_first_child_event(
        by_run_id, event_rows, runs_available=runs is not None, events_available=events is not None
    )
    return metrics


def _trace_terminal_completeness(
    runs: list[Mapping[str, Any]], *, runs_available: bool
) -> TraceMetric:
    if not runs_available:
        return _unavailable("run records were not supplied")
    parents = [row for row in runs if not row.get("parent_run_id")]
    if not parents:
        return _unavailable("no parent run record was present")
    terminal = {"completed", "failed", "cancelled", "timed_out"}
    active_run_ids = sorted(
        str(row.get("run_id") or "") for row in runs if row.get("status") not in terminal
    )
    return TraceMetric(
        value=not active_run_ids,
        available=True,
        details={"active_run_ids": active_run_ids, "parent_count": len(parents)},
    )


def _dependency_correctness(
    runs: list[Mapping[str, Any]],
    child_runs: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
    *,
    runs_available: bool,
    events_available: bool,
) -> TraceMetric:
    if not runs_available:
        return _unavailable("run records were not supplied")
    if not events_available:
        return _unavailable("event records were not supplied")
    plans: dict[tuple[str, str], dict[str, set[str]]] = {}
    plan_count = 0
    for parent in runs:
        parent_id = parent.get("run_id")
        raw_plan = (parent.get("metadata") or {}).get("multi_agent_plan")
        if not isinstance(parent_id, str) or not isinstance(raw_plan, Mapping):
            continue
        steps = raw_plan.get("steps")
        plan_id = raw_plan.get("plan_id") or parent.get("plan_id")
        if not isinstance(plan_id, str) or not isinstance(steps, list):
            continue
        dependencies: dict[str, set[str]] = {}
        for step in steps:
            if not isinstance(step, Mapping) or not isinstance(step.get("step_id"), str):
                continue
            raw_dependencies = step.get("depends_on", ())
            if not isinstance(raw_dependencies, (list, tuple)) or any(
                not isinstance(value, str) for value in raw_dependencies
            ):
                continue
            dependencies[step["step_id"]] = set(raw_dependencies)
        plans[(parent_id, plan_id)] = dependencies
        plan_count += 1
    if not plan_count:
        return _unavailable("no persisted multi-agent plan was present")

    events_by_child: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        child_id = event.get("child_run_id")
        if isinstance(child_id, str) and child_id:
            events_by_child[child_id].append(event)
    violations: list[dict[str, str]] = []
    missing: list[str] = []
    starts_by_child: dict[str, datetime | None] = {}
    completed_by_step: dict[tuple[str, str, str], list[datetime | None]] = defaultdict(list)
    for child in child_runs:
        child_id = child.get("run_id")
        if not isinstance(child_id, str):
            continue
        child_events = events_by_child.get(child_id, ())
        started_events = [event for event in child_events if event.get("type") == "subtask_started"]
        starts_by_child[child_id] = _event_time(started_events[0]) if started_events else None
        if child.get("status") == "completed":
            completions = [event for event in child_events if event.get("type") == "subtask_completed"]
            if completions:
                completed_by_step[
                    (
                        str(child.get("parent_run_id") or ""),
                        str(child.get("plan_id") or ""),
                        str(child.get("step_id") or ""),
                    )
                ].append(_event_time(completions[-1]))

    for child in child_runs:
        child_id = child.get("run_id")
        if not isinstance(child_id, str):
            continue
        parent_id = str(child.get("parent_run_id") or "")
        plan_id = str(child.get("plan_id") or "")
        step_id = str(child.get("step_id") or "")
        dependencies = plans.get((parent_id, plan_id))
        if dependencies is None or step_id not in dependencies or not dependencies[step_id]:
            continue
        events_for_child = events_by_child.get(child_id, ())
        started_events = [event for event in events_for_child if event.get("type") == "subtask_started"]
        if not started_events:
            if child.get("status") in {"running", "completed", "failed", "cancelled", "timed_out"}:
                missing.append(child_id)
            continue
        started_at = starts_by_child.get(child_id)
        if started_at is None:
            missing.append(child_id)
            continue
        for dependency_id in sorted(dependencies[step_id]):
            completion_times = completed_by_step.get((parent_id, plan_id, dependency_id), ())
            if not completion_times:
                violations.append({"child_run_id": child_id, "missing_completed_dependency": dependency_id})
                continue
            parsed_times = [time for time in completion_times if time is not None]
            if not parsed_times:
                missing.append(child_id)
            elif not any(time <= started_at for time in parsed_times):
                violations.append({"child_run_id": child_id, "dependency_started_too_early": dependency_id})
    if missing:
        return TraceMetric(
            value=None,
            available=False,
            reason="dependency start/completion event timestamps are missing or invalid",
            details={"missing_child_run_ids": sorted(set(missing)), "violations": violations},
        )
    return TraceMetric(
        value=not violations,
        available=True,
        details={"checked_child_count": len(child_runs), "violations": violations},
    )


def _terminal_inconsistencies(
    runs: list[Mapping[str, Any]],
    child_runs: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
    *,
    runs_available: bool,
    events_available: bool,
) -> TraceMetric:
    if not runs_available:
        return _unavailable("run records were not supplied")
    if not events_available:
        return _unavailable("event records were not supplied")
    expected_event = {
        "completed": "subtask_completed",
        "failed": "subtask_failed",
        "timed_out": "subtask_failed",
        "cancelled": "subtask_cancelled",
    }
    by_run: dict[str, list[str]] = defaultdict(list)
    for event in events:
        run_id = event.get("run_id")
        event_type = event.get("type")
        if isinstance(run_id, str) and event_type in {
            "subtask_completed",
            "subtask_failed",
            "subtask_cancelled",
        }:
            by_run[run_id].append(str(event_type))
    inconsistent: list[dict[str, Any]] = []
    for child in child_runs:
        run_id = child.get("run_id")
        expected = expected_event.get(str(child.get("status")))
        if expected is None or not isinstance(run_id, str):
            continue
        observed = by_run.get(run_id, [])
        if observed != [expected]:
            inconsistent.append(
                {"child_run_id": run_id, "status": child.get("status"), "expected": expected, "observed": observed}
            )
    terminal_parent_statuses = {"completed", "failed", "cancelled", "timed_out"}
    active_child_statuses = {"queued", "running", "waiting_confirmation", "waiting_user"}
    runs_by_id = {
        str(run["run_id"]): run
        for run in runs
        if isinstance(run.get("run_id"), str) and run.get("run_id")
    }
    # A terminal parent may not leave a live descendant orphaned from its
    # lifecycle. The parent record's durable child_run_ids are the authority.
    for parent in runs:
        if parent.get("status") not in terminal_parent_statuses:
            continue
        parent_id = parent.get("run_id")
        child_ids = parent.get("child_run_ids", ())
        if not isinstance(parent_id, str) or not isinstance(child_ids, (list, tuple)):
            continue
        for child_id in child_ids:
            child = runs_by_id.get(str(child_id))
            if child is not None and child.get("status") in active_child_statuses:
                inconsistent.append(
                    {
                        "parent_run_id": parent_id,
                        "child_run_id": str(child_id),
                        "status": parent.get("status"),
                        "child_status": child.get("status"),
                        "reason": "terminal_parent_with_active_child",
                    }
                )
    return TraceMetric(value=len(inconsistent), available=True, details={"runs": inconsistent})


def _approval_wait_count(
    runs: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
    *,
    runs_available: bool,
    events_available: bool,
) -> TraceMetric:
    if not runs_available or not events_available:
        return _unavailable("run and event records are required")
    reviews: set[str] = set()
    for event in events:
        if event.get("type") != "safety_review_required":
            continue
        review = (event.get("payload") or {}).get("review")
        if not isinstance(review, Mapping) or review.get("mode") != "manual":
            continue
        review_id = review.get("review_id")
        if isinstance(review_id, str) and review_id:
            reviews.add(review_id)
    pending_run_ids = sorted(
        str(run.get("run_id"))
        for run in runs
        if run.get("status") == "waiting_confirmation" and run.get("run_id")
    )
    return TraceMetric(
        value=len(reviews),
        available=True,
        details={"manual_review_ids": sorted(reviews), "currently_waiting_run_ids": pending_run_ids},
    )


def _approval_decision_count(events: list[Mapping[str, Any]], *, events_available: bool) -> TraceMetric:
    if not events_available:
        return _unavailable("event records were not supplied")
    decisions: dict[str, str] = {}
    for event in events:
        if event.get("type") != "safety_review_decided":
            continue
        review = (event.get("payload") or {}).get("review")
        if not isinstance(review, Mapping):
            continue
        review_id = review.get("review_id")
        status = review.get("status")
        if isinstance(review_id, str) and isinstance(status, str):
            decisions[review_id] = status
    statuses: dict[str, int] = defaultdict(int)
    for status in decisions.values():
        statuses[status] += 1
    return TraceMetric(
        value=len(decisions),
        available=True,
        details={"by_status": dict(sorted(statuses.items())), "review_ids": sorted(decisions)},
    )


def _time_to_first_child_event(
    runs_by_id: dict[str, Mapping[str, Any]],
    events: list[Mapping[str, Any]],
    *,
    runs_available: bool,
    events_available: bool,
) -> TraceMetric:
    if not runs_available or not events_available:
        return _unavailable("run and event records are required")
    first_by_parent: dict[str, datetime] = {}
    missing_parent_time: set[str] = set()
    for event in events:
        if event.get("type") != "subtask_created":
            continue
        parent_id = event.get("run_id")
        if not isinstance(parent_id, str) or parent_id not in runs_by_id:
            continue
        created_at = _event_time(event)
        parent_time = _parse_time(runs_by_id[parent_id].get("created_at"))
        if created_at is None or parent_time is None:
            missing_parent_time.add(parent_id)
            continue
        elapsed = (created_at - parent_time).total_seconds() * 1000
        if elapsed < 0:
            missing_parent_time.add(parent_id)
            continue
        if parent_id not in first_by_parent or created_at < first_by_parent[parent_id]:
            first_by_parent[parent_id] = created_at
    if not first_by_parent:
        return _unavailable("no parent subtask_created event with valid timestamps was present")
    durations = {
        parent_id: round(
            (event_time - _parse_time(runs_by_id[parent_id].get("created_at"))).total_seconds() * 1000,
            3,
        )
        for parent_id, event_time in first_by_parent.items()
    }
    return TraceMetric(
        value=round(sum(durations.values()) / len(durations), 3),
        available=True,
        reason=(
            "some parents had missing or invalid timestamps"
            if missing_parent_time
            else None
        ),
        details={"per_parent_ms": dict(sorted(durations.items())), "parents_missing_time": sorted(missing_parent_time)},
    )


def _event_time(event: Mapping[str, Any]) -> datetime | None:
    return _parse_time(event.get("created_at"))


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _unavailable(reason: str) -> TraceMetric:
    return TraceMetric(value=None, available=False, reason=reason)
