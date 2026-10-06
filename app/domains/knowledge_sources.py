"""Protocols for live, non-mirrored knowledge sources."""

from __future__ import annotations

from typing import Any, Protocol


class KnowledgeSourceProvider(Protocol):
    """A bounded source adapter consumed by :class:`KnowledgeService`."""

    source_type: str

    def handles_id(self, value: str) -> bool: ...

    def list_sources(self, *, source_ids: list[str] | None, account_ids: list[str] | None) -> list[Any]: ...

    def search(self, *, query: str, limit: int, source_ids: list[str] | None,
               account_ids: list[str] | None, max_snippet_chars: int) -> list[Any]: ...

    def load_chunks(self, *, chunk_ids: list[str], max_chars_per_chunk: int, offset: int,
                    source_ids: list[str] | None, account_ids: list[str] | None) -> list[Any]: ...

    def load_document(self, *, document_id: str, include_text: bool, max_chars: int,
                      source_ids: list[str] | None, account_ids: list[str] | None) -> Any | None: ...
