"""Runtime orchestration for the current backend scaffold."""

from __future__ import annotations

import sqlite3
import threading
from hashlib import sha1
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from app.api.schemas import (
    CapabilityItem,
    WorkspaceIndexResponse,
)
from app.core.agent_graph import AgentGraphRunner
from app.core.agent_runner import AgentTurnRunner
from app.core.agent_runs import AgentRunRecord, InMemoryAgentRunManager
from app.core.agent_turn import AgentTurnLoop, AgentTurnResult
from app.core.config import Settings
from app.core.context import ContextAssembler
from app.core.llm import LLMResponseMode, MockLLMClient, build_text_llm_client
from app.core.retrieval import LocalDebugRetrievalProvider
from app.core.runtime_loop import RuntimeDebugRun, RuntimeLoop
from app.core.sessions import (
    AgentSessionDetail,
    AgentSessionList,
    AgentSessionMessage,
    SessionRole,
    SessionService,
    SessionWorkspace,
)
from app.core.tools import MockToolExecutor, ToolExecutor, ToolRegistry
from app.core.tracing import TraceRecorder
from app.domains.knowledge import (
    KnowledgeChunkLoadResult,
    KnowledgeDocumentInput,
    KnowledgeDocumentRecord,
    KnowledgeImportResult,
    KnowledgeSearchResult,
    KnowledgeService,
)
from app.domains.mail import (
    MailAccountInput,
    MailImportResult,
    MailMatterList,
    MailMessageInput,
    MailSearchResult,
    MailService,
)
from app.domains.mail_knowledge import MailKnowledgeMirror, MailKnowledgeMirrorResult
from app.domains.matters import (
    MatterCreateInput,
    MatterList,
    MatterRecord,
    MatterSearchResult,
    MatterService,
    MatterSourceLinkInput,
    MatterUpdateInput,
)
from app.integrations.local_semantic import build_local_semantic_components
from app.integrations.outlook import (
    OutlookAuthCompleteResult,
    OutlookAuthStartResult,
    OutlookService,
    OutlookServiceError,
    OutlookSyncResult,
)
from app.platform import FilesystemScanner, PathResolver, ScanOptions, detect_platform
from app.storage.db import connect, get_db_path, init_db
from app.tool_packages.bash import (
    BASH_PACKAGE,
    BashAccessPolicy,
    BashInterruptSessionTool,
    BashListSessionsTool,
    BashReadSessionTool,
    BashRunTool,
    BashSessionManager,
    BashTerminateSessionTool,
    BashWriteSessionTool,
)
from app.tool_packages.filesystem import (
    FILESYSTEM_PACKAGE,
    EditFileTool,
    FileAccessPolicy,
    ReadFileTool,
)
from app.tool_packages.knowledge import (
    KNOWLEDGE_PACKAGE,
    LoadKnowledgeChunksTool,
    LoadKnowledgeDocumentTool,
    SearchKnowledgeTool,
)
from app.tool_packages.mail import (
    MAIL_PACKAGE,
    LoadMailMessagesTool,
    SearchMailTool,
    SyncMailTool,
)
from app.tool_packages.matter import (
    MATTER_PACKAGE,
    CreateManyMattersTool,
    CreateMatterTool,
    LinkMatterSourceTool,
    ListMattersTool,
    SearchMattersTool,
    UpdateMatterTool,
)


def _stable_id(prefix: str, text: str) -> str:
    digest = sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _is_absolute_path_for_platform(path: str, platform: str) -> bool:
    if not path.strip():
        return False
    if platform == "windows":
        return PureWindowsPath(path).is_absolute()
    return PurePosixPath(path).is_absolute()


def _normalized_workspace_path(path: str, platform: str) -> str:
    if platform == "windows":
        return PureWindowsPath(path).as_posix()
    return PurePosixPath(path).as_posix()


