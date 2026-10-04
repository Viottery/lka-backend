"""Offline retrieval benchmark for the checked-in normalized public datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import random
import statistics
import tempfile
import time
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.core.local_config import RerankerConfig
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeService, KnowledgeSourceInput
from app.integrations.local_reranker import FastEmbedCrossEncoderReranker
from app.integrations.local_semantic import FastEmbedEmbeddingProvider, SQLiteVecSemanticIndex
from app.storage.db import connect, get_db_path, init_db
from evals.lka_evals.public_datasets import PublicDataset, load_normalized_jsonl

DATASET_DIR = Path(__file__).resolve().parents[1] / "datasets" / "public_multihop"
MODES = ("keyword", "keyword_rerank", "semantic", "hybrid", "hybrid_rerank")


def _language(text: str) -> str:
    """Coarse deterministic script label for slice reporting."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return "unknown"
    cjk = sum("\u3400" <= char <= "\u9fff" for char in letters)
    latin = sum("LATIN" in unicodedata.name(char, "") for char in letters)
    if cjk and latin:
        return "mixed"
    return "zh" if cjk else "en" if latin else "other"


def _mean_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    names = sorted({key for row in rows for key in row["metrics"]})
    return {name: statistics.fmean(row["metrics"][name] for row in rows) for name in names}


def _quality_comparison(
    rows: list[dict[str, Any]], *, recall_tolerance: float,
    all_hop_tolerance: float, max_latency_ratio: float,
) -> dict[str, Any]:
    """Compare each available mode with keyword on the same dataset slice."""
    comparisons = []
    by_key = {(row["dataset"], row["mode"]): row for row in rows}
    for row in rows:
        if row["mode"] == "keyword":
            continue
        baseline = by_key.get((row["dataset"], "keyword"))
        if row["mode_status"] != "available" or not baseline or baseline["mode_status"] != "available":
            comparisons.append({"dataset": row["dataset"], "mode": row["mode"], "status": "unavailable"})
            continue
        current, base = row["metrics"], baseline["metrics"]
        recall_delta = current["recall_at_" + str(row["top_k"])] - base["recall_at_" + str(row["top_k"])]
        hop_delta = current["all_hop_recall"] - base["all_hop_recall"]
        base_p95 = baseline["latency_ms"]["p95"]
        latency_ratio = row["latency_ms"]["p95"] / base_p95 if base_p95 else None
        gates = {
            "recall_noninferiority": recall_delta >= -recall_tolerance,
            "all_hop_noninferiority": hop_delta >= -all_hop_tolerance,
            "latency_ratio": latency_ratio is not None and latency_ratio <= max_latency_ratio,
        }
        comparisons.append({"dataset": row["dataset"], "mode": row["mode"], "status": "compared",
            "baseline_mode": "keyword", "recall_delta": recall_delta, "all_hop_recall_delta": hop_delta,
            "p95_latency_ratio": latency_ratio, "gates": gates, "passed": all(gates.values())})
    return {"thresholds": {"recall_tolerance": recall_tolerance,
            "all_hop_tolerance": all_hop_tolerance, "max_latency_ratio": max_latency_ratio},
            "comparisons": comparisons}


