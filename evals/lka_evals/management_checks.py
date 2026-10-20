"""Strict trace provenance gates, separate from answer/arithmetic oracle checks."""

from __future__ import annotations

import json
from pathlib import Path


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def trace_checks(snapshot, *, parent, definition, initial_run_id):
    delegates = snapshot.get("manager_delegates", [])
    reports = snapshot.get("manager_delegate_reports", [])
    links = snapshot.get("managed_session_links", [])
    runs = {
        r["run_id"]: decoded(r.get("record_payload", r)) for r in snapshot.get("agent_runs", [])
    }
    messages = snapshot.get("agent_session_messages", [])
    events = []
    for message in messages:
        payload = decoded(message.get("payload", {}))
        for event in payload.get("tool_events", []):
            if event.get("result", {}).get("status") == "completed":
                events.append(
                    {
                        **event,
                        "session_id": message["session_id"],
                        "run_id": payload.get("run_id"),
                        "at": message.get("created_at", ""),
                    }
                )
    owned = {
        d["session_id"]: [l for l in links if l["manager_session_id"] == d["session_id"]]
        for d in delegates
    }

    def before(event, report):
        return bool(
            event["at"] and report.get("created_at") and event["at"] <= report["created_at"]
        )

    def readback(d, link, report):
        return any(
            e["tool_name"] == "management.read_session"
            and e["session_id"] == d["session_id"]
            and e.get("input", {}).get("session_id") == link["task_session_id"]
            and before(e, report)
            and runs.get(e["run_id"], {}).get("session_id") == d["session_id"]
            and e["result"].get("output", {}).get("latest_run", {}).get("run_id") == link["run_id"]
            and e["result"]["output"]["latest_run"].get("status") == "completed"
            and any(
                m.get("role") in {"agent", "assistant"}
                and any(
                    stored.get("message_id") == m.get("message_id")
                    and stored["session_id"] == link["task_session_id"]
                    and decoded(stored.get("payload", {})).get("run_id") == link["run_id"]
                    and bool(m.get("content"))
                    and m["content"] in stored.get("content", "")
                    for stored in messages
                )
                for m in e["result"]["output"].get("messages", [])
            )
            for e in events
        )

    chains = []
    coverage = set()
    handoff_facts = []
    for d in delegates:
        dreports = [r for r in reports if r["delegate_id"] == d["delegate_id"]]
        for link in owned[d["session_id"]]:
            run = runs.get(link["run_id"], {})
            valid_reports = [
                r
                for r in dreports
                if readback(d, link, r)
                and any(
                    ref.get("session_id") == link["task_session_id"]
                    and ref.get("run_id") == link["run_id"]
                    for ref in decoded(r.get("evidence", []))
                )
            ]
            chains.append(
                run.get("status") == "completed"
                and run.get("parent_run_id") is None
                and bool(valid_reports)
            )
            for e in events:
                if (
                    e["tool_name"] == "filesystem.read_file"
                    and e["session_id"] == link["task_session_id"]
                    and e["run_id"] == link["run_id"]
                ):
                    output = e["result"].get("output", {})
                    try:
                        content = decoded(output.get("content", ""))
                    except ValueError:
                        continue
                    name = Path(e.get("input", {}).get("path", "")).name
                    if name in definition["files"] and content == definition["files"][name]:
                        coverage.add(name)
                        facts = [
                            str(value)
                            for key, value in content.items()
                            if key == "source_id" or "date" in key or key == "availability"
                        ]
                        readbacks = [
                            x
                            for x in events
                            if x["tool_name"] == "management.read_session"
                            and x["session_id"] == d["session_id"]
                            and x.get("input", {}).get("session_id") == link["task_session_id"]
                        ]
                        handoff_facts.append(
                            any(
                                all(
                                    fact
                                    in " ".join(
                                        m.get("content", "")
                                        for m in x["result"].get("output", {}).get("messages", [])
                                    )
                                    for fact in facts
                                )
                                for x in readbacks
                            )
                            and any(
                                all(fact in r.get("summary", "") for fact in facts)
                                for r in valid_reports
                            )
                        )
    finals = [
        m
        for m in messages
        if m["session_id"] == parent
        and m.get("role") in {"agent", "assistant"}
        and decoded(m.get("payload", {})).get("run_id") != initial_run_id
        and '"decision"' in m.get("content", "")
    ]

    def report_seen(observed, report):
        return isinstance(observed, dict) and any(
            isinstance(candidate, dict)
            and candidate.get("report_id") == report["report_id"]
            and candidate.get("summary") == report.get("summary")
            and candidate.get("kind") == report.get("kind")
            and candidate.get("run_id") == report.get("run_id")
            and decoded(candidate.get("evidence", [])) == decoded(report.get("evidence", []))
            for candidate in observed.get("reports", [])
        )

    parent_read = all(
        any(
            report_seen(observed, report)
            for final in finals
            for observed in assembled_delegate_reads(
                events,
                parent=parent,
                delegate_id=report["delegate_id"],
                run_id=decoded(final.get("payload", {})).get("run_id"),
                before=final.get("created_at", ""),
            )
        )
        for report in reports
    )
    requested_date = definition.get("expected", {}).get("requested_date")
    date_constraints = True
    if requested_date:
        starts = [e for e in events if e["tool_name"] == "management.start_task"]
        date_constraints = (
            bool(delegates)
            and len(starts) == definition["task_count"]
            and all(requested_date in d.get("objective", "") for d in delegates)
            and all(requested_date in e.get("input", {}).get("purpose", "") for e in starts)
            and all(requested_date in r.get("summary", "") for r in reports)
        )
    return {
        "requested_date_preserved_through_delegation_and_task_instructions": date_constraints,
        "every_owned_completed_task_read_before_report": bool(chains)
        and all(chains)
        and len(chains) == definition["task_count"],
        "full_source_content_in_owned_execution_runs": coverage == set(definition["files"]),
        "source_and_date_facts_survive_task_and_delegate_handoffs": bool(handoff_facts)
        and all(handoff_facts),
        "every_published_report_read_by_parent_before_final": bool(reports)
        and bool(finals)
        and parent_read,
        "decision_reports_preserve_conflict": not definition["needs_decision"]
        or bool(reports)
        and all(r["kind"] in {"needs_decision", "blocked"} for r in reports),
    }


