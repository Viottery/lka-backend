"""Bounded, scope-aware candidate discovery, not a semantic merge decision."""

from __future__ import annotations

import re
import unicodedata

from app.domains.memory import MemoryRecord, MemoryService


def canonical_memory(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip().rstrip("。.! ")


def _terms(text: str) -> set[str]:
    text = canonical_memory(text)
    terms = set(re.findall(r"[a-z0-9_+-]{2,}", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run)
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def scoped_memories(memory: MemoryService, scope: str, project_id: str | None):
    """Page the whole scope: old preferences must not vanish behind recency."""
    offset = 0
    while True:
        page = memory.list(scope=scope, project_id=project_id, limit=1000, offset=offset)
        for record in page:
            if not record.metadata.get("needs_review") and memory.get_active(record.memory_id) is not None:
                yield record
        if len(page) < 1000:
            break
        offset += len(page)


def reconciliation_candidates(
    memory: MemoryService, queries: list[str], *, scope: str, project_id: str | None = None,
    max_items: int = 48, max_chars: int = 16_000, related_only: bool = False,
) -> list[MemoryRecord]:
    """Lexical/character recall + broad bounded fallback; no new model call.

    Chinese character recall complements phrase-based FTS. Scores only rank
    candidates: they never establish equivalence or authorize replacement.
    """
    queries = [query[:12_000] for query in queries if query.strip()]
    terms = [_terms(query) for query in queries]
    exact = {canonical_memory(query) for query in queries}
    hits = {record.memory_id for query in queries[:4]
            for record in memory.search(query, scope=scope, project_id=project_id, limit=max_items)}
    ranked = []
    for record in scoped_memories(memory, scope, project_id):
        if record.sensitivity not in {"normal", "public"}:
            continue
        record_terms = _terms(record.content)
        similarity = max((len(record_terms & query_terms) / max(1, len(record_terms | query_terms))
                          for query_terms in terms), default=0)
        score = similarity + (2 if record.memory_id in hits else 0) + (4 if canonical_memory(record.content) in exact else 0)
        if not related_only or score >= 0.08:
            ranked.append((score, record.updated_at, record.memory_id, record))
    ranked.sort(key=lambda item: item[:3], reverse=True)
    selected, used = [], 0
    for _, _, _, record in ranked:
        size = len(record.content) + 160
        if used + size > max_chars:
            continue  # Never cut away a condition/exception from a candidate.
        selected.append(record)
        used += size
        if len(selected) >= max_items:
            break
    return selected
