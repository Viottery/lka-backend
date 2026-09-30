"""Question-only clause-split probe for multi-hop retrieval; not an Agent rewrite score."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import tempfile
import time
from pathlib import Path

from app.domains.knowledge_query import validate_rewrite_request
from evals.lka_evals.public_datasets import PublicQuery, load_normalized_jsonl
from evals.lka_evals.public_retrieval import (
    _load_corpus,
    _new_service,
    _percentile,
    rank_metrics,
)

SEPARATORS = (
    ", while ", " while ", ", whereas ", " whereas ",
    " in contrast to ", " compared to ", " align with ",
    " in relation to ", ", but ", " but ", ", and ", " and ",
)
MAX_REWRITES = 8
MAX_TOTAL_REWRITE_CHARS = 2400


def clause_rewrites(question: str, *, max_rewrites: int = MAX_REWRITES) -> list[str]:
    """Split question text into bounded clauses without consulting gold data."""
    if not 1 <= max_rewrites <= MAX_REWRITES:
        raise ValueError(f"max_rewrites must be between 1 and {MAX_REWRITES}")
    lowered = question.casefold()
    separators: list[tuple[int, int]] = []
    for position in range(len(lowered)):
        matching = [
            len(separator)
            for separator in SEPARATORS
            if lowered.startswith(separator.casefold(), position)
        ]
        if matching:
            separators.append((position, max(matching)))

    matches: list[tuple[int, int]] = []
    cursor = 0
    for position, separator_length in separators:
        if position < cursor:
            continue
        matches.append((position, separator_length))
        cursor = position + separator_length
        if len(matches) == max_rewrites - 1:
            break

    parts: list[str] = []
    cursor = 0
    for position, separator_length in matches:
        parts.append(question[cursor:position].strip(" ,?"))
        cursor = position + separator_length
    if matches:
        tail = question[cursor:].strip(" ,?")
        if len(matches) < max_rewrites - 1:
            parts.append(tail)
        else:
            # Ignore further clauses once the rewrite budget is exhausted.
            for position, separator_length in separators:
                if position >= cursor:
                    parts.append(question[cursor:position].strip(" ,?"))
                    break
            else:
                parts.append(tail)
    if len(parts) < 2 or min(map(len, parts)) < 12:
        return []
    try:
        plan = validate_rewrite_request(
            question,
            {
                "evidence_gap": "The question contains factual clauses that may need separate evidence.",
                "expected_gain": "Retrieve evidence for each clause separately.",
                "queries": [
                    {"query": part, "purpose": f"Find evidence for clause {index}."}
                    for index, part in enumerate(parts, start=1)
                ],
            },
            max_rewrites=max_rewrites,
            max_total_chars=MAX_TOTAL_REWRITE_CHARS,
        )
    except ValueError:
        return []
    return [item.query for item in plan.queries]


def _relevant_ids(query: PublicQuery, by_title: dict[str, list[str]]) -> set[str]:
    relevant = set(query.supporting_document_ids)
    if not relevant:
        relevant.update(doc_id for title in query.supporting_titles for doc_id in by_title.get(title, []))
    return relevant


def run_probe(*, dataset_path: Path, sample_limit: int, seed: int, top_k: int,
              model_cache_dir: Path, rerank_model: str) -> dict:
    if sample_limit < 1 or top_k < 1:
        raise ValueError("sample_limit and top_k must be positive")
    dataset = load_normalized_jsonl(dataset_path)
    queries = random.Random(seed).sample(dataset.queries, min(sample_limit, len(dataset.queries)))
    by_uri = {f"/public/{dataset.dataset}/{doc.document_id}": doc.document_id for doc in dataset.documents}
    by_title: dict[str, list[str]] = {}
    for doc in dataset.documents:
        by_title.setdefault(doc.title, []).append(doc.document_id)

    with tempfile.TemporaryDirectory(prefix="lka-rewrite-probe-") as temp_dir:
        service = _new_service(Path(temp_dir), rerank_model=rerank_model, model_cache_dir=model_cache_dir)
        if service._reranker is None:
            raise RuntimeError("reranker was not configured")
        service._reranker.score("availability probe", ["availability probe"])
        _load_corpus(service, dataset)
        rows = []
        for query in queries:
            rewrites = clause_rewrites(query.question)
            started = time.perf_counter()
            if rewrites:
                result, traces = service.search_queries(
                    query=query.question, rewritten_queries=rewrites,
                    limit=top_k, mode="keyword", max_chunks_per_document=2,
                )
            else:
                result = service.search(
                    query=query.question, limit=top_k, mode="keyword", max_chunks_per_document=2,
                )
                traces = []
            latency_ms = (time.perf_counter() - started) * 1000
            if not result.rerank_applied or result.applied_mode != "keyword":
                raise RuntimeError(f"retrieval or reranking unavailable for {query.query_id}")
            ranked = []
            for item in result.results:
                document_id = by_uri.get(item.uri or "")
                if document_id is None:
                    matching = by_title.get(item.title, [])
                    document_id = matching[0] if len(matching) == 1 else None
                if document_id is not None and document_id not in ranked:
                    ranked.append(document_id)
            relevant = _relevant_ids(query, by_title)
            metrics = rank_metrics(ranked, relevant, top_k)
            rows.append({
                "query_id": query.query_id, "question": query.question,
                "rewrites": rewrites, "retrieval_traces": traces,
                "metrics": metrics, "latency_ms": latency_ms,
                "forbidden_hits": len(set(ranked) & set(query.forbidden_document_ids)),
            })

    metric_names = rows[0]["metrics"] if rows else {}
    return {
        "benchmark": "question_only_clause_split_rewrite_probe",
        "claim_scope": "document-level retrieval only; heuristic rewrites, not Agent/LLM or answer accuracy",
        "dataset": dataset.dataset,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "document_count": len(dataset.documents), "dataset_query_count": len(dataset.queries),
        "sample_query_ids_sha256": hashlib.sha256(
            "\n".join(query.query_id for query in queries).encode("utf-8")
        ).hexdigest(),
        "sample_limit": sample_limit, "query_count": len(rows), "seed": seed, "top_k": top_k,
        "rerank_model": rerank_model,
        "rewrite_policy": "question-only clause splits by fixed separators, up to 8 rewrites",
        "rewritten_query_count": sum(bool(row["rewrites"]) for row in rows),
        "rewrite_count_distribution": {
            str(count): sum(len(row["rewrites"]) == count for row in rows)
            for count in range(MAX_REWRITES + 1)
        },
        "metrics": {name: statistics.fmean(row["metrics"][name] for row in rows) for name in metric_names},
        "latency_ms": {
            "p50": _percentile([row["latency_ms"] for row in rows], .5),
            "p95": _percentile([row["latency_ms"] for row in rows], .95),
        },
        "forbidden_leak_count": sum(row["forbidden_hits"] for row in rows),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--sample-limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--rerank-model", default="BAAI/bge-reranker-base")
    parser.add_argument("--model-cache-dir", type=Path, default=Path("data/runtime/models"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_probe(
        dataset_path=args.dataset, sample_limit=args.sample_limit, seed=args.seed,
        top_k=args.top_k, rerank_model=args.rerank_model, model_cache_dir=args.model_cache_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
