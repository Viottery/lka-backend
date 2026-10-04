import asyncio
import subprocess
from unittest.mock import patch

from app.core.agent_turn import AgentTurnResult
from evals.lka_evals.live_budget import LiveBudget
from scripts.eval_realworld import CASES, _sender_coverage, run_case


def test_fixture_runtime_isolated_and_artifact_retained_without_api(tmp_path):
    async def fake_turn(self, *, session_id, user_input, existing_run_id=None):
        return AgentTurnResult(run_id="synthetic", session_id=session_id,
                               trace_id="synthetic", answer="amber citrus-642")

    budget = LiveBudget(tmp_path / "budget.sqlite3")
    with patch("scripts.eval_realworld.LocalKnowledgeAgentRuntime.run_agent_turn_async", fake_turn):
        report = asyncio.run(run_case("known_fact", output=tmp_path, budget=budget))
    assert report["mechanical_pass"] is True
    assert report["checks"]["files_unchanged"]
    assert report["budget_after"]["groups"] == {}
    from pathlib import Path
    root = Path(report["private_artifacts"])
    assert (root / "report.json").is_file()
    assert (root / "workspace/README.md").read_text().endswith("citrus-642\n")


def test_goals_do_not_prescribe_registered_tools():
    for case in CASES.values():
        assert not any(tool in case["goal"] for tool in
                       ("filesystem.read_file", "filesystem.edit_file", "bash.run", "mail.search"))


def test_multisource_goals_require_delivered_pages_not_a_specific_search_action():
    for name in ("heldout_web_multisource", "heldout_web_sqlite"):
        assert not CASES[name]["search_required"]
        assert CASES[name]["minimum_page_sources"] == 2
        assert CASES[name]["source_hosts"]
    assert CASES["web_search_release"]["search_required"]


def test_page_coverage_rejects_no_match_search_failure_and_fake_official_hosts():
    from types import SimpleNamespace

    from scripts.eval_realworld import _page_evidence_urls

    def event(tool, url, *, status="completed", **output):
        return SimpleNamespace(tool_name=tool, result={"status": status, "output": {"url": url, **output}})

    events = [
        event("web.open", "https://sqlite.org/wal.html", text="One writer."),
        event("web.open", "https://sqlite.org/wal.html#checkpoint", text="Checkpoint."),
        event("web.open", "https://sqlite.org/wal.html?offset=20000", text="Last page."),
        event("web.find", "https://sqlite.org/isolation.html", matches=[]),
        event("web.search", "https://sqlite.org/isolation.html", text="Search snippet."),
        event("web.open", "https://sqlite.org/isolation.html", status="failed", text="No evidence."),
        event("web.find", "https://sqlite.org.evil.test/isolation.html", matches=[{"snippet": "Snapshot."}]),
        event("web.open", "https://sqlite.org@evil.test/wal.html", text="Fake host."),
    ]
    hosts = CASES["heldout_web_sqlite"]["source_hosts"]
    assert _page_evidence_urls(events, source_hosts=hosts) == {"https://sqlite.org/wal.html"}
    events.append(event("web.find", "https://sqlite.org/isolation.html", matches=[{"snippet": "Snapshot."}]))
    assert len(_page_evidence_urls(events, source_hosts=hosts)) == 2


def test_copy_verification_catches_mutation_missing_copy_and_extra_files():
    from scripts.eval_realworld import _copy_checks

    before = {"inbox/中文 file.csv": "receipt-hash", "inbox/notes.txt": "note-hash"}
    expected = {"整理/2026-09/中文 file.csv": "inbox/中文 file.csv"}
    after = dict(before, **{"整理/2026-09/中文 file.csv": "receipt-hash"})
    assert all(_copy_checks(before, after, expected).values())
    assert not _copy_checks(before, before, expected)["copies_match_original_bytes"]
    mutated = dict(after, **{"inbox/notes.txt": "changed"})
    assert not _copy_checks(before, mutated, expected)["originals_preserved"]
    extra = dict(after, **{"secret.txt": "unexpected"})
    assert not _copy_checks(before, extra, expected)["no_unexpected_files"]
    wrong_copy = dict(after, **{"整理/2026-09/中文 file.csv": "normalized-bytes"})
    assert not _copy_checks(before, wrong_copy, expected)["copies_match_original_bytes"]


def test_total_only_or_partial_mail_answer_is_not_full_completion():
    expected = {"Alice <alice@example.test>": 7, "bob@example.test": 3}
    assert not _sender_coverage("There are 10 messages.", expected)["all_sender_counts_present"]
    partial = _sender_coverage("Total 10.\n| alice@example.test | 7 | updates |", expected)
    assert partial["matched_groups"] == 1
    assert partial["covered_messages"] == 7
    assert not partial["all_sender_counts_present"]
    complete = _sender_coverage("| Alice <alice@example.test> | 7 | updates |\n"
                                "| bob@example.test | 3 | tickets |", expected)
    assert complete["all_sender_counts_present"]
    wrong = _sender_coverage("| alice@example.test | 17 | updates |", expected)
    assert wrong["matched_groups"] == 0


