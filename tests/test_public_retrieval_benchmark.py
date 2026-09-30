import math
from pathlib import Path

import pytest

from evals.lka_evals.public_datasets import PublicDataset, PublicDocument, PublicQuery
from evals.lka_evals.public_retrieval import evaluate_dataset, rank_metrics, run_benchmark


def test_rank_metrics_expected_values_and_all_hops():
    scores = rank_metrics(["noise", "a", "b"], ["a", "b"], 3)
    assert scores["recall_at_3"] == 1.0
    assert scores["mrr_at_3"] == 0.5
    assert scores["all_hop_recall"] == 1.0
    assert scores["ndcg_at_3"] == pytest.approx((1 / math.log2(3) + 1 / 2) /
                                                (1 + 1 / math.log2(3)))


def test_rank_metrics_missing_hop_and_empty_relevance():
    assert rank_metrics(["a"], ["a", "b"], 1)["all_hop_recall"] == 0.0
    assert rank_metrics(["a"], [], 1)["recall_at_1"] == 0.0
    with pytest.raises(ValueError):
        rank_metrics([], [], 0)


def test_offline_service_evaluation_and_sample_determinism(tmp_path):
    dataset = PublicDataset("tiny", [PublicDocument("a", "Alpha", "alpha beta"),
                                      PublicDocument("b", "Beta", "alpha gamma")],
                            [PublicQuery("q1", "alpha", "", ["a"], hop_count=1),
                             PublicQuery("q2", "gamma", "", ["b"], hop_count=1)])
    first = evaluate_dataset(dataset, mode="keyword", top_k=2, sample_limit=1, seed=17,
                             data_dir=tmp_path / "one")
    second = evaluate_dataset(dataset, mode="keyword", top_k=2, sample_limit=1, seed=17,
                              data_dir=tmp_path / "two")
    assert first["query_count"] == 1
    assert first["metrics"] == second["metrics"]
    assert first["failure_rate"] == 0


def test_duplicate_titles_are_mapped_by_document_uri(tmp_path):
    dataset = PublicDataset("duplicate_titles",
                            [PublicDocument("first", "Shared title", "ordinary words"),
                             PublicDocument("second", "Shared title", "zebra unique token")],
                            [PublicQuery("q", "zebra", "", ["second"], hop_count=1)])
    result = evaluate_dataset(dataset, mode="keyword", top_k=2, sample_limit=0, seed=3,
                              data_dir=tmp_path / "duplicate")
    assert result["metrics"]["recall_at_2"] == 1.0


def test_semantic_and_hybrid_are_reported_unavailable_without_model(tmp_path):
    dataset = PublicDataset("tiny", [PublicDocument("a", "Alpha", "alpha")],
                            [PublicQuery("q", "alpha", "", ["a"])])
    for mode in ("keyword_rerank", "semantic", "hybrid", "hybrid_rerank"):
        result = evaluate_dataset(dataset, mode=mode, top_k=3, sample_limit=1,
                                  seed=1, data_dir=tmp_path / mode)
        assert result["mode_status"] == "unavailable"
        assert result["metrics"] is None
        assert result["latency_ms"]["p95"] is None


def test_keyword_rerank_uses_keyword_candidates_without_embedding(tmp_path, monkeypatch):
    from evals.lka_evals import public_retrieval

    class FakeReranker:
        max_candidates = 10

        def __init__(self, **_kwargs):
            pass

        def score(self, _query, candidates):
            return [1.0 if "Beta" in text else 0.0 for text in candidates]

    monkeypatch.setattr(public_retrieval, "FastEmbedCrossEncoderReranker", FakeReranker)
    dataset = PublicDataset(
        "tiny_rerank",
        [PublicDocument("a", "Alpha", "shared retrieval phrase"),
         PublicDocument("b", "Beta", "shared retrieval phrase")],
        [PublicQuery("q", "shared retrieval phrase", "", ["b"], hop_count=1)],
    )
    result = evaluate_dataset(dataset, mode="keyword_rerank", top_k=1, sample_limit=0,
                              seed=1, data_dir=tmp_path / "rerank",
                              rerank_model="fixture-reranker")
    assert result["mode_status"] == "available"
    assert result["metrics"]["recall_at_1"] == 1.0
    assert result["fallback_rate"] == 0.0
    assert result["setup_timing_ms"]["reranker_probe"] >= 0


def test_benchmark_manifest_contains_hash_and_rejects_invalid_options(tmp_path):
    source = Path("evals/datasets/public_multihop/hotpotqa_200.jsonl")
    report = run_benchmark(datasets=[source], modes=["keyword"], top_k=3,
                           sample_limit=1, seed=4, output=tmp_path / "report.json")
    assert len(report["manifest"][0]["sha256"]) == 64
    assert report["results"][0]["query_count"] == 1
    with pytest.raises(ValueError):
        run_benchmark(datasets=[source], modes=["nope"], top_k=3,
                      sample_limit=1, seed=4)


def test_benchmark_reports_source_language_slices_and_quality_comparison(tmp_path):
    path = tmp_path / "tiny.jsonl"
    PublicDataset("tiny", [
        PublicDocument("a", "甲", "内容 alpha", source_type="notes"),
        PublicDocument("b", "Beta", "alpha beta", source_type="mail"),
    ], [
        PublicQuery("zh", "甲在哪里？", "", ["a"], dataset="tiny"),
        PublicQuery("en", "Where is Beta?", "", ["b"], dataset="tiny"),
    ]).to_jsonl(path)
    report = run_benchmark(datasets=[path], modes=["keyword", "semantic"], top_k=2,
                           sample_limit=0, seed=5)
    keyword, semantic = report["results"]
    assert keyword["slices"]["language:zh"]["query_count"] == 1
    assert keyword["slices"]["language:en"]["query_count"] == 1
    assert keyword["slices"]["source:notes"]["query_count"] == 1
    assert keyword["slices"]["source:mail"]["query_count"] == 1
    assert keyword["metrics"]["supporting_doc_recall_at_2"] == keyword["metrics"]["recall_at_2"]
    assert semantic["mode_status"] == "unavailable"
    comparison = report["configuration"]["quality_gate_comparison"]["comparisons"][0]
    assert comparison["status"] == "unavailable"
    assert "python_version" in report["configuration"]
    assert "setup_timing_ms" in keyword
