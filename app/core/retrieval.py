"""Retrieval provider protocol and local debug implementation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from app.core.context import RelatedFile, RelatedSnippet, TaskContext


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

        workspace_path = Path(workspace)
        if not workspace_path.exists():
            return RetrievalResult(
                provider=self.provider_name,
                status="completed",
                workspace=workspace,
                query=query,
                notes=["Workspace path does not exist; returned an empty retrieval result."],
            )

        related_files: list[RelatedFile] = []
        for root, _, files in os.walk(workspace_path):
            for filename in sorted(files):
                if len(related_files) >= 10:
                    break
                full_path = Path(root) / filename
                try:
                    relative_path = str(full_path.relative_to(workspace_path))
                except ValueError:
                    relative_path = str(full_path)
                role = "readme" if filename.lower().startswith("readme") else "workspace_file"
                related_files.append(
                    RelatedFile(
                        path=relative_path,
                        role=role,
                        reason="Local debug retrieval sampled workspace file metadata.",
                        confidence=0.4,
                    )
                )
            if len(related_files) >= 10:
                break

        related_snippets = [
            RelatedSnippet(
                source="runtime_debug",
                text=f"Workspace sample contains {len(related_files)} file metadata item(s).",
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
            notes=["Read-only metadata retrieval completed without semantic search."],
        )