class LocalKnowledgeAgentRuntime:
    """Lightweight runtime used by the current API layer."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = get_db_path(settings.data_dir)
        init_db(self.db_path)
        self.local_app_config = settings.load_local_config()
        self.platform = detect_platform(settings.platform)
        self.path_resolver = PathResolver(
            self.platform,
            workspace_roots=settings.parsed_workspace_roots(),
            wsl_windows_mount_root=settings.wsl_windows_mount_root,
        )
        self.filesystem_scanner = FilesystemScanner(self.platform)
        self.session_service = SessionService(self._conn)
        self.mail_service = MailService(self._conn)
        self.matter_service = MatterService(self._conn)
        embedding_config = self.local_app_config.embedding
        embedding_provider, semantic_index = build_local_semantic_components(
            conn_factory=self._conn,
            enabled=embedding_config.enabled,
            provider_name=embedding_config.provider,
            index_provider_name=embedding_config.index_provider,
            model_name=embedding_config.model_name,
            dimensions=embedding_config.dimensions,
            cache_dir=str(embedding_config.cache_dir),
            batch_size=embedding_config.batch_size,
            query_prefix=embedding_config.query_prefix,
            normalize_embeddings=embedding_config.normalize_embeddings,
            local_files_only=embedding_config.local_files_only,
        )
        self.knowledge_service = KnowledgeService(
            self._conn,
            embedding_provider=embedding_provider,
            semantic_index=semantic_index,
            default_retrieval_mode=embedding_config.default_retrieval_mode,
            auto_index_on_import=embedding_config.auto_index_on_import,
        )
        self.mail_knowledge_mirror = MailKnowledgeMirror(
            mail_service=self.mail_service,
            knowledge_service=self.knowledge_service,
        )
        self.outlook_service = OutlookService(
            self._conn,
            self.mail_service,
            self.local_app_config,
        )
        self._mail_sync_lock = threading.Lock()
        self._mail_sync_stop_event = threading.Event()
        self._mail_sync_thread: threading.Thread | None = None
        self.last_mail_sync_result: dict[str, Any] | None = None
        self.tool_registry = ToolRegistry()
        self.tool_registry.register_package(MAIL_PACKAGE)
        self.tool_registry.register_package(MATTER_PACKAGE)
        self.tool_registry.register_package(KNOWLEDGE_PACKAGE)
        self.tool_registry.register_package(FILESYSTEM_PACKAGE)
        self.tool_registry.register_package(BASH_PACKAGE)
        self.tool_registry.register_tool(SearchMailTool(self.mail_knowledge_mirror))
        self.tool_registry.register_tool(LoadMailMessagesTool(self.mail_service))
        self.tool_registry.register_tool(SyncMailTool(self.sync_outlook_mail))
        self.tool_registry.register_tool(CreateMatterTool(self.matter_service))
        self.tool_registry.register_tool(CreateManyMattersTool(self.matter_service))
        self.tool_registry.register_tool(SearchMattersTool(self.matter_service))
        self.tool_registry.register_tool(ListMattersTool(self.matter_service))
        self.tool_registry.register_tool(UpdateMatterTool(self.matter_service))
        self.tool_registry.register_tool(LinkMatterSourceTool(self.matter_service))
        self.tool_registry.register_tool(SearchKnowledgeTool(self.knowledge_service))
        self.tool_registry.register_tool(LoadKnowledgeChunksTool(self.knowledge_service))
        self.tool_registry.register_tool(LoadKnowledgeDocumentTool(self.knowledge_service))
        file_policy = FileAccessPolicy.from_workspace_roots(
            self.settings.parsed_workspace_roots()
        )
        self.tool_registry.register_tool(ReadFileTool(file_policy))
        self.tool_registry.register_tool(EditFileTool(file_policy))
        bash_policy = BashAccessPolicy.from_workspace_roots(
            self.settings.parsed_workspace_roots()
        )
        self.bash_session_manager = BashSessionManager()
        self.tool_registry.register_tool(
            BashRunTool(
                policy=bash_policy,
                session_manager=self.bash_session_manager,
            )
        )
        self.tool_registry.register_tool(BashListSessionsTool(self.bash_session_manager))
        self.tool_registry.register_tool(BashReadSessionTool(self.bash_session_manager))
        self.tool_registry.register_tool(BashWriteSessionTool(self.bash_session_manager))
        self.tool_registry.register_tool(
            BashInterruptSessionTool(self.bash_session_manager)
        )
        self.tool_registry.register_tool(
            BashTerminateSessionTool(self.bash_session_manager)
        )
        self.tool_executor = ToolExecutor(self.tool_registry)
        self.agent_run_manager = InMemoryAgentRunManager()
        self.agent_llm_client = build_text_llm_client(self.local_app_config.llm)
        self.agent_turn_loop = AgentTurnLoop(
            session_service=self.session_service,
            tool_executor=self.tool_executor,
            llm_client=self.agent_llm_client,
            log_dir=self.settings.data_dir / "agent_logs",
            max_decision_steps=self.local_app_config.agent.max_decision_steps,
            run_manager=self.agent_run_manager,
            safety_review_mode=self.local_app_config.safety.tool_review_mode,
            safety_manual_wait_poll_seconds=(
                self.local_app_config.safety.manual_wait_poll_seconds
            ),
        )
        self.agent_turn_runner: AgentTurnRunner = self.agent_turn_loop
        if self.local_app_config.agent.orchestrator == "langgraph":
            self.agent_turn_runner = AgentGraphRunner(self.agent_turn_loop)
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

    def start(self) -> None:
        """Start runtime services that should run while the API process is alive."""

        self._run_startup_mail_sync()
        self._start_background_mail_sync()

    def stop(self) -> None:
        """Stop runtime background services."""

        self._mail_sync_stop_event.set()
        if self._mail_sync_thread and self._mail_sync_thread.is_alive():
            self._mail_sync_thread.join(timeout=5)
        self._mail_sync_thread = None

    def _run_startup_mail_sync(self) -> None:
        config = self.local_app_config.mail.outlook
        if not config.enabled or not config.startup_sync_enabled:
            self.last_mail_sync_result = {
                "provider": "outlook",
                "status": "skipped",
                "trigger": "startup",
                "reason": "Outlook startup sync is disabled.",
            }
            return
        self._sync_outlook_mail_safely(trigger="startup")

    def _start_background_mail_sync(self) -> None:
        config = self.local_app_config.mail.outlook
        if (
            not config.enabled
            or not config.background_sync_enabled
            or config.sync_interval_seconds <= 0
            or (self._mail_sync_thread is not None and self._mail_sync_thread.is_alive())
        ):
            return

        self._mail_sync_stop_event.clear()
        self._mail_sync_thread = threading.Thread(
            target=self._background_mail_sync_loop,
            name="lka-outlook-sync",
            daemon=True,
        )
        self._mail_sync_thread.start()

    def _background_mail_sync_loop(self) -> None:
        config = self.local_app_config.mail.outlook
        while not self._mail_sync_stop_event.wait(config.sync_interval_seconds):
            self._sync_outlook_mail_safely(trigger="background")

    def _sync_outlook_mail_safely(self, *, trigger: str) -> None:
        config = self.local_app_config.mail.outlook
        try:
            self.sync_outlook_mail(
                folder=config.sync_folder,
                limit=config.sync_limit,
                max_pages=config.sync_max_pages,
                trigger=trigger,
            )
        except OutlookServiceError as exc:
            self.last_mail_sync_result = {
                "provider": "outlook",
                "status": "failed",
                "trigger": trigger,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        except Exception as exc:
            self.last_mail_sync_result = {
                "provider": "outlook",
                "status": "failed",
                "trigger": trigger,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

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

        package_capabilities = [
            CapabilityItem(
                name=package.name,
                type="tool_package",
                risk=package.risk,
                requires_confirmation=any(
                    tool.requires_confirmation or tool.read_only is not True
                    for tool in self.tool_registry.list_tools(package=package.name)
                ),
                read_only=all(
                    tool.read_only is True
                    for tool in self.tool_registry.list_tools(package=package.name)
                ),
            )
            for package in self.tool_registry.list_packages()
        ]
        return package_capabilities + [
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

    def run_agent_turn(
        self,
        *,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        """Run the minimal general agent turn loop."""

        return self.agent_turn_runner.run(
            session_id=session_id,
            user_input=user_input,
            llm_client_name=llm_client_name,
            llm_model=llm_model,
            llm_response_mode=llm_response_mode,
            existing_run_id=existing_run_id,
        )

    async def run_agent_turn_async(
        self,
        *,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        """Run one agent turn without blocking the event loop."""

        return await self.agent_turn_runner.run_async(
            session_id=session_id,
            user_input=user_input,
            llm_client_name=llm_client_name,
            llm_model=llm_model,
            llm_response_mode=llm_response_mode,
            existing_run_id=existing_run_id,
        )

    def create_agent_run(
        self,
        *,
        session_id: str | None,
        user_input: str,
        parent_run_id: str | None = None,
    ) -> AgentRunRecord:
        """Create a queued run before an HTTP stream starts consuming events."""

        return self.agent_turn_runner.create_run_for_turn(
            session_id=session_id,
            user_input=user_input,
            parent_run_id=parent_run_id,
        )

    def import_mail(
        self,
        *,
        account: MailAccountInput,
        messages: list[MailMessageInput],
    ) -> MailImportResult:
        """Persist locally imported mail messages."""

        result = self.mail_service.import_messages(account=account, messages=messages)
        self.mail_knowledge_mirror.sync(
            account_id=result.account_id,
            external_ids=[message.external_id for message in messages],
        )
        return result

    def search_mail(
        self,
        *,
        query: str,
        limit: int = 10,
        mode: str | None = None,
        order_by: Literal["relevance", "source_time_desc"] = "relevance",
        max_snippet_chars: int = 420,
    ) -> MailSearchResult:
        """Search local mail evidence through the knowledge retrieval layer."""

        return self.mail_knowledge_mirror.search(
            query=query,
            limit=limit,
            mode=mode,
            order_by=order_by,
            max_snippet_chars=max_snippet_chars,
        )

    def import_knowledge_document(
        self,
        *,
        payload: KnowledgeDocumentInput,
    ) -> KnowledgeImportResult:
        """Persist one local knowledge document and its chunks."""

        return self.knowledge_service.import_text_document(payload)

    def search_knowledge(
        self,
        *,
        query: str,
        limit: int = 10,
        source_types: list[str] | None = None,
        mode: str | None = None,
    ) -> KnowledgeSearchResult:
        """Search local source-agnostic knowledge chunks."""

        return self.knowledge_service.search(
            query=query,
            limit=limit,
            source_types=source_types,
            mode=mode,
        )

    def sync_knowledge_semantic_index(self, *, allow_model_download: bool = False) -> Any:
        """Embed eligible local chunks and synchronize the configured local semantic index."""

        return self.knowledge_service.sync_semantic_index(
            allow_model_download=allow_model_download,
        )

    def load_knowledge_chunks(
        self,
        *,
        chunk_ids: list[str],
        max_chars_per_chunk: int = 420,
    ) -> KnowledgeChunkLoadResult:
        """Load selected privacy-filtered local knowledge chunks."""

        return self.knowledge_service.load_chunks(
            chunk_ids=chunk_ids,
            max_chars_per_chunk=max_chars_per_chunk,
        )

    def load_knowledge_document(
        self,
        *,
        document_id: str,
        include_text: bool = False,
        max_chars: int = 12000,
    ) -> KnowledgeDocumentRecord:
        """Load one local knowledge document record."""

        return self.knowledge_service.load_document(
            document_id=document_id,
            include_text=include_text,
            max_chars=max_chars,
        )

    def create_session(
        self,
        *,
        title: str | None = None,
        metadata: dict | None = None,
        initial_message: str | None = None,
    ) -> AgentSessionDetail:
        """Create a persistent agent session for parallel/multi-turn work."""

        return self.session_service.create_session(
            title=title,
            metadata=metadata,
            initial_message=initial_message,
        )

    def list_sessions(self, *, limit: int = 50) -> AgentSessionList:
        """Return recent agent sessions for frontend session switching."""

        return self.session_service.list_sessions(limit=limit)

    def get_session(self, *, session_id: str) -> AgentSessionDetail:
        """Return one session with its ordered message history."""

        return self.session_service.get_session(session_id=session_id)

    def set_session_workspace(
        self,
        *,
        session_id: str,
        path: str,
        platform: str,
    ) -> SessionWorkspace:
        """Persist a backend-accessible workspace root for one Agent session."""

        normalized_platform = platform.strip().lower()
        if normalized_platform not in {"linux", "windows", "macos"}:
            raise ValueError("platform must be one of: linux, windows, macos")
        is_windows_via_wsl = (
            self.platform.name == "linux" and normalized_platform == "windows"
        )
        if normalized_platform != self.platform.name and not is_windows_via_wsl:
            raise ValueError(
                "workspace platform does not match this backend: "
                f"frontend selected {normalized_platform}, backend runs on {self.platform.name}"
            )
        if not _is_absolute_path_for_platform(path, normalized_platform):
            raise ValueError(f"workspace path must be an absolute {normalized_platform} path")

        if is_windows_via_wsl:
            resolved_workspace = self.path_resolver.resolve_windows_workspace_from_wsl(path)
        else:
            resolved = Path(path).expanduser().resolve(strict=False)
            resolved_workspace = self.path_resolver.resolve_workspace(
                str(resolved), source_frontend=f"{normalized_platform}-native"
            )
        if not resolved_workspace.exists or not resolved_workspace.resolved_path.is_dir():
            raise ValueError(f"workspace directory does not exist: {resolved_workspace.resolved_path}")
        if not self.path_resolver.is_allowed_workspace(resolved_workspace):
            raise ValueError("workspace path is outside configured LKA_WORKSPACE_ROOTS")
        workspace = SessionWorkspace(
            path=_normalized_workspace_path(path, normalized_platform),
            platform=normalized_platform,
            backend_path=resolved_workspace.normalized_path,
        )
        self.session_service.set_workspace(session_id=session_id, workspace=workspace)
        return workspace

    def append_session_message(
        self,
        *,
        session_id: str,
        role: SessionRole,
        content: str,
        payload: dict | None = None,
    ) -> AgentSessionMessage:
        """Append a deterministic message to a persistent session."""

        return self.session_service.append_message(
            session_id=session_id,
            role=role,
            content=content,
            payload=payload,
        )

    def list_mail_matters(self, *, limit: int = 50) -> MailMatterList:
        """Return locally extracted mail matters."""

        return self.mail_service.list_matters(limit=limit)

    def create_matter(self, *, payload: MatterCreateInput) -> MatterRecord:
        """Persist an independent local matter."""

        return self.matter_service.create_matter(payload)

    def search_matters(self, *, query: str, limit: int = 10) -> MatterSearchResult:
        """Search independent local matters."""

        return self.matter_service.search_matters(query=query, limit=limit)

    def list_matters(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> MatterList:
        """List independent local matters."""

        return self.matter_service.list_matters(limit=limit, status=status)

    def update_matter(
        self,
        *,
        matter_id: str,
        payload: MatterUpdateInput,
    ) -> MatterRecord:
        """Update an independent local matter."""

        return self.matter_service.update_matter(matter_id=matter_id, payload=payload)

    def link_matter_source(
        self,
        *,
        matter_id: str,
        source_link: MatterSourceLinkInput,
    ) -> MatterRecord:
        """Link an independent matter to a source object."""

        return self.matter_service.link_source(
            matter_id=matter_id,
            source_link=source_link,
        )

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
        trigger: str = "api",
    ) -> OutlookSyncResult:
        """Read Outlook mail through Microsoft Graph and persist it locally."""

        with self._mail_sync_lock:
            result = self.outlook_service.sync_messages(
                folder=folder,
                limit=limit,
                max_pages=max_pages,
            )
            self.mail_knowledge_mirror.sync(account_id=result.account_id)
            payload = result.model_dump(mode="json")
            payload["provider"] = "outlook"
            payload["trigger"] = trigger
            self.last_mail_sync_result = payload
            return result

    def sync_mail_knowledge_mirror(
        self,
        *,
        account_id: str | None = None,
    ) -> MailKnowledgeMirrorResult:
        """Project persisted local mail evidence into the generic knowledge store."""

        return self.mail_knowledge_mirror.sync(account_id=account_id)
