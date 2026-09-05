"""Composable retrieval contracts for local knowledge evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingModelInfo:
    provider_name: str
    model_name: str
    dimensions: int
    normalized: bool

    @property
    def index_key(self) -> str:
        return f"{self.provider_name}:{self.model_name}:{self.dimensions}"


class EmbeddingProvider(Protocol):
    @property
    def model_info(self) -> EmbeddingModelInfo: ...

    def embed_passages(self, passages: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...

    def prepare(self, *, allow_model_download: bool) -> None: ...


@dataclass(frozen=True)
class SemanticVectorRecord:
    chunk_id: str
    checksum: str
    vector: list[float]


@dataclass(frozen=True)
class SemanticHit:
    chunk_id: str
    rank: int
    score: float


@dataclass(frozen=True)
class SemanticIndexStatus:
    available: bool
    index_key: str
    indexed_chunks: int
    reason: str | None = None


class SemanticIndex(Protocol):
    def upsert(self, records: Sequence[SemanticVectorRecord]) -> tuple[int, int]: ...

    def remove(self, chunk_ids: Sequence[str]) -> int: ...

    def reconcile(self, active_chunk_ids: Iterable[str]) -> int: ...

    def search(self, vector: Sequence[float], limit: int) -> list[SemanticHit]: ...

    def status(self) -> SemanticIndexStatus: ...


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk_id: str
    rank: int
    score: float | None
    channel: str


class CandidateRetriever(Protocol):
    channel: str

    def retrieve(self, query: str, limit: int) -> list[RetrievalCandidate]: ...


class RankFusion(Protocol):
    def fuse(
        self,
        candidates_by_channel: dict[str, Sequence[RetrievalCandidate]],
        limit: int,
    ) -> list[RetrievalCandidate]: ...


class ReciprocalRankFusion:
    """Fuse ranking channels without comparing incompatible score scales."""

    def __init__(self, *, rank_constant: int = 60) -> None:
        self.rank_constant = rank_constant

    def fuse(
        self,
        candidates_by_channel: dict[str, Sequence[RetrievalCandidate]],
        limit: int,
    ) -> list[RetrievalCandidate]:
        scores: dict[str, float] = defaultdict(float)
        channels: dict[str, list[str]] = defaultdict(list)
        for channel, candidates in candidates_by_channel.items():
            for candidate in candidates:
                scores[candidate.chunk_id] += 1 / (self.rank_constant + candidate.rank)
                channels[candidate.chunk_id].append(channel)
        ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
        return [
            RetrievalCandidate(
                chunk_id=chunk_id,
                rank=index + 1,
                score=scores[chunk_id],
                channel="+".join(sorted(channels[chunk_id])),
            )
            for index, chunk_id in enumerate(ordered[:limit])
        ]


@dataclass(frozen=True)
class RetrievalSelection:
    requested_mode: str
    applied_mode: str
    candidates: list[RetrievalCandidate]
    available_channels: list[str]
    warning: str | None = None


class ConfigurableKnowledgeRetriever:
    """Select and fuse registered candidate retrievers without provider knowledge."""

    def __init__(
        self,
        *,
        retrievers: Sequence[CandidateRetriever],
        fusion: RankFusion | None = None,
    ) -> None:
        self._retrievers = {retriever.channel: retriever for retriever in retrievers}
        self._fusion = fusion or ReciprocalRankFusion()

    def retrieve(self, *, query: str, limit: int, mode: str) -> RetrievalSelection:
        requested_mode = mode if mode in {"keyword", "semantic", "hybrid"} else "hybrid"
        requested_channels = {
            "keyword": ["keyword"],
            "semantic": ["semantic"],
            "hybrid": ["keyword", "semantic"],
        }[requested_mode]
        candidates_by_channel: dict[str, list[RetrievalCandidate]] = {}
        for channel in requested_channels:
            retriever = self._retrievers.get(channel)
            if retriever is None:
                continue
            candidates = retriever.retrieve(query, max(limit * 4, limit))
            if candidates:
                candidates_by_channel[channel] = candidates
        if not candidates_by_channel and requested_mode != "keyword":
            fallback = self._retrievers.get("keyword")
            if fallback is not None:
                candidates_by_channel["keyword"] = fallback.retrieve(query, max(limit * 4, limit))
                return RetrievalSelection(
                    requested_mode=requested_mode,
                    applied_mode="keyword",
                    candidates=candidates_by_channel["keyword"][:limit],
                    available_channels=sorted(self._retrievers),
                    warning="semantic index has no eligible vectors; keyword retrieval was used",
                )
        if len(candidates_by_channel) == 1:
            channel, candidates = next(iter(candidates_by_channel.items()))
            return RetrievalSelection(
                requested_mode=requested_mode,
                applied_mode=channel,
                candidates=candidates[:limit],
                available_channels=sorted(self._retrievers),
            )
        return RetrievalSelection(
            requested_mode=requested_mode,
            applied_mode="hybrid" if candidates_by_channel else requested_mode,
            candidates=self._fusion.fuse(candidates_by_channel, limit),
            available_channels=sorted(self._retrievers),
        )


class CallableCandidateRetriever:
    """Adapter for deterministic retrieval sources owned by another service."""

    def __init__(
        self,
        *,
        channel: str,
        callback: Callable[[str, int], list[RetrievalCandidate]],
    ) -> None:
        self.channel = channel
        self._callback = callback

    def retrieve(self, query: str, limit: int) -> list[RetrievalCandidate]:
        return self._callback(query, limit)


class SemanticCandidateRetriever:
    channel = "semantic"

    def __init__(self, *, provider: EmbeddingProvider, index: SemanticIndex) -> None:
        self._provider = provider
        self._index = index

    def retrieve(self, query: str, limit: int) -> list[RetrievalCandidate]:
        if not query.strip() or not self._index.status().available:
            return []
        return [
            RetrievalCandidate(
                chunk_id=hit.chunk_id,
                rank=hit.rank,
                score=hit.score,
                channel=self.channel,
            )
            for hit in self._index.search(self._provider.embed_query(query), limit)
        ]
