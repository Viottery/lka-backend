"""Bounded public multi-hop tasks through the actual Agent, not direct QA.

Each case imports the entire normalized dataset into an isolated runtime. Gold
answers/supporting IDs are used only after the turn, never as planning hints.
Two explicitly selected cases maximum; no judge, repair, or implicit paid retry.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from evals.lka_evals.live_budget import LiveBudget
from evals.lka_evals.public_datasets import (
    PublicDataset,
    PublicQuery,
    fixture_payload,
    load_normalized_jsonl,
)
from evals.lka_evals.public_qa import RunBudget, parse_answer, score_answer
from evals.lka_evals.public_retrieval import DATASET_DIR
from scripts import eval_realworld
from scripts.eval_runtime_memory_quality import LEDGER, MODEL, OUTPUT, isolated_config

DATASETS = ("hotpotqa_200", "2wikimultihopqa_200")
SOURCE_FILES = (
    "app/core/agent_turn.py", "app/core/tools.py", "app/core/llm/service.py",
    "app/tool_packages/knowledge.py", "app/integrations/local_semantic.py",
    "app/integrations/local_reranker.py", "scripts/eval_realworld.py",
)


def source_fingerprints() -> dict[str, str]:
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def make_case(dataset: PublicDataset, query_id: str) -> tuple[dict, PublicQuery]:
    query = next((row for row in dataset.queries if row.query_id == query_id), None)
    if query is None or not dataset.documents:
        raise ValueError("An existing query and nonempty full corpus are required")
    # This is the user's task/answer format, not a tool recipe or gold hint.
    goal = (
        "Answer this question using only the local knowledge base. Cite the exact source_ref "
        "values of evidence you actually read. If evidence is insufficient, abstain. "
        'Return only short JSON {"answer":"short answer", "citations":["source_ref"], '
        '"abstain":false}; abstention uses an empty answer and empty citations.\nQuestion: '
        + query.question
    )
    return {"goal": goal, "files": {}, "read_only": True,
            "setup": {"knowledge": [fixture_payload(dataset)]}}, query


def delivered_evidence(events: list[dict], dataset: str) -> dict:
    """Source-ref delivery/coverage diagnostics, not semantic grounding."""
    searched, loaded, traces = set(), set(), []
    for event in events:
        result = event.get("result", {})
        output = result.get("output")
        if result.get("status") != "completed" or not isinstance(output, dict):
            continue
        tool = event.get("tool_name")
        if tool == "knowledge.search":
            refs, records = searched, output.get("results", [])
            traces.append({key: output.get(key) for key in
                           ("query", "applied_mode", "rerank_applied", "retrieval_warning", "rewrite_trace")})
        elif tool == "knowledge.load_chunks":
            refs, records = loaded, output.get("chunks", [])
        else:
            continue
        for record in records:
            ref = record.get("source_ref") if isinstance(record, dict) else None
            if isinstance(ref, str) and ref:
                refs.add(ref)

    def ids(refs):
        prefix = "/public/" + dataset + "/"
        return sorted({ref.removeprefix("public_dataset:").split("#")[0][len(prefix):]
                       for ref in refs if ref.removeprefix("public_dataset:").startswith(prefix)})

    return {"search_source_refs": sorted(searched), "loaded_source_refs": sorted(loaded),
            "search_document_ids": ids(searched), "loaded_document_ids": ids(loaded),
            "retrievals": traces, "label": "delivered source refs; NOT entailment or full model-read proof"}


def analyze_report(report: dict, dataset: PublicDataset, query: PublicQuery) -> dict:
    result = report.get("result", {})
    evidence = delivered_evidence(result.get("tool_events", []), dataset.dataset)
    analysis = {"label": "actual Agent small selected failure/heldout diagnostic; NOT dataset accuracy",
                "dataset": dataset.dataset, "query_id": query.query_id,
                "corpus_document_count": len(dataset.documents), "gold": query.answer,
                "supporting_document_ids": query.supporting_document_ids,
                "runtime_error": report.get("error"), "task_wall_seconds": report.get("task_wall_seconds"),
                "metrics": report.get("metrics"), "evidence": evidence,
                "retrieval_config": report.get("retrieval_config"),
                "semantic_index_error": report.get("semantic_index_error"),
                "semantic_index_seconds": report.get("semantic_index_seconds"),
                "runtime_initialization_seconds": report.get("runtime_initialization_seconds"),
                "semantic_review": {"status": "pending_root_review"}}
    support = set(query.supporting_document_ids)
    analysis["search_all_hops_delivered"] = bool(support) and support.issubset(evidence["search_document_ids"])
    analysis["loaded_all_hops_delivered"] = bool(support) and support.issubset(evidence["loaded_document_ids"])
    try:
        answer = parse_answer(result.get("answer", ""))
    except (ValueError, TypeError):
        analysis["answer_format_valid"] = False
        return analysis
    analysis.update({"answer_format_valid": True, "answer": answer,
                     "project_normalized_metrics_not_official": score_answer(answer["answer"], query.answer)})
    known = set(evidence["search_source_refs"]) | set(evidence["loaded_source_refs"])
    analysis["citations_identify_delivered_refs"] = bool(answer["citations"]) and all(
        ref in known for ref in answer["citations"])
    return analysis


async def run_public_case(dataset: PublicDataset, query_id: str, *, ledger: LiveBudget,
                          output: Path = OUTPUT, runner=None, full_retrieval: bool = True) -> dict:
    started = time.perf_counter()
    case, query = make_case(dataset, query_id)
    if ledger.usd_limit > 50:
        raise ValueError("The authoritative shared evaluation budget cannot exceed 50")
    source_before = source_fingerprints()
    settings = Settings()
    configured = settings.load_local_config()
    if runner is None and (configured.llm.is_disabled() or configured.llm.model != MODEL):
        raise ValueError("Only the configured, priced deepseek-flash provider is authorized")
    config = isolated_config(configured).model_copy(update={
        "embedding": configured.embedding.model_copy(update={"local_files_only": True,
                                                               "auto_index_on_import": False}),
        "reranker": configured.reranker.model_copy(update={"local_files_only": True}),
    })
    tag = "public-agent-" + dataset.dataset + "-" + query_id + "-" + datetime.now(UTC).strftime("%H%M%S%f")
    budget = RunBudget(ledger, source_id=tag, limit=20)
    name = "public_agent_" + dataset.dataset + "_" + query_id
    with patch.dict(eval_realworld.CASES, {name: case}), patch.object(
        Settings, "load_local_config", return_value=config,
    ):
        report = await (runner or eval_realworld.run_case)(name, output=output, budget=budget,
                                                         timeout=180, full_retrieval=full_retrieval)
    analysis = analyze_report(report, dataset, query)
    source_after = source_fingerprints()
    analysis["source_fingerprints_before_preparation"] = source_before
    analysis["source_fingerprints_after_turn"] = source_after
    analysis["source_files_changed_during_case"] = source_before != source_after
    analysis["bulk_fixture_preparation"] = "auto-index on import disabled; one explicit cached index sync"
    analysis["case_wall_seconds_including_fixture_preparation"] = time.perf_counter() - started
    root = Path(report["private_artifacts"]).resolve()
    if root.parent != output.resolve() or not root.name.startswith(name + "_"):
        raise ValueError("Evaluation artifact path is outside its isolated output")
    root.chmod(0o700)
    raw = root / "report.json"
    raw.chmod(0o600)
    analysis["raw_report_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    analysis["run_budget"] = budget.snapshot()["run_budget"]
    analysis["private_artifacts"] = str(root)
    analysis["shared_budget_after"] = ledger.snapshot()
    path = root / "public_analysis.json"
    path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--dataset", choices=DATASETS, required=True)
    parser.add_argument("--query-id", action="append", required=True)
    parser.add_argument("--keyword-only", action="store_true", help="explicit no-local-model ablation")
    args = parser.parse_args()
    if not args.remote or not 1 <= len(args.query_id) <= 2 or len(set(args.query_id)) != len(args.query_id):
        parser.error("--remote and one or two distinct explicit query IDs required; Root GO before dispatch")
    dataset = load_normalized_jsonl(DATASET_DIR / (args.dataset + ".jsonl"))
    # Validate the whole requested batch before the first paid task.
    for query_id in args.query_id:
        make_case(dataset, query_id)
    ledger = LiveBudget(LEDGER, usd_limit=50)
    for query_id in args.query_id:
        analysis = asyncio.run(run_public_case(dataset, query_id, ledger=ledger,
                                               full_retrieval=not args.keyword_only))
        print(json.dumps({key: value for key, value in analysis.items() if key not in {
            "evidence", "source_fingerprints_before_preparation", "source_fingerprints_after_turn",
        }},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
