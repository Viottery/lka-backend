"""Public Agent diagnostics use the whole corpus and never pass the oracle."""

import asyncio
import hashlib
import json

import pytest

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded
from evals.lka_evals.public_datasets import PublicDataset, PublicDocument, PublicQuery
from scripts import eval_realworld
from scripts.eval_runtime_public_rag_quality import (
    analyze_report,
    delivered_evidence,
    make_case,
    run_public_case,
)


@pytest.fixture
def dataset():
    return PublicDataset("hotpotqa_200", [
        PublicDocument("doc-A", "Title A", "A routes to B."),
        PublicDocument("doc-B", "Title B", "B's colour is blue."),
        PublicDocument("distractor", "Not relevant", "Unrelated content."),
    ], [PublicQuery("question-1", "What is A's associated colour?", "GOLD-ANSWER-DO-NOT-INJECT",
                    ["doc-A", "doc-B"], ["SUPPORT-DO-NOT-INJECT"], hop_count=2)])


def event(tool, refs, status="completed", **extra):
    field = "chunks" if tool == "knowledge.load_chunks" else "results"
    return {"tool_name": tool, "result": {"status": status, "output": {
        field: [{"source_ref": ref} for ref in refs], **extra}}}


def test_goal_does_not_encode_gold_hops_or_a_tool_recipe(dataset):
    case, query = make_case(dataset, "question-1")
    assert query is dataset.queries[0]
    assert query.question in case["goal"]
    for oracle in (query.answer, *query.supporting_document_ids, *query.supporting_titles):
        assert oracle not in case["goal"]
    assert len(case["setup"]["knowledge"][0]["documents"]) == len(dataset.documents) == 3
    assert query.answer not in json.dumps(case)
    assert "knowledge.search" not in case["goal"] and "rewrite" not in case["goal"]


def test_unknown_query_and_empty_corpus_fail_before_dispatch(dataset):
    with pytest.raises(ValueError):
        make_case(dataset, "unknown")
    with pytest.raises(ValueError):
        make_case(PublicDataset(dataset.dataset, [], dataset.queries), "question-1")


def test_search_delivery_is_not_loaded_evidence_or_semantic_success(dataset):
    a = "public_dataset:/public/hotpotqa_200/doc-A#chunk=0"
    b = "public_dataset:/public/hotpotqa_200/doc-B#chunk=1"
    events = [event("knowledge.search", [a, b], rewrite_trace={"applied": True}),
              event("knowledge.load_chunks", [a]),
              event("knowledge.load_chunks", [b], status="rejected"),
              event("other.tool", [b])]
    report = {"result": {"tool_events": events, "answer": json.dumps({
        "answer": "wrong", "citations": [a], "abstain": False})}}
    analysis = analyze_report(report, dataset, dataset.queries[0])
    assert analysis["search_all_hops_delivered"] is True
    assert analysis["loaded_all_hops_delivered"] is False
    assert analysis["citations_identify_delivered_refs"] is True
    assert analysis["project_normalized_metrics_not_official"]["exact_match"] == 0
    assert analysis["semantic_review"]["status"] == "pending_root_review"
    assert analysis["evidence"]["retrievals"][0]["rewrite_trace"] == {"applied": True}


@pytest.mark.parametrize("answer", ["Not JSON", "{}", "", None, '{"answer":"blue"}'])
def test_incomplete_or_unstructured_final_does_not_get_answer_credit(dataset, answer):
    analysis = analyze_report({"result": {"answer": answer}}, dataset, dataset.queries[0])
    assert analysis["answer_format_valid"] is False
    assert "project_normalized_metrics_not_official" not in analysis


def test_empty_abstention_or_foreign_citation_does_not_pass_reference_check(dataset):
    for answer in [{"answer": "", "citations": [], "abstain": True},
                   {"answer": "blue", "citations": ["unread-source"], "abstain": False}]:
        analysis = analyze_report({"result": {"answer": json.dumps(answer)}}, dataset, dataset.queries[0])
        assert analysis["answer_format_valid"] is True
        assert analysis["citations_identify_delivered_refs"] is False
    assert delivered_evidence([], dataset.dataset)["loaded_document_ids"] == []


@pytest.mark.parametrize("code_changed", [False, True])
def test_fake_runner_is_isolated_bounded_and_preserves_raw_sha(dataset, monkeypatch, tmp_path, code_changed):
    configured = LocalAppConfig()
    configured = configured.model_copy(update={"embedding": configured.embedding.model_copy(
        update={"auto_index_on_import": True})})
    monkeypatch.setattr(Settings, "load_local_config", lambda _: configured)
    fingerprints = iter([{"code": "old"}, {"code": "new" if code_changed else "old"}])
    monkeypatch.setattr("scripts.eval_runtime_public_rag_quality.source_fingerprints", lambda: next(fingerprints))
    ledger = LiveBudget(tmp_path / "shared.sqlite3")
    before = dict(eval_realworld.CASES)

    async def runner(name, *, output, budget, timeout, full_retrieval):
        case = eval_realworld.CASES[name]
        assert len(case["setup"]["knowledge"][0]["documents"]) == 3
        assert timeout == 180 and full_retrieval is True
        assert Settings().load_local_config().embedding.auto_index_on_import is False
        for _ in range(20):
            call = budget.reserve(kind="llm", stage="fake-provider", incoming=10, outgoing=10)
            budget.finish(call, {"prompt_tokens": 3, "completion_tokens": 4}, status="completed")
        with pytest.raises(LiveBudgetExceeded):
            budget.reserve(kind="llm", stage="fake-provider", incoming=10, outgoing=10)
        with pytest.raises(LiveBudgetExceeded):
            budget.reserve(kind="search", stage="fake-search")
        root = output / (name + "_offline-fixture")
        root.mkdir()
        report = {"private_artifacts": str(root), "result": {"answer": json.dumps({
            "answer": "", "citations": [], "abstain": True})}, "mechanical_pass": True}
        (root / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    result = asyncio.run(run_public_case(dataset, "question-1", ledger=ledger, output=tmp_path,
                                        runner=runner))
    root = tmp_path / ("public_agent_hotpotqa_200_question-1_offline-fixture")
    raw = root / "report.json"
    assert result["raw_report_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert result["run_budget"]["dispatches"] == 20
    assert result["run_budget"]["call_limit"] == 20
    assert result["shared_budget_after"]["groups"]["llm"]["calls"] == 20
    assert eval_realworld.CASES == before
    assert result["semantic_review"]["status"] == "pending_root_review"
    assert result["source_files_changed_during_case"] is code_changed
    assert (result["source_fingerprints_before_preparation"]
            == result["source_fingerprints_after_turn"]) is (not code_changed)
    assert raw.stat().st_mode & 0o777 == 0o600
    assert (root / "public_analysis.json").stat().st_mode & 0o777 == 0o600


def test_shared_budget_above_authorized_limit_rejected_without_a_runner(dataset, tmp_path):
    async def no_dispatch(*args, **kwargs):
        pytest.fail("must fail before runtime or paid dispatch")

    with pytest.raises(ValueError, match="50"):
        asyncio.run(run_public_case(dataset, "question-1", ledger=LiveBudget(
            tmp_path / "invalid.sqlite3", usd_limit=51), output=tmp_path, runner=no_dispatch))
