"""Offline evaluator contracts; no private config or provider dispatch."""

import copy
import json
import sys

import pytest

from evals.lka_evals.management_cases import load_catalog, main, materialize_case, select_cases
from evals.lka_evals.management_checks import result_status, trace_checks


def test_list_and_dry_run_never_import_runtime(capsys):
    prior = sys.modules.pop("evals.lka_evals.management_runtime", None)
    try:
        assert main(["--list", "--json", "--local-config", "/missing/private.toml"]) == 0
        listing = json.loads(capsys.readouterr().out)
        assert len(listing["cases"]) == 17
        indexed = {case["case_id"]: case for case in listing["cases"]}
        for name in ("session_progress_cache", "management_notifications", "management_result_recovery"):
            assert indexed[name]["kind"] == "offline_contract"
            expected = [f"tests/test_{name}.py"]
            if name == "session_progress_cache":
                expected += ["tests/test_progress_continuation_integration.py", "tests/test_observation_read_recovery.py",
                             "tests/test_cached_result_read_routes.py", "tests/test_observation_artifact_lineage.py",
                             "tests/test_prompt_budget.py", "tests/test_answer_evidence_calibration_quality.py",
                             "tests/test_task_progress.py", "tests/test_task_progress_runtime.py",
                             "tests/test_progress_review_safety.py"]
            assert indexed[name]["nodeids"] == expected
        assert "evals.lka_evals.management_runtime" not in sys.modules
        assert main(["--case", "approval_conflict"]) == 0
        assert (
            json.loads(capsys.readouterr().out)["cases"][0]["status"]
            == "unvalidated_budget_exhausted"
        )
        assert "evals.lka_evals.management_runtime" not in sys.modules
    finally:
        if prior:
            sys.modules["evals.lka_evals.management_runtime"] = prior


@pytest.mark.parametrize("names", [["date"], ["date_handoff", "date_handoff"]])
def test_exact_unique_selection(names):
    with pytest.raises(ValueError):
        select_cases(names)


def test_gates_and_existing_output(tmp_path):
    for options in [[], ["--llm-mode", "real"]]:
        with pytest.raises(SystemExit):
            main(["--run", "--case", "date_handoff", "--output", str(tmp_path), *options])
    with pytest.raises(SystemExit):
        main(["--run", "--case", "date_handoff", "--output", str(tmp_path / "fresh")])
    assert not (tmp_path / "fresh").exists()


def test_materialization_randomizes_only_date_source():
    case = load_catalog()["cases"]["date_handoff"]
    a, b = materialize_case(case), materialize_case(case)
    assert a["expected"]["source_id"] != b["expected"]["source_id"]
    assert a["files"]["venue.json"]["source_id"] == a["expected"]["source_id"]
    assert a["expected"]["decision"] == "needs_decision"
    assert case["files"]["venue.json"]["source_id"] == "RANDOM_AT_RUN"


