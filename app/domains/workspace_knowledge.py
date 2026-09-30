"""Bounded, privacy-aware ingestion of configured local workspaces."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from app.domains.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeService,
    KnowledgeSourceInput,
)


class WorkspaceKnowledgeResult(BaseModel):
    workspace_root: str
    discovered_files: int = 0
    imported_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    imported_chunks: int = 0
    pruned_documents: int = 0
    bytes_read: int = 0
    errors: list[str] = Field(default_factory=list)


class WorkspaceKnowledgeIndexer:
    """Index text files beneath an explicitly configured workspace root."""

    def __init__(
        self,
        knowledge_service: KnowledgeService,
        allowed_workspace_roots: list[str | Path] | tuple[str | Path, ...],
        *,
        max_files: int = 500,
        max_file_bytes: int = 2_000_000,
        max_total_bytes: int = 20_000_000,
    ) -> None:
        if max_files < 1 or max_file_bytes < 1 or max_total_bytes < 1:
            raise ValueError("workspace indexing limits must be positive")
        self._knowledge = knowledge_service
        self._allowed_roots = tuple(self._canonical(path) for path in allowed_workspace_roots)
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes

    def index_workspace(self, path: str | Path) -> WorkspaceKnowledgeResult:
        """Import eligible files from one configured root; individual failures are reported."""
        requested_root = Path(path).expanduser().resolve(strict=True)
        if requested_root not in self._allowed_roots:
            raise ValueError("workspace path must be one of the configured allowed roots")
        if not requested_root.is_dir():
            raise ValueError("workspace path must be a directory")

        result = WorkspaceKnowledgeResult(workspace_root=str(requested_root))
        root_key = hashlib.sha256(str(requested_root).encode("utf-8")).hexdigest()[:16]
        candidates, truncated, scan_errors = self._candidate_files(requested_root, self._max_files)
        if scan_errors:
            result.errors.append(f"workspace traversal failed in {len(scan_errors)} directories")
        indexed_uris: set[str] = set()
        for candidate in candidates[: self._max_files]:
            result.discovered_files += 1
            try:
                canonical = candidate.resolve(strict=True)
                if not self._is_within(canonical, requested_root) or candidate.is_symlink():
                    result.skipped_files += 1
                    continue
                stat = canonical.stat()
                if not canonical.is_file() or stat.st_size > self._max_file_bytes:
                    result.skipped_files += 1
                    continue
                if result.bytes_read + stat.st_size > self._max_total_bytes:
                    result.skipped_files += 1
                    result.errors.append(f"{candidate.name}: aggregate byte limit reached")
                    continue
                raw = canonical.read_bytes()
                if len(raw) > self._max_file_bytes or result.bytes_read + len(raw) > self._max_total_bytes:
                    result.skipped_files += 1
                    result.errors.append(f"{candidate.name}: byte limit exceeded while reading")
                    continue
                result.bytes_read += len(raw)
                text = raw.decode("utf-8-sig")
                relative = canonical.relative_to(requested_root).as_posix()
                uri = f"workspace://{root_key}/{relative}"
                mime_type = "text/markdown" if canonical.suffix.lower() == ".md" else "text/plain"
                imported = self._knowledge.import_text_document(
                    KnowledgeDocumentInput(
                        source=KnowledgeSourceInput(
                            source_type="workspace_file",
                            display_name=requested_root.name,
                            uri=f"workspace://{root_key}",
                            sensitivity="personal",
                            remote_policy="redact",
                            access_scope={"workspace_paths": [str(requested_root)]},
                        ),
                        title=canonical.name,
                        uri=uri,
                        text=text,
                        mime_type=mime_type,
                        sensitivity="personal",
                        remote_policy="redact",
                        metadata={"relative_path": relative},
                    )
                )
                result.imported_files += 1
                result.imported_chunks += imported.imported_chunks
                indexed_uris.add(uri)
            except (OSError, UnicodeError, ValueError) as exc:
                result.failed_files += 1
                # Avoid copying file contents or OS paths into user-facing errors.
                result.errors.append(f"{candidate.name}: {type(exc).__name__}")
            except sqlite3.Error as exc:
                result.failed_files += 1
                result.errors.append(f"{candidate.name}: import failed ({type(exc).__name__})")

        excess = int(truncated)
        if excess:
            result.skipped_files += excess
            result.errors.append("file count limit reached; remaining files not indexed")
        if not result.errors and not result.failed_files:
            result.pruned_documents = self._knowledge.prune_workspace_documents(
                source_uri=f"workspace://{root_key}", keep_uris=indexed_uris,
            )
        return result

    @staticmethod
    def _canonical(path: str | Path) -> Path:
        return Path(path).expanduser().resolve(strict=True)

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @classmethod
    def _candidate_files(cls, root: Path, max_files: int) -> tuple[list[Path], bool, list[OSError]]:
        found: list[Path] = []
        errors: list[OSError] = []
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=errors.append):
            current = Path(directory)
            dirs[:] = sorted(name for name in dirs if not (current / name).is_symlink())
            for name in sorted(files):
                candidate = current / name
                if candidate.suffix.lower() in {".md", ".txt"}:
                    found.append(candidate)
                    if len(found) > max_files:
                        return found, True, errors
        return found, False, errors
