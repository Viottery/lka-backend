from __future__ import annotations

from pathlib import Path

from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeService, KnowledgeSourceInput
from app.domains.knowledge_retrieval import EmbeddingModelInfo
from app.integrations.local_semantic import SQLiteVecSemanticIndex
from app.storage.db import connect, get_db_path, init_db


class DeterministicEmbeddingProvider:
    """Offline test provider proving service behavior is not tied to FastEmbed."""

    model_info = EmbeddingModelInfo(
        provider_name="test",
        model_name="deterministic",
        dimensions=2,
        normalized=True,
    )

    def embed_passages(self, passages: list[str]) -> list[list[float]]:
        return [self._vector(value) for value in passages]

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)

    def prepare(self, *, allow_model_download: bool) -> None:
        _ = allow_model_download

    def _vector(self, value: str) -> list[float]:
        return [1.0, 0.0] if any(term in value for term in ("审批", "批准", "同意")) else [0.0, 1.0]


def _semantic_service(tmp_path: Path) -> KnowledgeService:
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    provider = DeterministicEmbeddingProvider()
    index = SQLiteVecSemanticIndex(conn_factory=lambda: connect(db_path), model_info=provider.model_info)
    return KnowledgeService(
        lambda: connect(db_path),
        embedding_provider=provider,
        semantic_index=index,
        default_retrieval_mode="hybrid",
    )


def _import(service: KnowledgeService, *, title: str, text: str, sensitivity: str = "public") -> None:
    service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                display_name=title,
                uri=f"/knowledge/{title}",
                sensitivity=sensitivity,
                remote_policy="deny" if sensitivity == "secret" else "allow",
            ),
            title=title,
            uri=f"/knowledge/{title}",
            text=text,
            sensitivity=sensitivity,
            remote_policy="deny" if sensitivity == "secret" else "allow",
        )
    )


def test_semantic_index_is_local_and_retrieves_paraphrase(tmp_path):
    service = _semantic_service(tmp_path)
    _import(service, title="policy.md", text="生产发布必须获得两名维护者审批。")
    _import(service, title="runbook.md", text="事故发生后先收集高延迟请求样本。")

    sync = service.sync_semantic_index()
    result = service.search(query="上线需要多少人同意", limit=3, mode="semantic")

    assert sync.enabled is True
    assert sync.embedded_chunks == 2
    assert result.requested_mode == "semantic"
    assert result.applied_mode == "semantic"
    assert result.results[0].title == "policy.md"
    assert result.results[0].retrieval_channels == ["semantic"]


def test_hybrid_retrieval_fuses_channels_and_never_embeds_secret_chunks(tmp_path):
    service = _semantic_service(tmp_path)
    _import(service, title="policy.md", text="生产发布必须获得两名维护者审批。")
    _import(service, title="secrets.md", text="内部审批密钥轮换记录。", sensitivity="secret")

    sync = service.sync_semantic_index()
    result = service.search(query="审批", limit=3, mode="hybrid")

    assert sync.indexed_chunks == 1
    assert result.applied_mode == "hybrid"
    assert result.results[0].title == "policy.md"
    assert set(result.results[0].retrieval_channels) == {"keyword", "semantic"}
    assert all(item.title != "secrets.md" for item in result.results)


def test_source_scope_applies_before_keyword_candidate_limit(tmp_path):
    service = _semantic_service(tmp_path)
    for index in range(25):
        _import(service, title=f"other-{index}.md", text="unique sentinel approval keyword")
    authorized = service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                display_name="allowed.md", uri="/knowledge/allowed.md",
                sensitivity="public", remote_policy="allow",
            ),
            title="allowed.md", text="unique sentinel approval keyword",
            sensitivity="public", remote_policy="allow",
        )
    )

    result = service.search(
        query="unique sentinel approval keyword", limit=1, mode="keyword",
        source_ids=[authorized.source_id],
    )

    assert [item.title for item in result.results] == ["allowed.md"]


def test_document_diversity_cap_keeps_other_evidence_visible(tmp_path):
    service = _semantic_service(tmp_path)
    service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(display_name="long.md", uri="/knowledge/long.md"),
            title="long.md", uri="/knowledge/long.md",
            text="alphaunique " * 300, chunk_chars=200,
        )
    )
    _import(service, title="short.md", text="alphaunique concise independent evidence")

    result = service.search(
        query="alphaunique", mode="keyword", limit=4,
        keyword_candidate_k=20, max_chunks_per_document=1,
    )

    assert {item.title for item in result.results} == {"long.md", "short.md"}
    assert len(result.results) == 2