def fixture_chain():
    document = {"source_id": "RANDOM-123", "available_date": "2026-11-13"}
    task_message = {
        "message_id": "tm",
        "session_id": "task",
        "role": "agent",
        "content": "RANDOM-123 2026-11-13",
        "created_at": "1",
        "payload": {"run_id": "tr"},
    }

    def event(name, inputs, output):
        return {
            "tool_name": name,
            "input": inputs,
            "result": {"status": "completed", "output": output},
        }

    snapshot = {
        "manager_delegates": [{"delegate_id": "d", "session_id": "child"}],
        "managed_session_links": [
            {"manager_session_id": "child", "task_session_id": "task", "run_id": "tr"}
        ],
        "agent_runs": [
            {
                "run_id": "tr",
                "record_payload": {"run_id": "tr", "status": "completed", "parent_run_id": None},
            },
            {"run_id": "cr", "record_payload": {"session_id": "child"}},
        ],
        "manager_delegate_reports": [
            {
                "delegate_id": "d",
                "report_id": "rep",
                "kind": "needs_decision",
                "created_at": "3",
                "summary": "RANDOM-123 2026-11-13",
                "evidence": [{"session_id": "task", "run_id": "tr"}],
            }
        ],
        "agent_session_messages": [
            {
                "session_id": "task",
                "created_at": "1",
                "payload": {
                    "run_id": "tr",
                    "tool_events": [
                        event(
                            "filesystem.read_file",
                            {"path": "/synthetic/venue.json"},
                            {"content": json.dumps(document)},
                        )
                    ],
                },
            },
            task_message,
            {
                "session_id": "child",
                "created_at": "2",
                "payload": {
                    "run_id": "cr",
                    "tool_events": [
                        event(
                            "management.read_session",
                            {"session_id": "task"},
                            {
                                "latest_run": {"run_id": "tr", "status": "completed"},
                                "messages": [
                                    {
                                        "message_id": "tm",
                                        "role": "agent",
                                        "content": "RANDOM-123 2026-11-13",
                                    }
                                ],
                            },
                        )
                    ],
                },
            },
            {
                "session_id": "parent",
                "role": "agent",
                "created_at": "4",
                "content": '{"decision":"needs_decision"}',
                "payload": {
                    "run_id": "pr",
                    "tool_events": [
                        event(
                            "management.read_delegate",
                            {"delegate_id": "d"},
                            {"content": '{"report_id":"rep"}'},
                        )
                    ],
                },
            },
        ],
    }
    snapshot["agent_session_messages"][-1]["payload"]["tool_events"][0]["result"]["output"][
        "content"
    ] = json.dumps({"reports": snapshot["manager_delegate_reports"]})
    definition = {"files": {"venue.json": document}, "task_count": 1, "needs_decision": True}
    return snapshot, definition


def test_full_chain_and_forged_or_missing_provenance():
    snapshot, definition = fixture_chain()

    def check(value):
        return trace_checks(value, parent="parent", definition=definition, initial_run_id="initial")

    assert all(check(snapshot).values())
    for mutate in [
        lambda s: s["managed_session_links"][0].update(manager_session_id="other"),
        lambda s: s["agent_runs"][0]["record_payload"].update(status="failed"),
        lambda s: s["agent_session_messages"][2]["payload"].update(run_id="forged"),
        lambda s: s["agent_session_messages"][2].update(created_at="9"),
        lambda s: s["manager_delegate_reports"][0].update(evidence=[]),
        lambda s: s["agent_session_messages"][-1]["payload"].update(tool_events=[]),
        lambda s: s["agent_session_messages"][0]["payload"]["tool_events"][0]["result"][
            "output"
        ].update(content="{}"),
    ]:
        changed = copy.deepcopy(snapshot)
        mutate(changed)
        assert not all(check(changed).values())


def test_unknown_partial_budget_never_pass():
    assert result_status({}) == "unknown"
    assert result_status({"trace": None}) == "unknown"
    assert result_status({"trace": False}) == "partial_or_failed"
    assert result_status({"trace": True}, stop="call_budget_exhausted") == "budget_exhausted"


def test_ledger_reuse_charges_failures_and_refuses_raised_limit(tmp_path):
    from evals.lka_evals.live_budget import LiveBudgetExceeded
    from evals.lka_evals.management_runtime import bounded_budget

    path = tmp_path / "budget.sqlite3"
    budget = bounded_budget(path, usd_limit=0.1, call_limit=1)
    call = budget.reserve(kind="llm", stage="test", incoming=100, outgoing=100)
    budget.finish(call, None, status="failed")
    assert budget.snapshot()["groups"]["llm"]["charged_usd"] > 0
    reopened = bounded_budget(path, usd_limit=0.1, call_limit=1)
    with pytest.raises(LiveBudgetExceeded):
        reopened.reserve(kind="llm", stage="retry")
    with pytest.raises(ValueError):
        bounded_budget(path, usd_limit=0.1, call_limit=2)


