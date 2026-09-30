from __future__ import annotations

from pathlib import Path
from threading import Barrier, get_ident

import pytest

from app.domains.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeSearchResult,
    KnowledgeService,
    KnowledgeSourceInput,
)
from app.storage.db import connect, get_db_path, init_db


class RecordingReranker:
    max_candidates = 100

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append((query, list(candidates)))
        return [1.0 if "rewrite evidence" in candidate else 0.0 for candidate in candidates]


def _import(service: KnowledgeService, title: str, text: str) -> str:
    result = service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                display_name=title,
                uri=f"/knowledge/{title}",
                sensitivity="public",
                remote_policy="allow",
            ),
            title=title,
            text=text,
            sensitivity="public",
            remote_policy="allow",
        )
    )
    return result.source_id


def _service(tmp_path: Path, reranker: RecordingReranker | None = None) -> KnowledgeService:
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    return KnowledgeService(lambda: connect(db_path), reranker=reranker)


def test_search_queries_unions_original_and_rewrite_with_one_global_rerank(tmp_path):
    reranker = RecordingReranker()
    service = _service(tmp_path, reranker)
    _import(service, "original.md", "original evidence marker")
    _import(service, "rewrite.md", "rewrite evidence marker")

    result, traces = service.search_queries(
        query="original evidence",
        rewritten_queries=["rewrite evidence"],
        limit=2,
        mode="keyword",
    )

    assert {item.title for item in result.results} == {"original.md", "rewrite.md"}
    assert result.results[0].title == "rewrite.md"
    assert result.rerank_applied is True
    assert len(reranker.calls) == 1
    assert len(reranker.calls[0][1]) == 2
    assert [trace["query"] for trace in traces] == ["original evidence", "rewrite evidence"]
    assert all(trace["hit_count"] >= 1 and trace["query_id"] for trace in traces)


def test_search_queries_applies_scopes_to_each_query_and_limits_final_top_k(tmp_path, monkeypatch):
    service = _service(tmp_path)
    allowed_source = _import(service, "allowed.md", "scope marker original")
    _import(service, "blocked.md", "scope marker rewrite")
    calls: list[dict] = []
    original_search = service.search

    def recording_search(**kwargs):
        calls.append(kwargs.copy())
        return original_search(**kwargs)

    monkeypatch.setattr(service, "search", recording_search)
    result, _ = service.search_queries(
        query="scope marker original",
        rewritten_queries=["scope marker rewrite"],
        limit=1,
        source_ids=[allowed_source],
        account_ids=["acct-1"],
        mode="keyword",
        keyword_candidate_k=50,
    )

    assert len(calls) == 2
    assert all(call["source_ids"] == [allowed_source] for call in calls)
    assert all(call["account_ids"] == ["acct-1"] for call in calls)
    assert all(call["limit"] == 50 for call in calls)
    assert all(call["_apply_rerank"] is False for call in calls)
    assert len(result.results) <= 1
    assert all(item.source_id == allowed_source for item in result.results)


def test_search_queries_run_three_or_more_queries_concurrently(tmp_path, monkeypatch):
    service = _service(tmp_path)
    barrier = Barrier(3)
    thread_ids: list[int] = []

    def concurrent_search(*, query, **_kwargs):
        thread_ids.append(get_ident())
        barrier.wait(timeout=3)
        return KnowledgeSearchResult(
            query=query, query_id=f"id-{query}", results=[],
            requested_mode="keyword", applied_mode="keyword",
        )

    monkeypatch.setattr(service, "search", concurrent_search)
    result, traces = service.search_queries(
        query="original", rewritten_queries=["first", "second"],
        mode="keyword", max_parallel_searches=3,
    )
    assert result.results == []
    assert [trace["query"] for trace in traces] == ["original", "first", "second"]
    assert len(set(thread_ids)) == 3


def test_total_candidate_budget_is_split_across_queries_and_channels(tmp_path, monkeypatch):
    service = _service(tmp_path)
    calls: list[dict] = []

    def recording_search(*, query, **kwargs):
        calls.append(kwargs)
        return KnowledgeSearchResult(
            query=query, query_id=f"id-{query}", results=[],
            requested_mode="hybrid", applied_mode="hybrid",
        )

    monkeypatch.setattr(service, "search", recording_search)
    service.search_queries(
        query="original", rewritten_queries=["first", "second", "third"],
        mode="hybrid", max_total_candidates=60, keyword_candidate_k=400,
        semantic_candidate_k=400,
    )
    assert len(calls) == 4
    assert all(call["limit"] == 7 for call in calls)
    assert all(call["keyword_candidate_k"] == 7 for call in calls)
    assert all(call["semantic_candidate_k"] == 7 for call in calls)


def test_rejects_budget_smaller_than_query_channel_count(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="candidate budget"):
        service.search_queries(
            query="original", rewritten_queries=[f"query {index}" for index in range(16)],
            mode="hybrid", max_total_candidates=30,
        )
