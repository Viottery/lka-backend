"""Strict trace provenance gates, separate from answer/arithmetic oracle checks."""

from __future__ import annotations

import json
from pathlib import Path


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def tool_evidence(snapshot, runs):
    """Use durable tool lifecycle events, including turns published silently.

    Starts carry inputs; completions carry raw results. A run executes one tool
    at a time, so a completion must match that run's immediately pending start.
    Message payloads support older exported snapshots only; they cannot fill
    gaps in a snapshot that supplies the durable journal.
    """
    if "agent_run_events" not in snapshot:
        events = []
        for message in snapshot.get("agent_session_messages", []):
            payload = decoded(message.get("payload", {}))
            for event in payload.get("tool_events", []):
                if event.get("result", {}).get("status") == "completed":
                    events.append({**event, "session_id": message["session_id"],
                                   "run_id": payload.get("run_id"),
                                   "at": message.get("created_at", "")})
        return events

    invocations = {
        (row["run_id"], row["invocation_id"]): row
        for row in snapshot.get("agent_tool_invocations", [])
    }
    pending, events = {}, []
    rows = sorted(snapshot["agent_run_events"], key=lambda row: (row["run_id"], row["sequence"]))
    for row in rows:
        run_id = row["run_id"]
        run = runs.get(run_id, {})
        if not run.get("session_id"):
            continue
        event = decoded(row["event_payload"])
        if event.get("run_id", run_id) != run_id:
            continue
        payload = event.get("payload", {})
        metadata = payload.get("metadata", {})
        name = payload.get("tool_name")
        if event.get("type") == "tool_started":
            pending[run_id] = (name, metadata.get("input"))
        elif event.get("type") == "tool_completed":
            started = pending.pop(run_id, None)
            result = metadata.get("result", {})
            if (not started or started[0] != name or not isinstance(started[1], dict)
                    or result.get("tool_name", name) != name or result.get("status") != "completed"):
                continue
            invocation = invocations.get((run_id, result.get("invocation_id")))
            if "agent_tool_invocations" in snapshot and (
                invocation is None or invocation.get("tool_name") != name
                or invocation.get("status") != "completed"
                or decoded(invocation.get("result_payload")) != result
            ):
                continue
            events.append({"tool_name": name, "input": started[1], "result": result,
                           "session_id": run["session_id"], "run_id": run_id,
                           "at": row.get("created_at", event.get("created_at", ""))})
    return events


def assembled_task_answers(events, *, reader, task_session, task_run, runs, before):
    """Yield complete answer reads; continuation pages never cross reader runs."""
    for reader_run in {event["run_id"] for event in events if event["session_id"] == reader}:
        if runs.get(reader_run, {}).get("session_id") != reader:
            continue
        pieces, expected_offset, total = [], None, None
        for event in events:
            inputs = event.get("input", {})
            if (event["tool_name"] != "management.read_task_result"
                    or event["session_id"] != reader or event["run_id"] != reader_run
                    or inputs.get("run_id") != task_run or inputs.get("section", "answer") != "answer"):
                continue
            output = event.get("result", {}).get("output", {})
            observed_run = output.get("run", {})
            offset = inputs.get("start_char", 0)
            content, next_offset = output.get("content"), output.get("next_char")
            size = output.get("total_chars")
            if (not event.get("at") or not before or event["at"] > before
                    or event.get("result", {}).get("status") != "completed"
                    or observed_run.get("run_id") != task_run
                    or observed_run.get("session_id") != task_session
                    or observed_run.get("status") != "completed"
                    or output.get("section") != "answer" or output.get("start_char") != offset
                    or type(offset) is not int or offset < 0 or not isinstance(content, str)
                    or type(size) is not int or size < 0):
                pieces, expected_offset, total = [], None, None
                continue
            if offset == 0:
                pieces, expected_offset, total = [], 0, size
            if offset != expected_offset or size != total:
                pieces, expected_offset, total = [], None, None
                continue
            pieces.append(content)
            end = offset + len(content)
            if next_offset is None:
                if end == total:
                    yield "".join(pieces)
                pieces, expected_offset, total = [], None, None
            elif type(next_offset) is int and next_offset == end and offset < end < total:
                expected_offset = next_offset
            else:
                pieces, expected_offset, total = [], None, None


def trace_checks(snapshot, *, parent, definition, initial_run_id):
    delegates = snapshot.get("manager_delegates", [])
    reports = snapshot.get("manager_delegate_reports", [])
    links = snapshot.get("managed_session_links", [])
    runs = {
        r["run_id"]: decoded(r.get("record_payload", r)) for r in snapshot.get("agent_runs", [])
    }
    messages = snapshot.get("agent_session_messages", [])
    events = tool_evidence(snapshot, runs)
    owned = {
        d["session_id"]: [l for l in links if l["manager_session_id"] == d["session_id"]]
        for d in delegates
    }

    def before(event, report):
        return bool(
            event["at"] and report.get("created_at") and event["at"] <= report["created_at"]
        )

    def readback_texts(d, link, report):
        texts = [
            " ".join(m.get("content", "") for m in e["result"]["output"].get("messages", []))
            for e in events if (
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
            )
        ]
        task_run = runs.get(link["run_id"], {})
        answer = (task_run.get("result_snapshot") or {}).get("answer")
        if not isinstance(answer, str):
            answers = [m.get("content", "") for m in messages
                       if m["session_id"] == link["task_session_id"]
                       and m.get("role") in {"agent", "assistant"}
                       and decoded(m.get("payload", {})).get("run_id") == link["run_id"]]
            answer = answers[-1] if answers else None
        if answer:
            texts.extend(text for text in assembled_task_answers(
                events, reader=d["session_id"], task_session=link["task_session_id"],
                task_run=link["run_id"], runs=runs, before=report.get("created_at", ""),
            ) if text == answer)
        return texts

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
                if readback_texts(d, link, r)
                and any(
                    ref.get("session_id") == link["task_session_id"]
                    and ref.get("run_id") == link["run_id"]
                    for ref in decoded(r.get("evidence", []))
                )
            ]
            chains.append(
                run.get("status") == "completed"
                and run.get("session_id", link["task_session_id"]) == link["task_session_id"]
                and d.get("parent_session_id", parent) == parent
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
                        handoff_facts.append(
                            any(
                                all(fact in text for fact in facts)
                                for report in valid_reports
                                for text in readback_texts(d, link, report)
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
            and candidate.get("delegate_id") == report.get("delegate_id")
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
            for read_run_id in {
                event["run_id"] for event in events
                if event["session_id"] == parent
                and ("agent_run_events" not in snapshot
                     or runs.get(event["run_id"], {}).get("session_id") == parent)
            }
            for observed in assembled_delegate_reads(
                events,
                parent=parent,
                delegate_id=report["delegate_id"],
                run_id=read_run_id,
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
    """Yield complete JSON read chains observed in one owned parent run.

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