def rank_metrics(ranked_ids: list[str], relevant_ids: Iterable[str], k: int) -> dict[str, float]:
    """Compute binary-gain metrics for one ranked list."""
    if k < 1:
        raise ValueError("k must be positive")
    relevant = set(relevant_ids)
    top = ranked_ids[:k]
    hits = [1 if item in relevant else 0 for item in top]
    recall = sum(hits) / len(relevant) if relevant else 0.0
    reciprocal_rank = next((1.0 / index for index, hit in enumerate(hits, 1) if hit), 0.0)
    dcg = sum(hit / math.log2(index + 1) for index, hit in enumerate(hits, 1))
    ideal_count = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
    return {f"recall_at_{k}": recall, f"mrr_at_{k}": reciprocal_rank,
            f"ndcg_at_{k}": dcg / idcg if idcg else 0.0,
            "all_hop_recall": float(bool(relevant) and relevant.issubset(set(top)))}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _new_service(
    data_dir: Path, *, embedding_model: str | None = None,
    embedding_dimensions: int = 512, rerank_model: str | None = None,
    model_cache_dir: Path | None = None,
) -> KnowledgeService:
    db_path = get_db_path(data_dir)
    init_db(db_path)
    cache_dir = str(model_cache_dir or Path("./data/runtime/models"))
    provider = (
        FastEmbedEmbeddingProvider(
            model_name=embedding_model, dimensions=embedding_dimensions,
            cache_dir=cache_dir, batch_size=16, local_files_only=True,
        ) if embedding_model else None
    )
    index = (
        SQLiteVecSemanticIndex(
            conn_factory=lambda: connect(db_path), model_info=provider.model_info,
        ) if provider else None
    )
    reranker_config = RerankerConfig()
    reranker = (
        FastEmbedCrossEncoderReranker(
            model_name=rerank_model, cache_dir=cache_dir,
            batch_size=reranker_config.batch_size,
            max_candidates=reranker_config.max_candidates,
            max_query_chars=reranker_config.max_query_chars,
            max_candidate_chars=reranker_config.max_candidate_chars,
            max_concurrent_inferences=reranker_config.max_concurrent_inferences,
            queue_timeout_ms=reranker_config.queue_timeout_ms,
            local_files_only=True,
        ) if rerank_model else None
    )
    return KnowledgeService(
        lambda: connect(db_path), embedding_provider=provider,
        semantic_index=index, reranker=reranker, default_retrieval_mode="keyword",
    )


def _load_corpus(service: KnowledgeService, dataset: PublicDataset) -> None:
    for document in dataset.documents:
        uri = f"/public/{dataset.dataset}/{document.document_id}"
        service.import_text_document(KnowledgeDocumentInput(
            source=KnowledgeSourceInput(source_type=document.source_type, display_name=document.title,
                                       uri=uri, sensitivity="public", remote_policy="allow"),
            title=document.title, uri=uri, text=document.text, sensitivity="public",
            remote_policy="allow", chunk_chars=1800,
        ))


