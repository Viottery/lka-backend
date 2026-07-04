"""Runtime orchestration for the current backend scaffold."""

from __future__ import annotations

import os
import sqlite3
from hashlib import sha1
from pathlib import Path

from app.api.schemas import (
    CapabilityItem,
    WorkspaceIndexResponse,
)
from app.core.config import Settings
from app.storage.db import connect, get_db_path, init_db


def _stable_id(prefix: str, text: str) -> str:
    digest = sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


class LocalKnowledgeAgentRuntime:
    """Lightweight runtime used by the current API layer."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = get_db_path(settings.data_dir)
        init_db(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def health(self) -> dict[str, str]:
        """Return a small health payload used by the `/health` route."""

        return {
            "status": "ok",
            "version": self.settings.version,
            "service": self.settings.app_name,
        }

    def _count_workspace_items(self, workspace_path: Path) -> tuple[int, int]:
        indexed_files = 0
        indexed_chunks = 0
        for _root, _, files in os.walk(workspace_path):
            indexed_files += len(files)
            indexed_chunks += len(files) * 4
        return indexed_files, indexed_chunks

    def _upsert_workspace(
        self,
        workspace_id: str,
        workspace: str,
        source_frontend: str | None,
        indexed_files: int,
        indexed_chunks: int,
    ) -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO workspaces(workspace_id, workspace_path, source_frontend, status, indexed_files, indexed_chunks)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    workspace_path=excluded.workspace_path,
                    source_frontend=excluded.source_frontend,
                    status=excluded.status,
                    indexed_files=excluded.indexed_files,
                    indexed_chunks=excluded.indexed_chunks
                """,
                (workspace_id, workspace, source_frontend, "completed", indexed_files, indexed_chunks),
            )
            conn.commit()
        finally:
            conn.close()

    def index_workspace(self, workspace: str, source_frontend: str | None = None, options: dict | None = None) -> WorkspaceIndexResponse:
        """Record a lightweight workspace index summary.

        The current implementation counts files only. The `options` payload is
        accepted for compatibility with the API contract, but is not yet used.
        """

        workspace_path = Path(workspace)
        workspace_id = _stable_id("ws", workspace)
        indexed_files = 0
        indexed_chunks = 0
        if workspace_path.exists():
            indexed_files, indexed_chunks = self._count_workspace_items(workspace_path)
        self._upsert_workspace(workspace_id, workspace, source_frontend, indexed_files, indexed_chunks)
        return WorkspaceIndexResponse(
            workspace_id=workspace_id,
            status="completed",
            indexed_files=indexed_files,
            indexed_chunks=indexed_chunks,
        )

    def list_capabilities(self) -> list[CapabilityItem]:
        """Return the currently advertised capability catalog."""

        return [
            CapabilityItem(name="summarize_folder", type="native_skill", risk="low", requires_confirmation=False),
            CapabilityItem(name="extract_tasks", type="native_skill", risk="low", requires_confirmation=False),
            CapabilityItem(name="organize_files", type="native_skill", risk="medium", requires_confirmation=True),
            CapabilityItem(name="claude_code", type="expert_tool", risk="medium", requires_confirmation=True),
            CapabilityItem(name="codex", type="expert_tool", risk="medium", requires_confirmation=True),
        ]
