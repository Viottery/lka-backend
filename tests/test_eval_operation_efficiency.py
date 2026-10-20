import pytest

from app.core.llm.models import LLMRequest
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded
from scripts.eval_operation_efficiency import AttemptBudget, StageClient, paired_summary


def test_stage_override_is_control_only():
    records = []
    candidate = StageClient(object(), "control_no_thinking", records)
    for stage in ("route", "decision", "decision_repair"):
        request = LLMRequest(messages=[], prompt_summary="synthetic", metadata={"stage": stage})
        assert candidate.prepare(request).thinking_enabled is False
        assert request.thinking_enabled is None
    for stage in ("answer", "context_answer", "safety_review"):
        request = LLMRequest(messages=[], prompt_summary="synthetic", metadata={"stage": stage})
        assert candidate.prepare(request).thinking_enabled is None
    baseline = StageClient(object(), "configured", [])
    assert baseline.prepare(request).thinking_enabled is None


def test_budget_persists_and_refuses_search(tmp_path):
    ledger = LiveBudget(tmp_path / "budget.sqlite3")
    attempt = AttemptBudget(ledger, "test", token_limit=100)
    call = attempt.reserve(kind="llm", stage="synthetic", incoming=30, outgoing=50)
    attempt.finish(call, {"prompt_tokens": 3, "completion_tokens": 5}, status="completed")
    assert AttemptBudget(ledger, "test", token_limit=100).snapshot()["attempt_reserved_tokens"] == 80
    with pytest.raises(LiveBudgetExceeded):
        attempt.reserve(kind="llm", stage="synthetic", incoming=20, outgoing=1)
    with pytest.raises(LiveBudgetExceeded):
        attempt.reserve(kind="search", stage="synthetic")
    assert ledger.snapshot()["groups"]["llm"]["calls"] == 1


def test_unpaired_or_budget_failure_never_counts_as_accuracy_or_speed():
    metrics = {"llm_calls": 2, "tool_calls": 0, "input_tokens": 10, "output_tokens": 5}
    rows = [{"case_id": "pair", "arm": arm, "wall_seconds": 10, "metrics": metrics,
             "mechanical_pass": True} for arm in ("configured", "control_no_thinking")]
    rows += [{"case_id": "incomplete", "arm": "configured", "wall_seconds": .1, "metrics": None,
              "mechanical_pass": False, "error": {"type": "budget"}}]
    rows += [{"case_id": "incomplete", "arm": "control_no_thinking", "wall_seconds": 1,
              "metrics": metrics, "mechanical_pass": True}]
    summary = paired_summary(rows)
    assert summary["sample_count"] == 1
    assert summary["configured"]["wall_total"] == 10
    assert summary["control_no_thinking"]["passes"] == 1


def test_decision_only_summary_pairs_the_requested_arm():
    metrics = {"llm_calls": 2, "tool_calls": 1, "input_tokens": 10, "output_tokens": 5}
    rows = [{"case_id": "pair", "arm": arm, "wall_seconds": 10, "metrics": metrics,
             "mechanical_pass": True} for arm in ("configured", "decision_only")]
    summary = paired_summary(rows, candidate="decision_only")
    assert summary["sample_count"] == 1
    assert summary["decision_only"]["passes"] == 1
    assert paired_summary(rows)["sample_count"] == 0
