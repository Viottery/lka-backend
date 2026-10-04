import asyncio
import subprocess
from unittest.mock import patch

from app.core.agent_turn import AgentTurnResult
from evals.lka_evals.live_budget import LiveBudget
from scripts.eval_realworld import CASES, _sender_coverage, run_case


def test_fixture_runtime_isolated_and_artifact_retained_without_api(tmp_path):
    async def fake_turn(self, *, session_id, user_input):
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