def test_fixture_git_isolation_and_source_only_hashes(tmp_path):
    from scripts.eval_realworld import _hashes, _initialize_fixture_repo

    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "--quiet", str(outer)], check=True)
    workspace = outer / "nested" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("fixture-only\n")
    _initialize_fixture_repo(workspace)
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=workspace,
                          check=True, capture_output=True, text=True).stdout.strip()
    assert root == str(workspace.resolve())
    assert not any(path.startswith(".git/") for path in _hashes(workspace))
    assert _hashes(workspace)["README.md"]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=workspace,
                            check=True, capture_output=True, text=True).stdout
    assert status == ""


def test_child_metrics_include_usage_and_report_unknown_separately():
    from scripts.eval_realworld import _child_metrics

    metrics = _child_metrics({"child-a": [
        {"type": "llm_started", "payload": {}},
        {"type": "llm_completed", "payload": {"audit_record": {
            "input_token_count": 100, "output_token_count": 20, "duration_ms": 500}}},
    ], "child-b": [{"type": "llm_completed", "payload": {}}]})
    assert metrics == {"llm_calls": 2, "llm_total_duration_ms": 500,
                       "input_tokens": 100, "output_tokens": 20, "calls_missing_usage": 1}


def test_timed_out_evaluation_cancels_run_and_keeps_trace(tmp_path):
    async def stalled_turn(self, *, session_id, user_input, existing_run_id):
        await asyncio.sleep(1)

    budget = LiveBudget(tmp_path / "budget.sqlite3")
    with patch("scripts.eval_realworld.LocalKnowledgeAgentRuntime.run_agent_turn_async", stalled_turn):
        report = asyncio.run(run_case("known_fact", output=tmp_path, budget=budget, timeout=.01))
    assert report["error"]["type"] == "TimeoutError"
    assert report["run_snapshot"]["status"] == "cancelled"
    assert any(event["type"] == "run_cancelled" for event in report["run_events"])
    assert report["child_runs"] == []
    assert not report["mechanical_pass"]


def test_workflow_expert_usage_is_included_in_child_metrics():
    from scripts.eval_realworld import _child_metrics

    metrics = _child_metrics({"expert": [{"type": "expert.mail.llm_completed", "payload": {
        "duration_ms": 800, "input_token_count": 783, "output_token_count": 98,
    }}]})
    assert metrics == {"llm_calls": 1, "llm_total_duration_ms": 800,
                       "input_tokens": 783, "output_tokens": 98, "calls_missing_usage": 0}


def test_stream_metric_uses_final_answer_delta_not_reasoning(tmp_path):
    async def fake_turn(self, *, session_id, user_input, existing_run_id, llm_response_mode):
        from app.core.llm import LLMResponseMode

        assert llm_response_mode == LLMResponseMode.STREAM
        self.agent_run_manager.append_event(existing_run_id, "llm_delta", "reasoning",
            stage="answer", payload={"delta": "reasoning", "content_role": "reasoning"})
        self.agent_run_manager.append_event(existing_run_id, "llm_delta", "answer",
            stage="answer", payload={"delta": "amber", "content_role": "final_answer"})
        return AgentTurnResult(run_id=existing_run_id, session_id=session_id,
                               trace_id="synthetic", answer="amber citrus-642")

    budget = LiveBudget(tmp_path / "budget.sqlite3")
    with patch("scripts.eval_realworld.LocalKnowledgeAgentRuntime.run_agent_turn_async", fake_turn):
        report = asyncio.run(run_case("known_fact", output=tmp_path, budget=budget, response_mode="stream"))
    assert report["metrics"]["response_mode"] == "stream"
    assert report["metrics"]["first_final_token_seconds"] is not None


def test_followup_reuses_same_session_and_keeps_its_timeout_trace(tmp_path):
    sessions = []

    async def fake_turn(self, *, session_id, user_input, existing_run_id):
        sessions.append(session_id)
        if len(sessions) == 2:
            await asyncio.sleep(1)
        return AgentTurnResult(run_id=existing_run_id, session_id=session_id,
                               trace_id="synthetic", answer="jade willow-739")

    budget = LiveBudget(tmp_path / "budget.sqlite3")
    with patch("scripts.eval_realworld.LocalKnowledgeAgentRuntime.run_agent_turn_async", fake_turn):
        report = asyncio.run(run_case("heldout_context_reuse", output=tmp_path, budget=budget, timeout=.01))
    assert sessions == [report["session_id"]] * 2
    assert report["followup"]["run_id"] != report["evaluation_run_id"]
    assert report["followup"]["error"]["type"] == "TimeoutError"
    assert any(event["type"] == "run_cancelled" for event in report["followup"]["run_events"])
    assert not report["mechanical_pass"]


def test_timeout_cancels_children_and_preserves_their_events(tmp_path):
    async def stalled_turn(self, *, session_id, user_input, existing_run_id):
        self.agent_run_manager.create_child_run(
            parent_run_id=existing_run_id, plan_id="isolated", step_id="waiting",
            attempt=1, user_input="isolated pending work",
        )
        await asyncio.sleep(1)

    budget = LiveBudget(tmp_path / "budget.sqlite3")
    with patch("scripts.eval_realworld.LocalKnowledgeAgentRuntime.run_agent_turn_async", stalled_turn):
        report = asyncio.run(run_case("known_fact", output=tmp_path, budget=budget, timeout=.01))
    assert report["run_snapshot"]["status"] == "cancelled"
    assert len(report["child_runs"]) == 1
    assert report["child_runs"][0]["status"] == "cancelled"
    child_id = report["child_runs"][0]["run_id"]
    assert any(event["type"] == "run_cancelled" for event in report["child_events"][child_id])