def test_all_registered_clients_metered_and_temp_paths(tmp_path, monkeypatch):
    from app.core.local_config import LocalAppConfig
    from evals.lka_evals.management_runtime import (
        EvalSettings,
        Evaluation,
        RecordedClient,
        bounded_budget,
    )

    config = LocalAppConfig()
    from app.core.local_config import LLMClientConfig

    monkeypatch.setenv("EVAL_SYNTHETIC_KEY", "synthetic-value")
    config.llm.clients = [
        LLMClientConfig(name=name, api_key_env="EVAL_SYNTHETIC_KEY") for name in ("one", "two")
    ]
    config.llm.default_client = "one"
    config.management.enabled = True
    config.memory.enabled = False
    config.embedding.enabled = False
    config.reranker.enabled = False
    config.message_history.enabled = False
    settings = EvalSettings(
        _env_file=None,
        evaluation_config=config,
        LKA_DATA_DIR=tmp_path / "data",
        LKA_LOCAL_CONFIG=tmp_path / "missing.toml",
    )
    assert settings.load_local_config() == config
    evaluation = Evaluation(
        tmp_path, config, bounded_budget(tmp_path / "ledger", usd_limit=0.1, call_limit=1), ()
    )
    assert "management.decide_notification" in evaluation.report["allowed_tools"]
    assert "management.read_task_result" in evaluation.report["allowed_tools"]
    evaluation.current = {"denied_tools": []}
    runtime = evaluation.open_runtime(tmp_path / "case")
    try:
        assert all(
            isinstance(c, RecordedClient) for c in runtime.agent_llm_client.registry.list_clients()
        )
        assert str(runtime.settings.data_dir).startswith(str(tmp_path))
        with pytest.raises(PermissionError):
            runtime.tool_executor.execute(
                tool_name="filesystem.read_file", tool_input={"path": "/etc/passwd"}
            )
    finally:
        runtime.stop()


def test_async_provider_boundary_shared_between_consumers(tmp_path):
    import asyncio

    from app.core.llm.models import LLMRequest, LLMResponseMode
    from evals.lka_evals.live_budget import LiveBudgetExceeded
    from evals.lka_evals.management_runtime import RecordedClient, bounded_budget

    class FailingProvider:
        default_model = "deepseek-flash"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            assert request.max_output_tokens == 8000
            assert request.thinking_enabled is True
            assert request.response_mode is LLMResponseMode.JSON
            assert request.reasoning_effort == "high"
            raise RuntimeError("synthetic provider failure")

    provider = FailingProvider()
    budget = bounded_budget(tmp_path / "budget", usd_limit=0.1, call_limit=1)
    wrappers = [
        RecordedClient(
            provider,
            budget,
            allowed_model="deepseek-flash",
            protected_values=(),
            output=tmp_path / "calls.jsonl",
        )
        for _ in range(2)
    ]
    request = LLMRequest(
        messages=[],
        prompt_summary="synthetic review",
        model="deepseek-flash",
        max_output_tokens=8000,
        thinking_enabled=True,
        response_mode=LLMResponseMode.JSON,
        reasoning_effort="high",
    )
    with pytest.raises(RuntimeError):
        asyncio.run(wrappers[0].complete(request))
    with pytest.raises(LiveBudgetExceeded):
        asyncio.run(wrappers[1].complete(request))
    assert provider.calls == 1
    assert budget.snapshot()["groups"]["llm"]["known_usage_calls"] == 0
    assert budget.snapshot()["groups"]["llm"]["charged_usd"] > 0


def test_correct_answer_cannot_hide_missing_date_instructions():
    snapshot, definition = fixture_chain()
    definition["expected"] = {"requested_date": "2026-11-12"}
    checks = trace_checks(
        snapshot, parent="parent", definition=definition, initial_run_id="initial"
    )
    assert checks["every_owned_completed_task_read_before_report"]
    assert not checks["requested_date_preserved_through_delegation_and_task_instructions"]


