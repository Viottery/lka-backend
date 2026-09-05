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
