"""Runtime orchestration for the current backend scaffold."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from hashlib import sha1
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from uuid import uuid4

from app.api.schemas import (
    CapabilityItem,
    WorkspaceIndexResponse,
)
from app.core.agent_checkpoints import create_sqlite_checkpoint_runtime
from app.core.agent_executors import AgentDefinition, AgentExecutorRegistry, MockWorkflowExecutor
from app.core.agent_graph import AgentGraphRunner
from app.core.agent_runner import AgentTurnRunner
from app.core.agent_runs import AgentRunRecord, AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import AgentTurnLoop, AgentTurnResult
from app.core.child_agent import ChildAgentExecutor
from app.core.codex_app_server import CodexAppServerClient
from app.core.codex_expert import (
    CodexAppServerExpertExecutor,
    staged_file_change_approval_guard,
)
from app.core.codex_trace import CodexTraceEvent, CodexTraceJournal
from app.core.codex_transport import CodexSubprocessTransport
from app.core.codex_workspace import CodexWorkspaceFactory
from app.core.config import Settings
from app.core.context import ContextAssembler
from app.core.context_driver import ContextViews, EvidenceCandidate
from app.core.llm import LLMResponseMode, MockLLMClient, build_text_llm_client
from app.core.safety import SafetyReviewMode
from app.core.multi_agent import (
    GENERAL_AGENT_ID,
    ContextSnapshot,
    EvidenceRef,
    ForkPolicy,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResult,
)
from app.core.multi_agent_scheduler import (
    MultiAgentScheduler,
    MultiAgentScheduleResult,
    SchedulerContext,
)
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
from app.domains.workspace_knowledge import WorkspaceKnowledgeIndexer
from app.integrations.local_reranker import FastEmbedCrossEncoderReranker
from app.integrations.local_semantic import build_local_semantic_components
from app.integrations.outlook import (
    OutlookAuthCompleteResult,
    OutlookAuthStartResult,
    OutlookConfigError,
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
    ListKnowledgeSourcesTool,
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
        if (
            self.local_app_config.agent.multi_agent_planning_enabled
            and self.local_app_config.agent.orchestrator != "langgraph"
        ):
            raise ValueError(
                "Multi-Agent scheduling requires agent.orchestrator = 'langgraph' "
                "so parent runs can checkpoint and resume around child approvals."
            )
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
        reranker_config = self.local_app_config.reranker
        reranker = (
            FastEmbedCrossEncoderReranker(
                model_name=reranker_config.model_name,
                cache_dir=str(reranker_config.cache_dir),
                batch_size=reranker_config.batch_size,
                max_candidates=reranker_config.max_candidates,
                max_query_chars=reranker_config.max_query_chars,
                max_candidate_chars=reranker_config.max_candidate_chars,
                max_concurrent_inferences=reranker_config.max_concurrent_inferences,
                queue_timeout_ms=reranker_config.queue_timeout_ms,
                local_files_only=reranker_config.local_files_only,
            )
            if reranker_config.enabled else None
        )
        self.knowledge_service = KnowledgeService(
            self._conn,
            embedding_provider=embedding_provider,
            semantic_index=semantic_index,
            default_retrieval_mode=embedding_config.default_retrieval_mode,
            auto_index_on_import=embedding_config.auto_index_on_import,
            reranker=reranker,
            workspace_roots=tuple(settings.parsed_workspace_roots()),
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
        self.tool_registry.register_tool(SearchKnowledgeTool(
            self.knowledge_service, self.local_app_config.query_rewrite,
        ))
        self.tool_registry.register_tool(ListKnowledgeSourcesTool(self.knowledge_service))
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
        self.agent_run_store = SqliteAgentRunStore(self.db_path)
        self.agent_run_manager = InMemoryAgentRunManager(
            durable_store=self.agent_run_store
        )
        # A new Runtime cannot answer an app-server request held by a process
        # owned by the previous Runtime. Never leave such approvals actionable
        # or pretend a restarted Codex turn can safely resume its side effects.
        stale_codex_journal: CodexTraceJournal | None = None
        for stale_review in self.agent_run_manager.list_pending_safety_reviews():
            if not stale_review.tool_name.startswith("codex."):
                continue
            self.agent_run_manager.fail_child_run(
                stale_review.run_id,
                error_type="codex_process_lost",
                error="Codex approval was orphaned by Runtime restart; retry requires a new isolated attempt.",
            )
            if stale_codex_journal is None:
                stale_codex_journal = CodexTraceJournal(self.db_path)
            stale_codex_journal.mark_gap(
                stale_review.run_id,
                reason="codex_runtime_restart_during_approval",
            )
        self.tool_executor.run_manager = self.agent_run_manager
        fork_policy = None
        if self.local_app_config.agent.multi_agent_planning_enabled:
            registered_tools = self.tool_registry.list_tools()
            fork_policy = ForkPolicy(
                max_depth=self.local_app_config.agent.max_fork_depth,
                max_children=self.local_app_config.agent.max_children,
                max_fork_size=self.local_app_config.agent.max_fork_size,
                allowed_agent_ids=tuple(
                    agent_id for agent_id, enabled in (
                        (GENERAL_AGENT_ID, True),
                        ("mock_workflow", self.local_app_config.agent.mock_workflow_agent_enabled),
                        (CodexAppServerExpertExecutor.AGENT_ID, self.local_app_config.agent.codex_expert_enabled),
                    ) if enabled
                ),
                allowed_inference_profile_ids=(
                    self.local_app_config.agent.allowed_child_inference_profile_ids
                ),
                allowed_scope=ScopeGrant(
                    workspace_paths=tuple(
                        sorted(root.as_posix() for root in self.settings.parsed_workspace_roots())
                    ),
                    allowed_packages=tuple(
                        sorted({tool.package for tool in registered_tools if tool.package})
                    ),
                    allowed_tools=tuple(sorted(tool.name for tool in registered_tools)),
                    side_effect_level=SideEffectLevel.EXTERNAL,
                ),
            )
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
            tool_invocation_store=self.agent_run_store,
            fork_policy=fork_policy,
            fork_execution=self.execute_multi_agent_plan,
            fork_scope_resolver=self.fork_scope_resolver,
            fork_plan_finalizer=self.finalize_multi_agent_plan,
            fast_path_single_agent_enabled=(
                self.local_app_config.agent.fast_path_single_agent_enabled
            ),
            default_workspace_root=(
                str(self.settings.parsed_workspace_roots()[0])
                if self.settings.parsed_workspace_roots()
                else None
            ),
        )
        self.agent_turn_loop.multi_agent_max_retries = self.local_app_config.agent.multi_agent_max_retries
        self.agent_turn_runner: AgentTurnRunner = self.agent_turn_loop
        if self.local_app_config.agent.orchestrator == "langgraph":
            checkpointer = None
            checkpoint_runtime = None
            if self.local_app_config.agent.checkpoint_backend == "sqlite":
                checkpoint_runtime = create_sqlite_checkpoint_runtime(
                    self.settings.data_dir / "agent_checkpoints.sqlite3"
                )
            self.agent_turn_runner = AgentGraphRunner(
                self.agent_turn_loop,
                checkpointer=checkpointer,
                checkpoint_runtime=checkpoint_runtime,
                artifact_store=self.agent_run_store,
            )
        self.child_agent_executor = ChildAgentExecutor(
            runner=self.agent_turn_runner,
            run_manager=self.agent_run_manager,
        )
        self.agent_executor_registry = AgentExecutorRegistry.with_general_agent(
            self.child_agent_executor, self.run_child_agent_async
        )
        for profile in self.local_app_config.agent.inference_profiles:
            self.agent_executor_registry.register_inference_profile(profile)
        self.agent_executor_registry.register(
            # Explicit local opt-in only: this demonstration returns simulated
            # output and must not silently answer ordinary user tasks.
            AgentDefinition(
                agent_id="mock_workflow",
                version="1",
                executor_kind="workflow",
                enabled=self.local_app_config.agent.mock_workflow_agent_enabled,
            ),
            MockWorkflowExecutor(self.agent_run_manager),
        )
        self.codex_expert_executor: CodexAppServerExpertExecutor | None = None
        self.codex_trace_journal: CodexTraceJournal | None = None
        if self.local_app_config.agent.codex_expert_enabled:
            codex_binary = self.local_app_config.agent.codex_binary_path
            workspace_base = self.local_app_config.agent.codex_workspace_base
            if codex_binary is None or workspace_base is None:
                raise ValueError("Enabled Codex expert requires a binary and isolated workspace base.")
            if not codex_binary.is_file():
                raise ValueError("Configured Codex executable does not exist.")
            workspace_factory = CodexWorkspaceFactory(workspace_base)
            codex_state_home = (
                self.local_app_config.agent.codex_state_home
                or workspace_base / "_codex_state"
            )
            codex_home = self.local_app_config.agent.codex_home
            for private_home in (codex_state_home, codex_home):
                if private_home is None:
                    continue
                resolved_private_home = private_home.resolve(strict=False)
                for configured_root in self.settings.parsed_workspace_roots():
                    resolved_root = configured_root.resolve(strict=False)
                    if (
                        resolved_private_home == resolved_root
                        or resolved_root in resolved_private_home.parents
                        or resolved_private_home in resolved_root.parents
                    ):
                        raise ValueError(
                            "Codex state home must be separate from every configured source workspace."
                        )
            self.codex_trace_journal = CodexTraceJournal(self.db_path)

            async def create_codex_client(_child_run_id: str) -> CodexAppServerClient:
                # Do not merge grants from a pre-existing user profile.
                permission_profile_id = (
                    f"lka_codex_{uuid4().hex}"
                    if self.local_app_config.agent.codex_permission_mode == "profile"
                    else None
                )
                transport = await CodexSubprocessTransport.start(
                    binary_path=codex_binary,
                    cwd=workspace_base,
                    codex_state_home=codex_state_home,
                    codex_home=codex_home,
                    permission_profile_id=permission_profile_id,
                )
                return CodexAppServerClient(
                    transport, permission_profile_id=permission_profile_id
                )

            self.codex_expert_executor = CodexAppServerExpertExecutor(
                self.agent_run_manager,
                client_factory=create_codex_client,
                codex_model=self.local_app_config.agent.codex_model,
                reasoning_effort=self.local_app_config.agent.codex_reasoning_effort,
                route_human_approvals=True,
                approval_guard=staged_file_change_approval_guard,
                trace_journal=self.codex_trace_journal,
                workspace_factory=workspace_factory,
            )
            self.agent_executor_registry.register(
                AgentDefinition(
                    agent_id=CodexAppServerExpertExecutor.AGENT_ID,
                    version=CodexAppServerExpertExecutor.VERSION,
                    executor_kind="external_cli",
                    scope_mode="workspace_sandbox",
                    enabled=True,
                    can_resume=False,
                ),
                self.codex_expert_executor,
            )
        self.multi_agent_scheduler = MultiAgentScheduler(
            run_manager=self.agent_run_manager,
            child_executor=self.child_agent_executor,
            agent_registry=self.agent_executor_registry,
            max_concurrency=self.local_app_config.agent.multi_agent_max_concurrency,
            max_global_concurrency=self.local_app_config.agent.multi_agent_global_max_concurrency,
            max_retries=self.local_app_config.agent.multi_agent_max_retries,
        )
        self._multi_agent_resume_locks: dict[str, threading.Lock] = {}
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
        close_runner = getattr(self.agent_turn_runner, "close", None)
        if callable(close_runner):
            close_runner()
        self.agent_run_manager.close()

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
        except Exception as exc:  # noqa: BLE001 - persist unexpected provider failures for diagnostics.
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
        status: str = "completed",
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
                (workspace_id, workspace, source_frontend, status, indexed_files, indexed_chunks),
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

        indexed_files = scan_result.indexed_files
        indexed_chunks = scan_result.indexed_chunks
        knowledge_errors: list[str] = []
        status = "completed"
        if (options or {}).get("index_knowledge"):
            if not self.path_resolver.is_allowed_workspace(resolved):
                raise ValueError("workspace is outside configured roots")
            knowledge_result = WorkspaceKnowledgeIndexer(
                self.knowledge_service,
                [resolved.resolved_path],
                max_files=min(self.settings.max_scan_files, 500),
            ).index_workspace(resolved.resolved_path)
            indexed_files = knowledge_result.imported_files
            indexed_chunks = knowledge_result.imported_chunks
            knowledge_errors = knowledge_result.errors
            if knowledge_result.failed_files or knowledge_result.errors:
                status = "partial" if indexed_files else "failed"

        self._upsert_workspace(
            workspace_id,
            resolved.normalized_path,
            source_frontend,
            indexed_files,
            indexed_chunks,
            status,
        )
        return WorkspaceIndexResponse(
            workspace_id=workspace_id,
            status=status,
            indexed_files=indexed_files,
            indexed_chunks=indexed_chunks,
            knowledge_errors=knowledge_errors,
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
        safety_review_mode: SafetyReviewMode | None = None,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        """Run the minimal general agent turn loop."""

        return self.agent_turn_runner.run(
            session_id=session_id,
            user_input=user_input,
            llm_client_name=llm_client_name,
            llm_model=llm_model,
            llm_response_mode=llm_response_mode,
            safety_review_mode=safety_review_mode,
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
        safety_review_mode: SafetyReviewMode | None = None,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        """Run one agent turn without blocking the event loop."""

        return await self.agent_turn_runner.run_async(
            session_id=session_id,
            user_input=user_input,
            llm_client_name=llm_client_name,
            llm_model=llm_model,
            llm_response_mode=llm_response_mode,
            safety_review_mode=safety_review_mode,
            existing_run_id=existing_run_id,
        )

    async def run_child_agent_async(
        self,
        *,
        child_run_id: str,
        snapshot: ContextSnapshot,
        views: ContextViews,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
    ) -> TaskResult:
        """Execute a prepared child through the configured Agent Turn runner."""

        return await self.child_agent_executor.execute(
            child_run_id=child_run_id,
            snapshot=snapshot,
            views=views,
            llm_client_name=llm_client_name,
            llm_model=llm_model,
        )

    def fork_scope_resolver(
        self, run_id: str
    ) -> tuple[ScopeGrant, ScopeGrant, ScopeGrant]:
        """Return parent, session, and workspace ceilings for a fork request."""
        run = self.agent_run_manager.get_run(run_id)
        if run is None:
            return ScopeGrant(), ScopeGrant(), ScopeGrant()
        tools = self.tool_registry.list_tools()
        packages = tuple(sorted({tool.package for tool in tools if tool.package}))
        tool_names = tuple(sorted(tool.name for tool in tools))
        session = self.session_service.get_session_or_none(session_id=run.session_id)
        configured_paths = tuple(
            sorted(root.as_posix() for root in self.settings.parsed_workspace_roots())
        )
        selected_paths = (
            (session.workspace.backend_path,)
            if session is not None and session.workspace is not None
            else configured_paths
        )
        account_ids = self.mail_service.list_authorized_account_ids()
        visible_sources = set()
        for path in selected_paths or (None,):
            visible_sources.update(self.knowledge_service.list_authorized_source_ids(
                workspace_path=path, session_id=run.session_id,
            ))
        source_ids = tuple(sorted({
            *visible_sources,
            *(self.mail_knowledge_mirror.source_id_for_account(account_id) for account_id in account_ids),
        }))
        workspace_scope = ScopeGrant(
            workspace_paths=selected_paths,
            source_ids=source_ids,
            account_ids=account_ids,
            allowed_packages=packages,
            allowed_tools=tool_names,
            side_effect_level=SideEffectLevel.EXTERNAL,
        )
        parent_snapshot = run.metadata.get("context_snapshot")
        if isinstance(parent_snapshot, dict):
            try:
                effective_parent = ContextSnapshot.model_validate(parent_snapshot).effective_scope
            except Exception:  # noqa: BLE001 - malformed persisted context fails closed.
                effective_parent = ScopeGrant()
        else:
            effective_parent = ScopeGrant(
                workspace_paths=selected_paths,
                source_ids=source_ids,
                account_ids=account_ids,
                allowed_packages=packages,
                allowed_tools=tool_names,
                side_effect_level=SideEffectLevel.EXTERNAL,
            )
        session_scope = ScopeGrant(
            workspace_paths=selected_paths,
            source_ids=source_ids,
            account_ids=account_ids,
            allowed_packages=packages,
            allowed_tools=tool_names,
            side_effect_level=SideEffectLevel.EXTERNAL,
        )
        return effective_parent, session_scope, workspace_scope

    def _live_fork_policy(self) -> ForkPolicy | None:
        """Refresh trusted local inventories while preserving static fork limits."""
        base = self.agent_turn_loop.fork_policy
        if base is None:
            return None
        tools = self.tool_registry.list_tools()
        accounts = self.mail_service.list_authorized_account_ids()
        visible_sources = set(self.knowledge_service.list_authorized_source_ids())
        for root in self.settings.parsed_workspace_roots():
            visible_sources.update(
                self.knowledge_service.list_authorized_source_ids(
                    workspace_path=root.as_posix()
                )
            )
        sources = tuple(sorted({
            *visible_sources,
            *(self.mail_knowledge_mirror.source_id_for_account(account_id) for account_id in accounts),
        }))
        updated_scope = base.allowed_scope.model_copy(update={
            "workspace_paths": tuple(sorted(root.as_posix() for root in self.settings.parsed_workspace_roots())),
            "source_ids": sources,
            "account_ids": accounts,
            "allowed_packages": tuple(sorted({tool.package for tool in tools if tool.package})),
            "allowed_tools": tuple(sorted(tool.name for tool in tools)),
        })
        return base.model_copy(update={"allowed_scope": updated_scope})

    def _parent_knowledge_evidence_candidates(
        self, run_id: str, *, source_ids: tuple[str, ...], account_ids: tuple[str, ...]
    ) -> tuple[EvidenceCandidate, ...]:
        """Rehydrate only still-authorized parent RAG references for child derivation."""
        events = self.agent_run_store.list_completed_tool_results(
            run_id=run_id,
            tool_names=("knowledge.search", "knowledge.load_chunks"),
        )
        references: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for event in events:
            result = event["result"]
            if result.get("status") != "completed":
                continue
            output = result.get("output")
            if not isinstance(output, dict):
                continue
            records = output.get("results", output.get("chunks", []))
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict) or record.get("policy_decision") not in {"allowed", "redacted"}:
                    continue
                chunk_id = record.get("chunk_id")
                source_id = record.get("source_id")
                source_ref = record.get("source_ref")
                if not all(isinstance(value, str) and value for value in (chunk_id, source_id, source_ref)):
                    continue
                if chunk_id in seen_ids or source_id not in source_ids:
                    continue
                references.append((chunk_id, source_id))
                seen_ids.add(chunk_id)
                if len(references) >= 20:
                    break
            if len(references) >= 20:
                break
        if not references:
            return ()
        loaded = self.knowledge_service.load_chunks(
            chunk_ids=[chunk_id for chunk_id, _ in references],
            max_chars_per_chunk=420,
            source_ids=list(source_ids),
            account_ids=list(account_ids) if account_ids else None,
        )
        by_id = {chunk.chunk_id: chunk for chunk in loaded.chunks}
        candidates: list[EvidenceCandidate] = []
        for chunk_id, source_id in references:
            chunk = by_id.get(chunk_id)
            if chunk is None or chunk.source_id != source_id:
                continue
            candidates.append(EvidenceCandidate(
                evidence=EvidenceRef(
                    evidence_id=chunk_id, source_ref=chunk.source_ref,
                    source_id=source_id, untrusted_data=True,
                ),
                summary=f"{chunk.title}: {chunk.text[:160]}",
                excerpt=chunk.text[:420],
            ))
        return tuple(candidates)

    async def execute_multi_agent_plan_async(
        self,
        parent_run_id: str,
        *,
        context: SchedulerContext | None = None,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
    ) -> MultiAgentScheduleResult:
        """Run or resume the durable DAG attached to an active parent Agent run."""
        if self.multi_agent_scheduler is None:
            raise RuntimeError("Multi-Agent scheduling is not initialized.")
        if llm_client_name is None and llm_model is None:
            parent_events = self.agent_run_manager.list_events(parent_run_id)
            run_started = next(
                (event for event in parent_events if event.type == "run_started"),
                None,
            )
            selection = run_started.payload if run_started is not None else {}
            if selection.get("inference_selection_source") == "request":
                client_override = selection.get("inference_client_name")
                model_override = selection.get("inference_model")
                llm_client_name = client_override if isinstance(client_override, str) else None
                llm_model = model_override if isinstance(model_override, str) else None
        if context is None:
            parent_scope, session_scope, workspace_scope = self.fork_scope_resolver(parent_run_id)
            evidence_candidates = self._parent_knowledge_evidence_candidates(
                parent_run_id,
                source_ids=session_scope.source_ids,
                account_ids=session_scope.account_ids,
            )
            context = SchedulerContext(
                parent_effective_scope=parent_scope,
                session_scope=session_scope,
                workspace_scope=workspace_scope,
                policy_scope=ScopeGrant(
                    workspace_paths=workspace_scope.workspace_paths,
                    source_ids=session_scope.source_ids,
                    account_ids=session_scope.account_ids,
                    allowed_packages=tuple(sorted({tool.package for tool in self.tool_registry.list_tools() if tool.package})),
                    allowed_tools=tuple(sorted(tool.name for tool in self.tool_registry.list_tools())),
                    side_effect_level=SideEffectLevel.EXTERNAL,
                ),
                budget=RuntimeBudget(),
                fork_policy=self._live_fork_policy(),
                evidence_candidates=evidence_candidates,
            )
        return await self.multi_agent_scheduler.execute_plan_async(
            parent_run_id,
            context=context,
            llm_client_name=llm_client_name,
            llm_model=llm_model,
        )

    def execute_multi_agent_plan(self, parent_run_id: str) -> MultiAgentScheduleResult:
        """Synchronous bridge for Agent graph nodes running in worker threads."""
        return asyncio.run(self.execute_multi_agent_plan_async(parent_run_id))

    def cancel_multi_agent_child(self, *, parent_run_id: str, child_run_id: str) -> AgentRunRecord:
        """Cancel an active child owned by the given parent run."""
        if self.multi_agent_scheduler is None:
            raise RuntimeError("Multi-Agent scheduling is not initialized.")
        return self.multi_agent_scheduler.cancel_child_run(
            parent_run_id=parent_run_id,
            child_run_id=child_run_id,
        )

    async def retry_multi_agent_child(
        self, *, parent_run_id: str, child_run_id: str
    ) -> MultiAgentScheduleResult:
        """Retry an explicitly selected failed child attempt under its parent."""
        if self.multi_agent_scheduler is None:
            raise RuntimeError("Multi-Agent scheduling is not initialized.")
        parent_scope, session_scope, workspace_scope = self.fork_scope_resolver(parent_run_id)
        tools = self.tool_registry.list_tools()
        evidence_candidates = self._parent_knowledge_evidence_candidates(
            parent_run_id,
            source_ids=session_scope.source_ids,
            account_ids=session_scope.account_ids,
        )
        context = SchedulerContext(
            parent_effective_scope=parent_scope,
            session_scope=session_scope,
            workspace_scope=workspace_scope,
            policy_scope=ScopeGrant(
                workspace_paths=workspace_scope.workspace_paths,
                source_ids=session_scope.source_ids,
                account_ids=session_scope.account_ids,
                allowed_packages=tuple(sorted({tool.package for tool in tools if tool.package})),
                allowed_tools=tuple(sorted(tool.name for tool in tools)),
                side_effect_level=SideEffectLevel.EXTERNAL,
            ),
            budget=RuntimeBudget(),
            evidence_candidates=evidence_candidates,
            fork_policy=self._live_fork_policy(),
        )
        return await self.multi_agent_scheduler.retry_child_run_async(
            parent_run_id=parent_run_id,
            child_run_id=child_run_id,
            context=context,
        )

    async def resume_multi_agent_parent_async(
        self, parent_run_id: str
    ) -> AgentTurnResult | MultiAgentScheduleResult | None:
        """Resume parent orchestration after its child confirmation is resolved."""
        lock = self._multi_agent_resume_locks.setdefault(parent_run_id, threading.Lock())
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            parent = self.agent_run_manager.get_run(parent_run_id)
            if parent is None:
                raise KeyError(f"Agent run not found: {parent_run_id}")
            if parent.status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT}:
                if parent.parent_run_id is not None:
                    return await self.resume_multi_agent_parent_async(parent.parent_run_id)
                return None
            if parent.status not in {AgentRunStatus.RUNNING, AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}:
                raise ValueError("Parent run is not resumable for multi-Agent scheduling.")
            active_plan_id = parent.plan_id
            nested_plan = parent.metadata.get("multi_agent_plan")
            if (
                isinstance(nested_plan, dict)
                and nested_plan.get("parent_run_id") == parent.run_id
                and isinstance(nested_plan.get("plan_id"), str)
            ):
                active_plan_id = nested_plan["plan_id"]
            waiting = [
                self.agent_run_manager.get_run(child_id)
                for child_id in parent.child_run_ids
            ]
            waiting = [
                child for child in waiting
                if child is not None and child.plan_id == active_plan_id
                and child.status in {AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}
            ]
            if parent.status == AgentRunStatus.WAITING_USER:
                if waiting:
                    return None
                waiting_child_ids = parent.metadata.get("waiting_child_user_run_ids", [])
                # A continued child is RUNNING while its checkpoint is being
                # driven. Keep the parent parked until every child recorded by
                # the wait event is terminal; merely leaving WAITING_USER is
                # not sufficient to unlock the parent's graph.
                if not isinstance(waiting_child_ids, list) or not waiting_child_ids:
                    return None
                terminal_child_statuses = {
                    AgentRunStatus.COMPLETED,
                    AgentRunStatus.FAILED,
                    AgentRunStatus.CANCELLED,
                    AgentRunStatus.TIMED_OUT,
                }
                for child_id in waiting_child_ids:
                    child = self.agent_run_manager.get_run(str(child_id))
                    if (
                        child is None
                        or child.parent_run_id != parent_run_id
                        or child.status not in terminal_child_statuses
                    ):
                        return None
                resumed_parent = self.agent_run_manager.resume_after_child_user(parent_run_id)
                if resumed_parent.status != AgentRunStatus.RUNNING:
                    return None
                parent = self.agent_run_manager.get_run(parent_run_id)
                if parent is None:
                    raise KeyError(f"Agent run not found: {parent_run_id}")
            if waiting:
                return await self.execute_multi_agent_plan_async(parent_run_id)
            if isinstance(self.agent_turn_runner, AgentGraphRunner):
                if parent.status == AgentRunStatus.WAITING_CONFIRMATION:
                    self.agent_run_manager.resume_running(parent_run_id)
                result = await self.agent_turn_runner.resume_async(parent_run_id)
                updated_parent = self.agent_run_manager.get_run(parent_run_id)
                if updated_parent is not None and updated_parent.parent_run_id is not None and updated_parent.status in {
                    AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT
                }:
                    await self.resume_multi_agent_parent_async(updated_parent.parent_run_id)
                return result
            if parent.status == AgentRunStatus.WAITING_CONFIRMATION:
                self.agent_run_manager.resume_running(parent_run_id)
            result = await self.execute_multi_agent_plan_async(parent_run_id)
            updated_parent = self.agent_run_manager.get_run(parent_run_id)
            if updated_parent is not None and updated_parent.parent_run_id is not None and updated_parent.status in {
                AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT
            }:
                await self.resume_multi_agent_parent_async(updated_parent.parent_run_id)
            return result
        finally:
            lock.release()

    async def resume_multi_agent_user_question_async(
        self, run_id: str, command_id: str
    ) -> AgentTurnResult:
        """Resume a question interrupt using the private, durable answer journal."""
        run = self.agent_run_manager.get_run(run_id)
        if run is None:
            raise KeyError(f"Agent run not found: {run_id}")
        if run.status != AgentRunStatus.RUNNING:
            raise ValueError("Agent run is not active after a user continuation.")
        if run.metadata.get("pending_user_answer_command_id") != command_id:
            raise ValueError("User continuation command does not match the active run.")
        if self.agent_run_manager.get_user_continuation(run_id, command_id) is None:
            raise ValueError("Trusted user continuation journal entry is missing.")
        if not isinstance(self.agent_turn_runner, AgentGraphRunner):
            raise TypeError("User-question continuation requires the LangGraph orchestrator.")
        result = await self.agent_turn_runner.resume_async(run_id)
        resumed = self.agent_run_manager.get_run(run_id)
        if resumed is None or resumed.status not in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.TIMED_OUT,
        }:
            return result
        parent_id = resumed.parent_run_id
        if parent_id:
            # The parent hook has its own persisted wait-set gate and lock. Call
            # it for every terminal child, including nested children; it will
            # resume only when all direct children it is waiting for are
            # terminal, then propagate further up the ancestor chain.
            await self.resume_multi_agent_parent_async(parent_id)
        return result

    def finalize_multi_agent_plan(self, parent_run_id: str) -> None:
        """Close a plan after the parent Agent has produced its final answer."""
        parent = self.agent_run_manager.get_run(parent_run_id)
        if parent is None:
            return
        raw_plan = parent.metadata.get("multi_agent_plan")
        if not isinstance(raw_plan, dict) or raw_plan.get("parent_run_id") != parent_run_id:
            return
        from app.core.multi_agent import Plan, PlanStatus, PlanStepStatus

        plan = Plan.model_validate(raw_plan)
        if self.multi_agent_scheduler is None:
            raise RuntimeError("Multi-Agent scheduler is not initialized.")
        child_steps = [step for step in plan.steps if step.step_id != "root_coordinator"]
        if any(step.status in {PlanStepStatus.PENDING, PlanStepStatus.READY, PlanStepStatus.RUNNING, PlanStepStatus.WAITING} for step in child_steps):
            unresolved = ", ".join(
                step.step_id for step in child_steps
                if step.status in {PlanStepStatus.PENDING, PlanStepStatus.READY, PlanStepStatus.RUNNING, PlanStepStatus.WAITING}
            )
            raise RuntimeError(f"Cannot finalize a plan with unresolved child steps: {unresolved}.")
        root = next((step for step in plan.steps if step.step_id == "root_coordinator"), None)
        if root is not None and root.status == PlanStepStatus.RUNNING:
            plan = self.multi_agent_scheduler._set_step_status(
                plan, root.step_id, PlanStepStatus.COMPLETED
            )
        if any(step.status in {PlanStepStatus.FAILED, PlanStepStatus.BLOCKED} for step in child_steps):
            plan = plan.transition_to(PlanStatus.FAILED)
        elif plan.status == PlanStatus.REPLANNING:
            # The Planner chose a final answer after processing its last
            # replan/user observation. Re-enter execution state before closing
            # the plan; REPLANNING intentionally is not terminal.
            plan = plan.transition_to(PlanStatus.RUNNING).transition_to(PlanStatus.COMPLETED)
        elif plan.status == PlanStatus.RUNNING:
            plan = plan.transition_to(PlanStatus.COMPLETED)
        self.agent_run_manager.record_multi_agent_plan(
            parent_run_id,
            event_type="multi_agent_plan_finalized",
            payload={"plan_id": plan.plan_id, "status": plan.status.value},
            plan=plan.model_dump(mode="json"),
        )

    def list_codex_native_trace(
        self,
        child_run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[CodexTraceEvent]:
        """Read private native Codex events for trusted runtime/eval consumers.

        These payloads may contain source code, prompts, and command output;
        they must not be returned through the ordinary Agent run API.
        """
        child = self.agent_run_manager.get_run(child_run_id)
        if child is None or child.parent_run_id is None:
            raise KeyError(f"Child Agent run not found: {child_run_id}")
        if child.metadata.get("agent_id") != CodexAppServerExpertExecutor.AGENT_ID:
            raise ValueError("Child run is not a Codex expert run.")
        journal = self.codex_trace_journal or CodexTraceJournal(self.db_path)
        return journal.list(child_run_id, after_sequence=after_sequence, limit=limit)

    def resume_agent_run(self, run_id: str) -> AgentTurnResult:
        """Resume an incomplete LangGraph run from its latest checkpoint."""

        if not isinstance(self.agent_turn_runner, AgentGraphRunner):
            raise RuntimeError(  # noqa: TRY004 - this is runtime mode, not an argument type.
                "Agent run recovery requires the LangGraph orchestrator."
            )
        return self.agent_turn_runner.resume(run_id)

    async def resume_agent_run_async(self, run_id: str) -> AgentTurnResult:
        """Asynchronously resume an incomplete LangGraph run."""

        if not isinstance(self.agent_turn_runner, AgentGraphRunner):
            raise RuntimeError(  # noqa: TRY004 - this is runtime mode, not an argument type.
                "Agent run recovery requires the LangGraph orchestrator."
            )
        return await self.agent_turn_runner.resume_async(run_id)

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

        if not self.local_app_config.mail.outlook.enabled:
            raise OutlookConfigError("Outlook sync is disabled by local configuration.")
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