def evaluate_dataset(dataset: PublicDataset, *, mode: str, top_k: int, sample_limit: int,
                     seed: int, data_dir: Path, embedding_model: str | None = None,
                     embedding_dimensions: int = 512, rerank_model: str | None = None,
                     model_cache_dir: Path | None = None,
                     query_details: list[dict[str, Any]] | None = None,
                     capture_evidence_ids: set[str] | None = None) -> dict[str, Any]:
    queries = list(dataset.queries)
    if sample_limit and len(queries) > sample_limit:
        queries = random.Random(seed).sample(queries, sample_limit)
    requires_semantic = mode in {"semantic", "hybrid", "hybrid_rerank"}
    requires_rerank = mode in {"keyword_rerank", "hybrid_rerank"}
    unavailable_reason = (
        "No local embedding model was specified." if requires_semantic and not embedding_model
        else "No local reranker model was specified." if requires_rerank and not rerank_model
        else None
    )
    if unavailable_reason:
        return {"dataset": dataset.dataset, "mode": mode, "mode_status": "unavailable",
                "unavailable_reason": unavailable_reason,
                "query_count": len(queries), "document_count": len(dataset.documents),
                "sample_limit": sample_limit, "seed": seed, "top_k": top_k,
                "metrics": None, "forbidden_leak_count": None, "fallback_rate": None,
                "failure_count": 0, "failure_rate": 0.0,
                "latency_ms": {"p50": None, "p95": None}}

    setup_wall_started = time.perf_counter()
    setup_cpu_started = time.process_time()
    service = _new_service(
        data_dir, embedding_model=embedding_model if requires_semantic else None,
        embedding_dimensions=embedding_dimensions,
        rerank_model=rerank_model if requires_rerank else None,
        model_cache_dir=model_cache_dir,
    )
    reranker_probe_ms = 0.0
    try:
        if requires_rerank:
            probe_started = time.perf_counter()
            service._reranker.score("availability probe", ["availability probe"])
            reranker_probe_ms = (time.perf_counter() - probe_started) * 1000
    except Exception as exc:  # noqa: BLE001 - report missing local model as unavailable
        return {"dataset": dataset.dataset, "mode": mode, "mode_status": "unavailable",
                "unavailable_reason": f"Local reranker unavailable: {type(exc).__name__}",
                "query_count": len(queries), "document_count": len(dataset.documents),
                "sample_limit": sample_limit, "seed": seed, "top_k": top_k,
                "metrics": None, "forbidden_leak_count": None, "fallback_rate": None,
                "failure_count": 0, "failure_rate": 0.0,
                "latency_ms": {"p50": None, "p95": None}}
    import_started = time.perf_counter()
    _load_corpus(service, dataset)
    corpus_import_ms = (time.perf_counter() - import_started) * 1000
    index_build_ms = 0.0
    if requires_semantic:
        try:
            index_started = time.perf_counter()
            service.sync_semantic_index(allow_model_download=False)
            index_build_ms = (time.perf_counter() - index_started) * 1000
        except Exception as exc:  # noqa: BLE001 - offline capability is explicit
            return {"dataset": dataset.dataset, "mode": mode, "mode_status": "unavailable",
                    "unavailable_reason": f"Local embedding unavailable: {type(exc).__name__}",
                    "query_count": len(queries), "document_count": len(dataset.documents),
                    "sample_limit": sample_limit, "seed": seed, "top_k": top_k,
                    "metrics": None, "forbidden_leak_count": None, "fallback_rate": None,
                    "failure_count": 0, "failure_rate": 0.0,
                    "latency_ms": {"p50": None, "p95": None}}
    setup_wall_ms = (time.perf_counter() - setup_wall_started) * 1000
    setup_cpu_ms = (time.process_time() - setup_cpu_started) * 1000
    by_title: dict[str, list[str]] = {}
    by_uri = {f"/public/{dataset.dataset}/{doc.document_id}": doc.document_id
              for doc in dataset.documents}
    for doc in dataset.documents:
        by_title.setdefault(doc.title, []).append(doc.document_id)
    durations: list[float] = []
    accum: dict[str, list[float]] = {f"recall_at_{top_k}": [], f"mrr_at_{top_k}": [],
        f"supporting_doc_recall_at_{top_k}": [], f"ndcg_at_{top_k}": [],
        "all_hop_recall": []}
    forbidden_leaks = failures = fallbacks = 0
    query_rows: list[dict[str, Any]] = []
    for query in queries:
        started = time.perf_counter()
        try:
            retrieval_mode = "hybrid" if mode == "hybrid_rerank" else (
                "keyword" if mode == "keyword_rerank" else mode
            )
            result = service.search(query=query.question, limit=top_k,
                                    mode=retrieval_mode,
                                    max_chunks_per_document=2)
            durations.append((time.perf_counter() - started) * 1000)
            expected_mode = retrieval_mode
            fell_back = result.applied_mode != expected_mode or bool(result.retrieval_warning) or (
                requires_rerank and not result.rerank_applied
            )
            if fell_back:
                fallbacks += 1
            ranked: list[str] = []
            for item in result.results:
                document_id = by_uri.get(item.uri or "")
                if document_id is None:
                    matching_ids = by_title.get(item.title, [])
                    if len(matching_ids) == 1:
                        document_id = matching_ids[0]
                if document_id is not None and document_id not in ranked:
                    ranked.append(document_id)
            relevant = set(query.supporting_document_ids)
            if not relevant:
                relevant.update(doc_id for title in query.supporting_titles
                                for doc_id in by_title.get(title, []))
            values = rank_metrics(ranked, relevant, top_k)
            values[f"supporting_doc_recall_at_{top_k}"] = values[f"recall_at_{top_k}"]
            for name, value in values.items():
                accum[name].append(value)
            query_forbidden_leaks = len(set(ranked[:top_k]) & set(query.forbidden_document_ids))
            forbidden_leaks += query_forbidden_leaks
            supporting_types = {doc.source_type for doc in dataset.documents
                                if doc.document_id in relevant}
            source_slice = "+".join(sorted(supporting_types)) if supporting_types else "unknown"
            query_rows.append({"query_id": query.query_id, "dataset": query.dataset or dataset.dataset,
                "mode": mode, "source": source_slice, "language": _language(query.question),
                "metrics": values, "latency_ms": durations[-1], "failed": False,
                "fallback": float(fell_back), "forbidden_leaks": query_forbidden_leaks})
            if query_details is not None:
                detail = {**query_rows[-1], "ranked_document_ids": ranked,
                          "search": result.model_dump(mode="json")}
                if capture_evidence_ids and query.query_id in capture_evidence_ids:
                    try:
                        loaded = service.load_chunks(
                            chunk_ids=[item.chunk_id for item in result.results[:top_k]],
                            max_chars_per_chunk=1800,
                        )
                        detail["evidence"] = [chunk.model_dump(mode="json") for chunk in loaded.chunks]
                        detail["evidence_filtered_count"] = loaded.filtered_count
                    except Exception as exc:  # noqa: BLE001 - QA loading must not distort rank metrics
                        detail["evidence_error_type"] = type(exc).__name__
                query_details.append(detail)
        except Exception:  # noqa: BLE001 - count benchmark query failures
            failures += 1
            durations.append((time.perf_counter() - started) * 1000)
            for values in accum.values():
                values.append(0.0)
            query_rows.append({"query_id": query.query_id, "dataset": query.dataset or dataset.dataset,
                "mode": mode, "source": "unknown", "language": _language(query.question),
                "metrics": {name: 0.0 for name in accum}, "latency_ms": durations[-1],
                "failed": True, "fallback": 0.0, "forbidden_leaks": 0})
            if query_details is not None:
                query_details.append(dict(query_rows[-1]))
    count = len(queries)
    slices: dict[str, list[dict[str, Any]]] = {}
    for row in query_rows:
        for dimension in ("dataset", "mode", "source", "language"):
            key = f"{dimension}:{row[dimension]}"
            slices.setdefault(key, []).append(row)
    return {"dataset": dataset.dataset, "mode": mode, "mode_status": "available", "query_count": count,
            "document_count": len(dataset.documents), "sample_limit": sample_limit,
            "seed": seed, "top_k": top_k,
            "metrics": {name: statistics.fmean(values) if values else 0.0
                        for name, values in accum.items()},
            "forbidden_leak_count": forbidden_leaks, "fallback_rate": fallbacks / count if count else 0.0,
            "failure_count": failures, "failure_rate": failures / count if count else 0.0,
            "latency_ms": {"p50": _percentile(durations, .50), "p95": _percentile(durations, .95)},
            "setup_timing_ms": {"corpus_import": corpus_import_ms, "index_build": index_build_ms,
                                 "total_wall": setup_wall_ms, "total_cpu": setup_cpu_ms,
                                 "reranker_probe": reranker_probe_ms,
                                 "model_warmup_total": index_build_ms + reranker_probe_ms,
                                 "query_p50": _percentile(durations, .50),
                                 "query_p95": _percentile(durations, .95)},
            "slices": {key: {"query_count": len(rows), "metrics": _mean_metrics(rows),
                             "forbidden_leak_count": sum(item["forbidden_leaks"] for item in rows),
                             "fallback_rate": statistics.fmean(item["fallback"] for item in rows),
                             "failure_rate": statistics.fmean(item["failed"] for item in rows),
                             "latency_ms": {
                                 "p50": _percentile([item["latency_ms"] for item in rows], .50),
                                 "p95": _percentile([item["latency_ms"] for item in rows], .95),
                             }}
                       for key, rows in sorted(slices.items())}}


