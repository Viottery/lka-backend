"""Retrieval provider protocol and local debug implementation."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from app.core.context import RelatedFile, RelatedSnippet, TaskContext
from app.platform import FilesystemScanner, PathResolver, ScanOptions


class RetrievalResult(BaseModel):
    provider: str
    status: str
    workspace: str | None = None
    query: str
    related_files: list[RelatedFile] = Field(default_factory=list)
    related_snippets: list[RelatedSnippet] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RetrievalProvider(Protocol):
    def retrieve(
        self,
        *,
        workspace: str | None,
        query: str,
        task_context: TaskContext,
    ) -> RetrievalResult:
        ...


class LocalDebugRetrievalProvider:
    """Return deterministic, read-only workspace metadata for debug runs."""

    provider_name = "local_debug_retrieval"

    def __init__(
        self,
        *,
        path_resolver: PathResolver,
        filesystem_scanner: FilesystemScanner,
        scan_options: ScanOptions,
    ) -> None:
        self.path_resolver = path_resolver
        self.filesystem_scanner = filesystem_scanner
        self.scan_options = scan_options

    def retrieve(
        self,
        *,
        workspace: str | None,
        query: str,
        task_context: TaskContext,
    ) -> RetrievalResult:
        if workspace is None:
            return RetrievalResult(
                provider=self.provider_name,
                status="completed",
                workspace=None,
                query=query,
                notes=["No workspace was provided; returned an empty retrieval result."],
            )

        resolved = self.path_resolver.resolve_workspace(workspace)
        if not resolved.exists:
            return RetrievalResult(
                provider=self.provider_name,
                status="completed",
                workspace=workspace,
                query=query,
                notes=["Workspace path does not exist; returned an empty retrieval result."],
            )

        scan_result = self.filesystem_scanner.scan_workspace(
            resolved,
            options=self.scan_options,
        )
        related_files = [
            RelatedFile(
                path=file.relative_path,
                role=file.role,
                reason="Local debug retrieval sampled workspace file metadata.",
                confidence=0.4,
            )
            for file in scan_result.sampled_files
        ]

        related_snippets = [
            RelatedSnippet(
                source="runtime_debug",
                text=(
                    f"Workspace sample contains {len(related_files)} file metadata item(s) "
                    f"on {resolved.platform}."
                ),
                reason="Debug retrieval reports metadata only; text extraction is deferred to 2.3.",
            )
        ]
        return RetrievalResult(
            provider=self.provider_name,
            status="completed",
            workspace=workspace,
            query=query,
            related_files=related_files,
            related_snippets=related_snippets,
            notes=[
                "Read-only metadata retrieval completed without semantic search.",
                f"Resolved workspace path: {resolved.normalized_path}",
            ],
        )
