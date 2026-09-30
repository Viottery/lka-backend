#!/usr/bin/env python3
"""Probe keyword versus hybrid retrieval in an isolated synthetic knowledge DB.

Uses only the checked-in knowledge fixture and an already cached local embedding
model. It never downloads a model or opens the user's production database.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput
from evals.lka_evals.public_retrieval import _new_service


QUERIES = (
    ("exact", "生产发布 两名维护者", "project_policy.md"),
    ("paraphrase", "线上上线需要几个人审批", "project_policy.md"),
    ("incident", "系统请求响应很慢时应该先怎么排查", "incident_runbook.md"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path,
        default=Path("evals/fixtures/knowledge/retrieval_benchmark.json"),
    )
    parser.add_argument(
        "--model-cache", type=Path, default=Path("data/runtime/models"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="laya-route-kb-") as temp:
        service = _new_service(
            Path(temp),
            embedding_model="BAAI/bge-small-zh-v1.5",
            embedding_dimensions=512,
            model_cache_dir=args.model_cache.resolve(),
        )
        imported = 0
        for document in fixture["documents"]:
            source = document["source"]
            if source.get("sensitivity") == "secret" or source.get("remote_policy") == "deny":
                continue
            service.import_text_document(
                KnowledgeDocumentInput(
                    source=KnowledgeSourceInput(**source),
                    title=document["title"],
                    uri=document["uri"],
                    mime_type=document["mime_type"],
                    text=document["text"],
                    chunk_chars=1800,
                )
            )
            imported += 1
        started = time.perf_counter()
        index_status = service.sync_semantic_index(allow_model_download=False)
        index_ms = round((time.perf_counter() - started) * 1000, 1)

        rows = []
        for case_id, query, expected_title in QUERIES:
            for mode in ("keyword", "hybrid"):
                started = time.perf_counter()
                result = service.search(query=query, mode=mode, limit=3)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                titles = [item.title for item in result.results]
                rows.append({
                    "case_id": case_id,
                    "query": query,
                    "expected_title": expected_title,
                    "requested_mode": mode,
                    "applied_mode": result.applied_mode,
                    "warning": result.retrieval_warning,
                    "elapsed_ms": elapsed_ms,
                    "titles": titles,
                    "hit_at_1": bool(titles and titles[0] == expected_title),
                })
    report = {
        "fixture": str(args.fixture),
        "imported_documents": imported,
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "index_ms": index_ms,
        "index_enabled": index_status.enabled,
        "rows": rows,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