def test_ledger_rejects_runtime_database_without_mutation(tmp_path):
    import sqlite3

    from evals.lka_evals.management_runtime import bounded_budget

    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_sessions(id TEXT)")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="dedicated evaluation"):
        bounded_budget(path, usd_limit=0.1, call_limit=1)
    assert path.read_bytes() == original


def paginated_report_chain():
    snapshot, definition = fixture_chain()
    parent = snapshot["agent_session_messages"][-1]
    content = json.dumps(
        {
            "source_envelope": {"text": "synthetic " * 1000},
            "reports": snapshot["manager_delegate_reports"],
        }
    )
    prototype = parent["payload"]["tool_events"][0]
    pages = []
    for offset in range(0, len(content), 4000):
        page = copy.deepcopy(prototype)
        page["input"]["start_char"] = offset
        page["result"]["output"] = {
            "delegate_id": "d",
            "content": content[offset : offset + 4000],
            "next_char": offset + 4000 if offset + 4000 < len(content) else None,
        }
        pages.append(page)
    assert len(pages) >= 3
    parent["payload"]["tool_events"] = pages
    return snapshot, definition


def report_read_gate(snapshot, definition):
    return trace_checks(snapshot, parent="parent", definition=definition, initial_run_id="initial")[
        "every_published_report_read_by_parent_before_final"
    ]


def test_parent_reads_all_continuations_before_final():
    snapshot, definition = paginated_report_chain()
    assert report_read_gate(snapshot, definition)
    # Still require the entire exact persisted report and evidence after assembly.
    snapshot["manager_delegate_reports"][0]["evidence"][0]["run_id"] = "different"
    assert not report_read_gate(snapshot, definition)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "mixed_delegate",
        "mixed_run",
        "out_of_order",
        "incorrect_next",
        "after_final",
        "failed_page",
        "wrong_parent",
    ],
)
def test_parent_report_pages_cannot_be_stitched_across_scopes_or_gaps(mutation):
    snapshot, definition = paginated_report_chain()
    final = snapshot["agent_session_messages"][-1]
    pages = final["payload"]["tool_events"]
    if mutation == "missing":
        pages.pop(1)
    elif mutation == "mixed_delegate":
        pages[1]["input"]["delegate_id"] = "other-delegate"
    elif mutation == "out_of_order":
        pages[0], pages[1] = pages[1], pages[0]
    elif mutation == "incorrect_next":
        pages[0]["result"]["output"]["next_char"] = 3999
    elif mutation == "failed_page":
        pages[1]["result"]["status"] = "failed"
    else:
        page = pages.pop(1)
        other_message = {
            "session_id": "parent",
            "created_at": "4",
            "payload": {"run_id": "pr", "tool_events": [page]},
        }
        if mutation == "mixed_run":
            other_message["payload"]["run_id"] = "other-run"
        elif mutation == "after_final":
            other_message["created_at"] = "9"
        elif mutation == "wrong_parent":
            other_message["session_id"] = "other-parent"
        snapshot["agent_session_messages"].insert(-1, other_message)
    assert not report_read_gate(snapshot, definition)


def test_parent_report_pages_can_span_ordered_messages_of_same_run():
    snapshot, definition = paginated_report_chain()
    final = snapshot["agent_session_messages"][-1]
    pages = final["payload"].pop("tool_events")
    messages = [
        {
            "session_id": "parent",
            "created_at": "3.5",
            "payload": {"run_id": "pr", "tool_events": [page]},
        }
        for page in pages
    ]
    snapshot["agent_session_messages"][-1:-1] = messages
    assert report_read_gate(snapshot, definition)
    # A terminal-looking JSON prefix cannot stand in for the remaining pages.
    pages[-1]["result"]["output"]["next_char"] = sum(
        len(p["result"]["output"]["content"]) for p in pages
    )
    assert not report_read_gate(snapshot, definition)


