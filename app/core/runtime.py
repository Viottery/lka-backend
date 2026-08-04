"""Runtime orchestration for the current backend scaffold."""

from __future__ import annotations

import sqlite3
from hashlib import sha1

from app.api.schemas import (
    CapabilityItem,
    WorkspaceIndexResponse,
)
from app.core.config import Settings
from app.core.context import ContextAssembler
from app.core.llm import MockLLMClient, TextLLMClient, build_text_llm_client
from app.core.mail import (
    MailAccountInput,
    MailImportResult,
    MailMatterList,
    MailMessageInput,
    MailProcessResult,
    MailSearchResult,
    MailService,
)
from app.core.outlook import (
    OutlookAuthCompleteResult,
    OutlookAuthStartResult,
    OutlookService,
    OutlookSyncResult,
)
from app.core.retrieval import LocalDebugRetrievalProvider
from app.core.runtime_loop import RuntimeDebugRun, RuntimeLoop
from app.core.tools import MockToolExecutor
from app.core.tracing import TraceRecorder
from app.platform import FilesystemScanner, PathResolver, ScanOptions, detect_platform
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
        self.platform = detect_platform(settings.platform)
        self.path_resolver = PathResolver(
            self.platform,
            workspace_roots=settings.parsed_workspace_roots(),
        )
        self.filesystem_scanner = FilesystemScanner(self.platform)
        self.mail_service = MailService(self._conn)
        self.local_app_config = settings.load_local_config()
        self.mail_llm_client: TextLLMClient | None = build_text_llm_client(
            self.local_app_config.llm
        )
        self.outlook_service = OutlookService(
            self._conn,
            self.mail_service,
            self.local_app_config,
        )
        self.debug_loop = RuntimeLoop(
            context_assembler=ContextAssembler(),
            retrieval_provider=LocalDebugRetrievalProvider(
                path_resolver=self.path_resolver,
                filesystem_scanner=self.filesystem_scanner,
                scan_options=self._scan_options(),
            ),
            llm_client=MockLLMClient(),
            tool_executor=MockToolExecutor(),
            trace_recorder=TraceRecorder(self._conn),
        )

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def health(self) -> dict[str, str]:
        """Return a small health payload used by the `/health` route."""

        return {
            "status": "ok",
            "version": self.settings.version,
            "service": self.settings.app_name,
        }

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

    def _scan_options(self, options: dict | None = None) -> ScanOptions:
        options = options or {}
        return ScanOptions(
            recursive=bool(options.get("recursive", True)),
            skip_hidden=bool(options.get("skip_hidden", self.settings.skip_hidden)),
            allow_symlinks=bool(options.get("allow_symlinks", self.settings.allow_symlinks)),
            max_files=int(options.get("max_files", self.settings.max_scan_files)),
            sample_limit=int(options.get("sample_limit", 10)),
        )

    def index_workspace(
        self,
        workspace: str,
        source_frontend: str | None = None,
        options: dict | None = None,
    ) -> WorkspaceIndexResponse:
        """Record a lightweight workspace index summary.

        The current implementation counts files only. The `options` payload can
        tune cross-platform scan behavior while preserving the API response.
        """

        resolved = self.path_resolver.resolve_workspace(
            workspace,
            source_frontend=source_frontend,
        )
        workspace_id = _stable_id("ws", resolved.normalized_path)
        if self.path_resolver.is_allowed_workspace(resolved):
            scan_result = self.filesystem_scanner.scan_workspace(
                resolved,
                options=self._scan_options(options),
            )
        else:
            scan_result = self.filesystem_scanner.scan_workspace(
                resolved,
                options=ScanOptions(max_files=0),
            )

        self._upsert_workspace(
            workspace_id,
            resolved.normalized_path,
            source_frontend,
            scan_result.indexed_files,
            scan_result.indexed_chunks,
        )
        return WorkspaceIndexResponse(
            workspace_id=workspace_id,
            status="completed",
            indexed_files=scan_result.indexed_files,
            indexed_chunks=scan_result.indexed_chunks,
        )

    def list_capabilities(self) -> list[CapabilityItem]:
        """Return the currently advertised capability catalog."""

        return [
            CapabilityItem(name="search_mail", type="native_skill", risk="low", requires_confirmation=False),
            CapabilityItem(name="process_mail", type="native_skill", risk="low", requires_confirmation=False),
            CapabilityItem(name="summarize_folder", type="native_skill", risk="low", requires_confirmation=False),
            CapabilityItem(name="extract_tasks", type="native_skill", risk="low", requires_confirmation=False),
            CapabilityItem(name="organize_files", type="native_skill", risk="medium", requires_confirmation=True),
            CapabilityItem(name="claude_code", type="expert_tool", risk="medium", requires_confirmation=True),
            CapabilityItem(name="codex", type="expert_tool", risk="medium", requires_confirmation=True),
        ]

    def run_debug(
        self,
        *,
        session_id: str,
        workspace: str | None,
        user_input: str,
    ) -> RuntimeDebugRun:
        """Run the fixed-stage debug runtime chain."""

        return self.debug_loop.run_debug(
            session_id=session_id,
            workspace=workspace,
            user_input=user_input,
        )

    def import_mail(
        self,
        *,
        account: MailAccountInput,
        messages: list[MailMessageInput],
    ) -> MailImportResult:
        """Persist locally imported mail messages."""

        return self.mail_service.import_messages(account=account, messages=messages)

    def search_mail(self, *, query: str, limit: int = 10) -> MailSearchResult:
        """Search locally persisted mail with SQLite FTS."""

        return self.mail_service.search_messages(query=query, limit=limit)

    def process_mail(self, *, query: str | None = None, limit: int = 10) -> MailProcessResult:
        """Run the first deterministic mail matter extraction loop."""

        return self.mail_service.process_messages(
            query=query,
            limit=limit,
            llm_client=self.mail_llm_client,
        )

    def list_mail_matters(self, *, limit: int = 50) -> MailMatterList:
        """Return locally extracted mail matters."""

        return self.mail_service.list_matters(limit=limit)

    def start_outlook_auth(self) -> OutlookAuthStartResult:
        """Start Microsoft Graph Device Code Flow for Outlook read-only sync."""

        return self.outlook_service.start_device_auth()

    def complete_outlook_auth(self, *, device_code: str) -> OutlookAuthCompleteResult:
        """Complete one Device Code Flow polling attempt and persist tokens locally."""

        return self.outlook_service.complete_device_auth(device_code=device_code)

    def sync_outlook_mail(
        self,
        *,
        folder: str | None = None,
        limit: int = 25,
        max_pages: int = 1,
    ) -> OutlookSyncResult:
        """Read Outlook mail through Microsoft Graph and persist it locally."""

        return self.outlook_service.sync_messages(
            folder=folder,
            limit=limit,
            max_pages=max_pages,
        )