def run_benchmark(*, datasets: list[Path], modes: list[str], top_k: int, sample_limit: int,
                  seed: int, output: Path | None = None, embedding_model: str | None = None,
                  embedding_dimensions: int = 512, rerank_model: str | None = None,
                  model_cache_dir: Path | None = None, recall_tolerance: float = 0.05,
                  all_hop_tolerance: float = 0.05, max_latency_ratio: float = 2.0) -> dict[str, Any]:
    if top_k < 1 or sample_limit < 0:
        raise ValueError("top_k must be positive and sample_limit non-negative")
    unknown = set(modes) - set(MODES)
    if unknown:
        raise ValueError(f"unsupported modes: {', '.join(sorted(unknown))}")
    if min(recall_tolerance, all_hop_tolerance) < 0 or max_latency_ratio <= 0:
        raise ValueError("quality tolerances must be non-negative and latency ratio positive")
    rows = []
    manifest = []
    for path in datasets:
        raw = path.read_bytes()
        dataset = load_normalized_jsonl(path)
        manifest.append({"dataset": dataset.dataset, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                         "documents": len(dataset.documents), "queries": len(dataset.queries)})
        for mode in modes:
            with tempfile.TemporaryDirectory(prefix="lka-retrieval-eval-") as temp:
                rows.append(evaluate_dataset(dataset, mode=mode, top_k=top_k,
                    sample_limit=sample_limit, seed=seed, data_dir=Path(temp),
                    embedding_model=embedding_model, embedding_dimensions=embedding_dimensions,
                    rerank_model=rerank_model, model_cache_dir=model_cache_dir))
    effective_cache_dir = model_cache_dir or Path("./data/runtime/models")
    report = {"schema_version": 2, "benchmark": "public_retrieval_offline",
              "manifest": manifest, "configuration": {"modes": modes, "top_k": top_k,
              "sample_limit": sample_limit, "seed": seed, "hardware": platform.platform(),
              "model_warmup_policy": "embedding index build and/or reranker probe before timed queries",
              "corpus_import_and_index_build_excluded_from_query_latency": True,
              "embedding_model": embedding_model,
              "embedding_dimensions": embedding_dimensions, "rerank_model": rerank_model,
              "retrieval_profile": "knowledge.search default (max 2 chunks/document)",
              "reranker_batch_size": RerankerConfig().batch_size,
              "reranker_max_candidates": RerankerConfig().max_candidates,
              "reranker_max_query_chars": RerankerConfig().max_query_chars,
              "reranker_max_candidate_chars": RerankerConfig().max_candidate_chars,
              "model_cache_dir": str(effective_cache_dir),
              "model_cache_dir_exists": effective_cache_dir.exists(),
              "python_version": platform.python_version(), "platform": platform.platform(),
              "cache_timing_note": "query latency is warm after embedding index build and reranker probe",
              "quality_gate_comparison": _quality_comparison(rows,
                  recall_tolerance=recall_tolerance, all_hop_tolerance=all_hop_tolerance,
                  max_latency_ratio=max_latency_ratio)}, "results": rows}
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", action="append", type=Path,
                        help="normalized JSONL path; repeatable (default: all bundled public datasets)")
    parser.add_argument("--mode", action="append", choices=MODES,
                        help="repeatable; semantic/hybrid are reported unavailable without a local embedding provider (default: all modes)")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--sample-limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--embedding-model", help="cached local FastEmbed model; never downloaded by the runner")
    parser.add_argument("--embedding-dimensions", type=int, default=512)
    parser.add_argument("--rerank-model", help="cached local FastEmbed cross-encoder; never downloaded by the runner")
    parser.add_argument("--model-cache-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = args.dataset or sorted(DATASET_DIR.glob("*.jsonl"))
    report = run_benchmark(datasets=paths, modes=args.mode or list(MODES), top_k=args.top_k,
                           sample_limit=args.sample_limit, seed=args.seed, output=args.output,
                           embedding_model=args.embedding_model,
                           embedding_dimensions=args.embedding_dimensions,
                           rerank_model=args.rerank_model, model_cache_dir=args.model_cache_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