def durable_chain(*, paginated=False):
    snapshot, definition = paginated_report_chain() if paginated else fixture_chain()
    run_sessions = {"tr": "task", "cr": "child", "pr": "parent"}
    for row in snapshot["agent_runs"]:
        row["record_payload"]["session_id"] = run_sessions[row["run_id"]]
    snapshot["agent_runs"].append({"run_id": "pr", "record_payload": {"session_id": "parent"}})
    rows, invocations = [], []
    sequences = {}
    for message in snapshot["agent_session_messages"]:
        run_id = message["payload"]["run_id"]
        for tool in message["payload"].pop("tool_events", []):
            sequence = sequences.get(run_id, 0) + 1
            result = {**tool["result"], "invocation_id": f"{run_id}-{sequence}",
                      "tool_name": tool["tool_name"]}
            for index, (kind, metadata) in enumerate([
                ("tool_started", {"input": tool["input"]}),
                ("tool_completed", {"result": result}),
            ]):
                rows.append({"run_id": run_id, "sequence": sequence + index,
                             "created_at": message["created_at"],
                             "event_payload": json.dumps({"run_id": run_id, "type": kind,
                                 "payload": {"tool_name": tool["tool_name"], "metadata": metadata}})})
            invocations.append({"run_id": run_id, "invocation_id": result["invocation_id"],
                                "tool_name": tool["tool_name"], "status": "completed",
                                "result_payload": json.dumps(result)})
            sequences[run_id] = sequence + 1
    snapshot["agent_run_events"] = rows
    snapshot["agent_tool_invocations"] = invocations
    # Silent manager rounds have no public message carrying their tools.
    snapshot["agent_session_messages"] = [
        message for message in snapshot["agent_session_messages"]
        if message["session_id"] != "child"
    ]
    return snapshot, definition


def test_silent_delegate_readback_uses_durable_tool_evidence():
    snapshot, definition = durable_chain()
    assert all(trace_checks(snapshot, parent="parent", definition=definition,
                            initial_run_id="initial").values())


@pytest.mark.parametrize("mutation", ["missing_readback", "foreign_run", "foreign_task", "foreign_task_owner", "foreign_delegate_owner", "failed_invocation"])
def test_silent_readback_requires_matching_owned_completed_evidence(mutation):
    snapshot, definition = durable_chain()
    if mutation == "missing_readback":
        snapshot["agent_run_events"] = [row for row in snapshot["agent_run_events"] if row["run_id"] != "cr"]
    elif mutation == "foreign_run":
        snapshot["agent_runs"][1]["record_payload"]["session_id"] = "foreign"
    elif mutation == "foreign_task":
        snapshot["manager_delegate_reports"][0]["evidence"][0]["run_id"] = "foreign"
    elif mutation == "foreign_task_owner":
        snapshot["agent_runs"][0]["record_payload"]["session_id"] = "foreign"
    elif mutation == "foreign_delegate_owner":
        snapshot["manager_delegates"][0]["parent_session_id"] = "foreign"
    else:
        next(row for row in snapshot["agent_tool_invocations"] if row["run_id"] == "cr")["status"] = "executing"
    checks = trace_checks(snapshot, parent="parent", definition=definition, initial_run_id="initial")
    assert not checks["every_owned_completed_task_read_before_report"]


def test_complete_report_read_in_prior_silent_parent_run_counts():
    snapshot, definition = durable_chain(paginated=True)
    snapshot["agent_session_messages"][-1]["payload"]["run_id"] = "final-run"
    assert report_read_gate(snapshot, definition)
    # Even a complete report body in a prefix does not prove the envelope was read.
    rows = snapshot["agent_run_events"]
    parent_rows = [row for row in rows if row["run_id"] == "pr"]
    first_result = json.loads(parent_rows[1]["event_payload"])
    first_result["payload"]["metadata"]["result"]["output"].update(
        content=json.dumps({"reports": snapshot["manager_delegate_reports"]}), next_char=4000)
    parent_rows[1]["event_payload"] = json.dumps(first_result)
    invocation = next(row for row in snapshot["agent_tool_invocations"] if row["run_id"] == "pr")
    invocation["result_payload"] = json.dumps(first_result["payload"]["metadata"]["result"])
    snapshot["agent_run_events"] = [row for row in rows if row["run_id"] != "pr"] + parent_rows[:2]
    assert not report_read_gate(snapshot, definition)


