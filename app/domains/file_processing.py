"""Explicit file-type preferences and bounded local knowledge extraction."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import PurePath
from typing import Any

from app.domains.documents import DocumentService
from app.integrations.document_conversion import convert_document_snapshot

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_CHARS = 2_000_000


@dataclass(frozen=True)
class FileTypePolicy:
    extensions: tuple[str, ...]
    preferred_processor: str
    index_supported: bool
    source_mime_type: str
    notes: str


_POLICIES = (
    FileTypePolicy((".md",), "native_text", True, "text/markdown", "UTF-8 text; original formatting retained."),
    FileTypePolicy((".txt", ".text"), "native_text", True, "text/plain", "UTF-8 text."),
    FileTypePolicy((".pdf",), "native_pdf", True, "application/pdf", "Text layer only; no OCR or visual fidelity guarantee."),
    FileTypePolicy((".docx",), "markitdown", True, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Local Markdown conversion; layout and images may be lost."),
    FileTypePolicy((".pptx",), "markitdown", True, "application/vnd.openxmlformats-officedocument.presentationml.presentation", "Local Markdown conversion; visual slide layout may be lost."),
    FileTypePolicy((".xlsx",), "markitdown", True, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Cached values only; formulas, images and merged-cell layout may be omitted; hidden sheets are included."),
    FileTypePolicy((".xls",), "markitdown", True, "application/vnd.ms-excel", "Table values only; formulas, images and workbook formatting are not preserved; hidden sheets may be included."),
    FileTypePolicy((".html", ".htm"), "markitdown", True, "text/html", "Local HTML text; external resources are not fetched."),
    FileTypePolicy((".epub",), "markitdown", True, "application/epub+zip", "Local text extraction; illustrations and layout may be lost."),
    FileTypePolicy((".csv",), "markitdown", True, "text/csv", "Local text conversion."),
    FileTypePolicy((".json",), "markitdown", True, "application/json", "Local text conversion."),
    FileTypePolicy((".xml",), "markitdown", True, "application/xml", "Local text conversion."),
    FileTypePolicy((".msg",), "markitdown", True, "application/vnd.ms-outlook", "Local message conversion; attachments may not be represented."),
    FileTypePolicy((".png", ".jpg", ".jpeg", ".webp"), "vision", False, "image/*", "Requires explicit visual reading; no automatic OCR or indexing."),
    FileTypePolicy((".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".avi", ".webm"), "transcription", False, "audio/video", "Requires an explicitly configured transcription workflow."),
    FileTypePolicy((".zip",), "archive_members", False, "application/zip", "Archive members must be selected and processed individually."),
    FileTypePolicy((".doc", ".ppt"), "unsupported", False, "application/octet-stream", "Convert legacy Office files to supported formats first."),
)


def file_type_policies() -> list[dict[str, Any]]:
    return [{**asdict(policy), "extensions": list(policy.extensions)} for policy in _POLICIES]


def get_file_type_policy(filename: str) -> FileTypePolicy | None:
    extension = PurePath(filename).suffix.lower()
    return next((policy for policy in _POLICIES if extension in policy.extensions), None)


def supported_index_extensions() -> frozenset[str]:
    return frozenset(ext for policy in _POLICIES if policy.index_supported for ext in policy.extensions)


class FileProcessingError(ValueError):
    """Safe processing failure with no source contents or private paths."""


@dataclass(frozen=True)
class ProcessedFile:
    text: str
    mime_type: str
    metadata: dict[str, Any]


class FileProcessor:
    def __init__(self, document_service: DocumentService | None = None) -> None:
        self._documents = document_service or DocumentService()

    def process_bytes(
        self, filename: str, raw: bytes, *, source_max_bytes: int | None = None,
    ) -> ProcessedFile:
        policy = get_file_type_policy(filename)
        if policy is None or not policy.index_supported:
            raise FileProcessingError("File type is unsupported for local indexing")
        source_max_bytes = MAX_SOURCE_BYTES if source_max_bytes is None else source_max_bytes
        if len(raw) > source_max_bytes:
            raise FileProcessingError("Source exceeds the configured processing limit")
        extraction: dict[str, Any] = {
            "processor": policy.preferred_processor,
            "original_mime_type": policy.source_mime_type,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "warnings": [] if policy.preferred_processor == "native_text" else [policy.notes],
            "fidelity_verified": False,
        }
        try:
            if policy.preferred_processor == "native_text":
                text = raw.decode("utf-8-sig")
                mime_type = policy.source_mime_type
                extraction["processor_version"] = "1"
            elif policy.preferred_processor == "native_pdf":
                result = self._documents.extract_snapshot(
                    raw, max_chars=MAX_EXTRACTED_CHARS, source_max_bytes=source_max_bytes,
                )
                text = result.pop("text")
                extraction.update(result)
                extraction["processor_version"] = version("pypdf")
                mime_type = "text/markdown"
            else:
                text = convert_document_snapshot(raw, PurePath(filename).suffix.lower())
                extraction["processor_version"] = version("markitdown")
                mime_type = "text/markdown"
            text = text.strip()
            if not text:
                raise FileProcessingError("Source has no extractable text")
            if len(text) > MAX_EXTRACTED_CHARS:
                raise FileProcessingError("Extracted text exceeds the 2000000-character limit")
            return ProcessedFile(text=text, mime_type=mime_type, metadata={"extraction": extraction})
        except FileProcessingError:
            raise
        except Exception as exc:
            raise FileProcessingError(f"Local {policy.preferred_processor} extraction failed ({type(exc).__name__})") from exc
