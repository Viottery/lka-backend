"""Adapters for public multi-hop retrieval datasets.

The adapter deliberately has no network or datasets-library dependency. Downloaded
records are normalized locally, making the benchmark reproducible and license-aware.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class PublicDocument:
    document_id: str
    title: str
    text: str
    source_type: str = "public_dataset"


@dataclass(frozen=True)
class PublicQuery:
    query_id: str
    question: str
    answer: str
    supporting_document_ids: list[str] = field(default_factory=list)
    supporting_titles: list[str] = field(default_factory=list)
    hop_count: int = 1
    forbidden_document_ids: list[str] = field(default_factory=list)
    dataset: str = "unknown"
    question_type: str = "unknown"
    split: str = "validation"


@dataclass
class PublicDataset:
    dataset: str
    documents: list[PublicDocument]
    queries: list[PublicQuery]

    def to_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for document in self.documents:
                handle.write(json.dumps({"kind": "document", **asdict(document)}, ensure_ascii=False) + "\n")
            for query in self.queries:
                handle.write(json.dumps({"kind": "query", **asdict(query)}, ensure_ascii=False) + "\n")


def load_normalized_jsonl(path: Path, *, dataset: str | None = None) -> PublicDataset:
    documents: list[PublicDocument] = []
    queries: list[PublicQuery] = []
    inferred_dataset = dataset or path.stem
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("kind") == "document":
            documents.append(PublicDocument(
                document_id=str(record["document_id"]), title=str(record["title"]), text=str(record["text"]),
                source_type=str(record.get("source_type") or "public_dataset"),
            ))
        elif record.get("kind") == "query":
            queries.append(PublicQuery(
                query_id=str(record["query_id"]), question=str(record["question"]), answer=str(record.get("answer") or ""),
                supporting_document_ids=[str(x) for x in record.get("supporting_document_ids", [])],
                supporting_titles=[str(x) for x in record.get("supporting_titles", [])],
                hop_count=max(1, int(record.get("hop_count") or 1)),
                forbidden_document_ids=[str(x) for x in record.get("forbidden_document_ids", [])],
                dataset=str(record.get("dataset") or inferred_dataset),
                question_type=str(record.get("question_type") or "unknown"),
                split=str(record.get("split") or "validation"),
            ))
        else:
            raise ValueError(f"Unknown normalized record at {path}:{line_number}")
    return PublicDataset(inferred_dataset, documents, queries)


def from_hotpotqa(records: Iterable[dict[str, Any]], *, corpus: dict[str, str]) -> PublicDataset:
    """Normalize HotpotQA-style records with ``context`` and ``supporting_facts``."""
    queries: list[PublicQuery] = []
    used_titles: set[str] = set()
    for index, record in enumerate(records):
        titles = [str(item[0]) for item in record.get("supporting_facts", [])]
        ids = [f"hotpot_{title}" for title in titles]
        used_titles.update(titles)
        queries.append(PublicQuery(str(record.get("_id") or f"hotpot_{index}"), str(record["question"]), str(record.get("answer") or ""), ids, titles, max(2, len(titles)), dataset="hotpotqa"))
    documents = [PublicDocument(f"hotpot_{title}", title, text) for title, text in corpus.items() if title in used_titles]
    return PublicDataset("hotpotqa", documents, queries)


def fixture_payload(dataset: PublicDataset) -> dict[str, Any]:
    """Convert normalized documents to the existing isolated knowledge fixture contract."""
    return {"documents": [{"source": {"source_type": document.source_type, "display_name": document.title, "uri": f"/public/{dataset.dataset}/{document.document_id}", "sensitivity": "public", "remote_policy": "allow"}, "title": document.title, "uri": f"/public/{dataset.dataset}/{document.document_id}", "mime_type": "text/plain", "text": document.text} for document in dataset.documents]}