@pytest.mark.parametrize("mutation", ["missing", "foreign_run", "reversed", "wrong_parent"])
def test_durable_parent_report_pagination_cannot_cross_gaps_or_ownership(mutation):
    snapshot, definition = durable_chain(paginated=True)
    rows = snapshot["agent_run_events"]
    parent_rows = [row for row in rows if row["run_id"] == "pr"]
    if mutation == "missing":
        rows.remove(parent_rows[3])
    elif mutation == "foreign_run":
        parent_rows[2]["run_id"] = parent_rows[3]["run_id"] = "foreign"
    elif mutation == "wrong_parent":
        snapshot["agent_runs"][-1]["record_payload"]["session_id"] = "foreign"
    else:
        # Sequence is authoritative even if exported rows happen to be reordered.
        parent_rows[0]["sequence"], parent_rows[2]["sequence"] = parent_rows[2]["sequence"], parent_rows[0]["sequence"]
        parent_rows[1]["sequence"], parent_rows[3]["sequence"] = parent_rows[3]["sequence"], parent_rows[1]["sequence"]
    assert not report_read_gate(snapshot, definition)


@pytest.mark.parametrize("read_tool", ["management.read_session", "management.read_task_result"])
def test_snapshot_checks_actual_silent_manager_readback(tmp_path, monkeypatch, read_tool):
    from evals.lka_evals.management_runtime import Evaluation
    from tests.test_management_api import runtime as runtime_fixture
    from tests.test_management_delegate_integration import bind_all, dispatch, queued, script
    from tests.test_management_tasks import manager_context
    from tests.test_unified_entry_quality import ScriptedClient, operation

    instances = runtime_fixture.__wrapped__(tmp_path, monkeypatch)
    runtime = next(instances)
    try:
        runtime.local_app_config.safety.tool_review_mode = "skip"
        context = manager_context.__wrapped__(runtime)
        delegate = runtime.management_delegates.delegate_work(context, command_id="d", objective="Verify task")
        runtime.agent_run_manager.complete_run(context.run_id)
        bind_all(runtime)
        script(runtime, [("management.start_task", {"command_id": "task", "purpose": "Produce evidence"})], "Waiting.")
        dispatch(runtime)
        runtime.agent_turn_loop.llm_client = ScriptedClient([
            operation("final_answer", reason="Evidence complete"), "Verified task result."])
        dispatch(runtime)
        bind_all(runtime)
        child_run = queued(runtime, delegate["session_id"])
        evaluation = Evaluation.__new__(Evaluation)
        evaluation.runtime = runtime
        link = evaluation.snapshot()["managed_session_links"][0]
        script(runtime, [
            (read_tool, {"session_id": link["task_session_id"]} if read_tool == "management.read_session"
             else {"run_id": link["run_id"]}),
            ("management.report_delegate", {"command_id": "report", "kind": "completed",
                "summary": "Verified task result.",
                "evidence": [{"session_id": link["task_session_id"], "run_id": link["run_id"]}]}),
        ], "Reviewed and reported.")
        dispatch(runtime)
        assert runtime.agent_run_manager.get_run(child_run.run_id).result_snapshot["user_visible"] is False
        snapshot = evaluation.snapshot()
        assert not any(json.loads(message["payload"]).get("run_id") == child_run.run_id
                       for message in snapshot["agent_session_messages"])
        definition = {"files": {}, "task_count": 1, "needs_decision": False}
        checks = trace_checks(snapshot, parent=context.session_id, definition=definition,
                              initial_run_id=context.run_id)
        assert checks["every_owned_completed_task_read_before_report"]
        snapshot["agent_run_events"] = [row for row in snapshot["agent_run_events"] if row["run_id"] != child_run.run_id]
        assert not trace_checks(snapshot, parent=context.session_id, definition=definition,
                                initial_run_id=context.run_id)["every_owned_completed_task_read_before_report"]
    finally:
        next(instances, None)


