"""Versioned public multi-hop failures; no model or live backend required."""

import hashlib
import json
import random
from pathlib import Path

import pytest

from evals.lka_evals.public_rewrite_probe import clause_rewrites

DATASET = Path("evals/datasets/public_multihop/multihop_rag_600.jsonl")
CASES = Path("evals/fixtures/knowledge/multihop_rewrite_failcases.json")


def _load_cases_and_queries():
    manifest = json.loads(CASES.read_text(encoding="utf-8"))
    raw = DATASET.read_bytes()
    records = [json.loads(line) for line in raw.splitlines()]
    queries = {record["query_id"]: record for record in records if record["kind"] == "query"}
    documents = {record["document_id"] for record in records if record["kind"] == "document"}
    return manifest, raw, queries, documents


def test_curated_failcases_reference_same_public_sample_and_gold_documents():
    manifest, raw, queries, documents = _load_cases_and_queries()
    assert hashlib.sha256(raw).hexdigest() == manifest["dataset_sha256"]
    sampled = {
        query["query_id"]
        for query in random.Random(manifest["sample_seed"]).sample(
            list(queries.values()), manifest["sample_limit"]
        )
    }
    assert manifest["top_k"] == 10
    case_ids = [case["query_id"] for case in manifest["cases"]]
    assert len(case_ids) == len(set(case_ids))
    assert set(case_ids) <= sampled
    for case in manifest["cases"]:
        assert case["category"] in {
            "rewrite_length_gate", "entity_split", "candidate_miss",
            "fusion_cutoff", "annotation_review",
        }
        assert case["status"] == (
            "needs_manual_review" if case["category"] == "annotation_review" else "observed"
        )
        assert set(case["focus_document_ids"]) <= set(
            queries[case["query_id"]]["supporting_document_ids"]
        ) <= documents


@pytest.mark.parametrize("query_id", ["multihoprag_0318", "multihoprag_0374"])
@pytest.mark.xfail(strict=True, reason="Long original questions currently fail the 300-char rewrite gate")
def test_long_multihop_question_can_still_produce_bounded_rewrites(query_id):
    _, _, queries, _ = _load_cases_and_queries()
    question = queries[query_id]["question"]
    assert len(question) > 300
    assert clause_rewrites(question)


@pytest.mark.xfail(strict=True, reason="Clause probe currently splits the shared legal-and-financial concept")
def test_clause_probe_keeps_shared_entity_context():
    _, _, queries, _ = _load_cases_and_queries()
    rewrites = clause_rewrites(queries["multihoprag_0360"]["question"])
    assert rewrites
    assert any("legal and financial actions" in rewrite for rewrite in rewrites)