def test_source_scope_applies_before_semantic_candidate_limit(tmp_path):
    service = _semantic_service(tmp_path)
    for index in range(25):
        _import(service, title=f"other-{index}.md", text="需要审批。")
    authorized = service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                display_name="allowed.md", uri="/knowledge/allowed.md",
                sensitivity="public", remote_policy="allow",
            ),
            title="allowed.md", text="必须获得审批。",
            sensitivity="public", remote_policy="allow",
        )
    )
    service.sync_semantic_index()

    result = service.search(
        query="需要同意", limit=1, mode="semantic",
        source_ids=[authorized.source_id],
    )

    assert [item.title for item in result.results] == ["allowed.md"]


def test_local_rerank_reorders_only_authorized_candidates_and_falls_back(tmp_path):
    class FakeReranker:
        max_candidates = 10

        def __init__(self):
            self.seen = []
            self.fail = False

        def score(self, query, candidates):
            self.seen = list(candidates)
            if self.fail:
                raise RuntimeError("offline model missing")
            return [1.0 if "preferred" in text else 0.0 for text in candidates]

    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    reranker = FakeReranker()
    service = KnowledgeService(lambda: connect(db_path), reranker=reranker)
    _import(service, title="other.md", text="sentinel matching terms")
    _import(service, title="secret.md", text="sentinel matching terms", sensitivity="secret")
    allowed = service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(display_name="preferred.md", uri="/knowledge/preferred.md", sensitivity="public", remote_policy="allow"),
            title="preferred.md", text="sentinel matching terms", sensitivity="public", remote_policy="allow",
        )
    )

    result = service.search(query="sentinel matching terms", limit=2, mode="keyword", source_ids=[allowed.source_id])
    assert result.rerank_applied is True
    assert [item.title for item in result.results] == ["preferred.md"]
    assert all("other.md" not in text for text in reranker.seen)
    assert all("secret.md" not in text for text in reranker.seen)
    assert result.results[0].rerank_score == 1.0

    service.search(query="sentinel matching terms", limit=3, mode="keyword")
    assert all("secret.md" not in text for text in reranker.seen)

    reranker.fail = True
    fallback = service.search(query="sentinel matching terms", limit=2, mode="keyword", source_ids=[allowed.source_id])
    assert fallback.rerank_applied is False
    assert fallback.results[0].rerank_score is None
    assert "reranker unavailable" in fallback.retrieval_warning


def test_empty_source_grant_cannot_load_or_search(tmp_path):
    service = _semantic_service(tmp_path)
    imported = service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(display_name="private.md", uri="/knowledge/private.md", sensitivity="public", remote_policy="allow"),
            title="private.md", text="private sentinel", sensitivity="public", remote_policy="allow",
        )
    )
    service.sync_semantic_index()
    assert service.search(query="private", mode="hybrid", source_ids=[]).results == []
    chunk_id = service.search(query="private", mode="keyword").results[0].chunk_id
    assert service.load_chunks(chunk_ids=[chunk_id], source_ids=[]).chunks == []
    assert service.list_sources(source_ids=[]) == []
    import pytest

    with pytest.raises(ValueError, match="not found"):
        service.load_document(document_id=imported.document_id, source_ids=[])


def test_candidate_cache_is_scoped_and_invalidated_after_import(tmp_path):
    service = _semantic_service(tmp_path)
    _import(service, title="first.md", text="审批需要签字。")
    service.sync_semantic_index()
    provider = service._embedding_provider
    original = provider.embed_query
    calls = 0

    def counted(query):
        nonlocal calls
        calls += 1
        return original(query)

    provider.embed_query = counted
    first = service.search(query="审批", mode="semantic", cache_namespace="session_a")
    second = service.search(query="审批", mode="semantic", cache_namespace="session_a")
    assert [item.chunk_id for item in first.results] == [item.chunk_id for item in second.results]
    assert calls == 1
    service.search(query="审批", mode="semantic", cache_namespace="session_b")
    assert calls == 2
    _import(service, title="second.md", text="审批材料。")
    service.sync_semantic_index()
    service.search(query="审批", mode="semantic", cache_namespace="session_a")
    assert calls == 3