def task_answer_chain():
    snapshot, definition = fixture_chain()
    answer = "Full execution result. " * 400 + "RANDOM-123 2026-11-13"
    snapshot["agent_session_messages"][1]["content"] = answer
    snapshot["agent_runs"][0]["record_payload"].update(
        session_id="task", result_snapshot={"answer": answer})
    child = snapshot["agent_session_messages"][2]
    prototype = child["payload"]["tool_events"][0]
    pages = []
    for offset in range(0, len(answer), 4000):
        page = copy.deepcopy(prototype)
        page["tool_name"] = "management.read_task_result"
        page["input"] = {"run_id": "tr", "start_char": offset}
        page["result"]["output"] = {
            "run": {"run_id": "tr", "session_id": "task", "status": "completed"},
            "section": "answer", "content": answer[offset:offset + 4000],
            "start_char": offset, "total_chars": len(answer),
            "next_char": offset + 4000 if offset + 4000 < len(answer) else None,
        }
        pages.append(page)
    child["payload"]["tool_events"] = pages
    return snapshot, definition


def test_complete_task_answer_readbacks_preserve_full_chain():
    snapshot, definition = task_answer_chain()
    assert all(trace_checks(snapshot, parent="parent", definition=definition,
                            initial_run_id="initial").values())
    # Published message fallback has the same exact-answer requirement.
    snapshot["agent_runs"][0]["record_payload"].pop("result_snapshot")
    assert all(trace_checks(snapshot, parent="parent", definition=definition,
                            initial_run_id="initial").values())


@pytest.mark.parametrize("mutation", [
    "partial", "wrong_order", "foreign_input", "foreign_run", "foreign_session",
    "wrong_section", "different_answer", "mixed_reader_run", "after_report", "prefix_only",
])
def test_task_answer_readback_rejects_partial_or_foreign_evidence(mutation):
    snapshot, definition = task_answer_chain()
    child = snapshot["agent_session_messages"][2]
    pages = child["payload"]["tool_events"]
    output = pages[1]["result"]["output"]
    if mutation == "partial":
        pages.pop(1)
    elif mutation == "wrong_order":
        pages[0], pages[1] = pages[1], pages[0]
    elif mutation == "foreign_input":
        pages[1]["input"]["run_id"] = "foreign"
    elif mutation == "foreign_run":
        output["run"]["run_id"] = "foreign"
    elif mutation == "foreign_session":
        output["run"]["session_id"] = "foreign"
    elif mutation == "wrong_section":
        output["section"] = "evidence"
    elif mutation == "different_answer":
        output["content"] = "X" * len(output["content"])
    elif mutation == "mixed_reader_run":
        page = pages.pop(1)
        snapshot["agent_runs"].append({"run_id": "other", "record_payload": {"session_id": "child"}})
        snapshot["agent_session_messages"].insert(2, {"session_id": "child", "created_at": "2",
            "payload": {"run_id": "other", "tool_events": [page]}})
    elif mutation == "after_report":
        child["created_at"] = "9"
    else:
        first = pages[0]["result"]["output"]
        first.update(next_char=None, total_chars=len(first["content"]))
        pages[:] = pages[:1]
    checks = trace_checks(snapshot, parent="parent", definition=definition, initial_run_id="initial")
    assert not checks["every_owned_completed_task_read_before_report"]
    assert not checks["source_and_date_facts_survive_task_and_delegate_handoffs"]