def result_status(checks, *, stop=None, error_category=None):
    """Unknown/partial/budget failures never enter the passing denominator."""
    if error_category == "live_budget_exceeded" or stop == "call_budget_exhausted":
        return "budget_exhausted"
    if not checks or any(value is None for value in checks.values()):
        return "unknown"
    return "passed" if all(value is True for value in checks.values()) else "partial_or_failed"


def assembled_delegate_reads(events, *, parent, delegate_id, run_id, before):
    """Yield only complete JSON read chains observed in this parent's final run.

    Events retain durable message/tool order. Offsets cannot be sorted into an
    apparent read that never happened; each continuation must follow its actual
    predecessor and consume precisely the advertised next_char.
    """
    pieces = []
    expected_offset = None
    for event in events:
        if (
            event.get("tool_name") != "management.read_delegate"
            or event.get("session_id") != parent
            or event.get("run_id") != run_id
            or event.get("input", {}).get("delegate_id") != delegate_id
            or not event.get("at")
            or not before
            or event["at"] > before
            or event.get("result", {}).get("status") != "completed"
        ):
            continue
        offset = event.get("input", {}).get("start_char", 0)
        output = event["result"].get("output", {})
        content, next_offset = output.get("content"), output.get("next_char")
        if (
            type(offset) is not int
            or offset < 0
            or not isinstance(content, str)
            or output.get("delegate_id", delegate_id) != delegate_id
        ):
            pieces, expected_offset = [], None
            continue
        if offset == 0:
            pieces, expected_offset = [], 0
        if offset != expected_offset:
            pieces, expected_offset = [], None
            continue
        pieces.append(content)
        if next_offset is None:
            try:
                yield json.loads("".join(pieces))
            except ValueError:
                pass
            pieces, expected_offset = [], None
        elif (
            type(next_offset) is int
            and next_offset > offset
            and next_offset == offset + len(content)
        ):
            expected_offset = next_offset
        else:
            pieces, expected_offset = [], None
