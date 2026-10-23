from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.account_auth import AccountAuthMiddleware
from app.api.device_connection_auth import DeviceConnectionAuthMiddleware
from app.api.remote_session_boundary import RemoteSessionBoundaryMiddleware
from app.api.routes.accounts import router as accounts_router
from app.api.routes.agent import router as agent_router
from app.api.routes.background_controls import router as background_controls_router
from app.api.routes.capabilities import router as capabilities_router
from app.api.routes.cloud_node import router as cloud_node_router
from app.api.routes.device_connections import hub_router as connection_hub_router
from app.api.routes.device_connections import node_router as connection_node_router
from app.api.routes.device_connections import router as connections_router
from app.api.routes.devices import node_router as device_node_router
from app.api.routes.devices import router as devices_router
from app.api.routes.distributed_client import router as distributed_client_router
from app.api.routes.health import router as health_router
from app.api.routes.knowledge import router as knowledge_router
from app.api.routes.mail import router as mail_router
from app.api.routes.managers import router as managers_router
from app.api.routes.matters import router as matters_router
from app.api.routes.memories import router as memories_router
from app.api.routes.memory_replication import hub_router as memory_replication_hub_router
from app.api.routes.memory_replication import node_router as memory_replication_node_router
from app.api.routes.memory_settings import router as memory_settings_router
from app.api.routes.message_metadata import router as message_metadata_router
from app.api.routes.message_reading import router as message_reading_router
from app.api.routes.messages import router as messages_router
from app.api.routes.node_workspaces import router as node_workspaces_router
from app.api.routes.projects import router as projects_router
from app.api.routes.remote_execution import node_router as execution_node_router
from app.api.routes.remote_execution import router as execution_router
from app.api.routes.runtime_debug import router as runtime_debug_router
from app.api.routes.session_attachments import router as session_attachments_router
from app.api.routes.session_files import router as session_files_router
from app.api.routes.session_inputs import router as session_inputs_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.shared_memory_writes import node_router as shared_memory_writes_node_router
from app.api.routes.shared_memory_writes import router as shared_memory_writes_router
from app.api.routes.shared_sync import node_router as sync_node_router
from app.api.routes.shared_sync import router as sync_router
from app.api.routes.ui_preferences import router as ui_preferences_router
from app.api.routes.watches import router as watches_router
from app.api.routes.workspaces import router as workspaces_router
from app.core.config import get_settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.session_admission import SessionBusyError
from app.storage.sqlite_timing import (
    DatabaseTimingMiddleware,
    close_sqlite_timing,
    configure_sqlite_timing,
)


def create_app() -> FastAPI:
    settings = get_settings()
    timing = configure_sqlite_timing(
        settings.data_dir / "logs" / "database_timing.jsonl",
        enabled=settings.db_timing_enabled, sample_rate=settings.db_timing_sample_rate,
        slow_ms=settings.db_timing_slow_ms,
    )
    runtime = LocalKnowledgeAgentRuntime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        runtime.start()
        try:
            yield
        finally:
            try:
                runtime.stop()
            finally:
                close_sqlite_timing(timing)

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    app.state.runtime = runtime
    app.state.account_service = runtime.account_service
    app.state.device_service = runtime.device_service
    app.state.sync_service = runtime.sync_service
    app.state.shared_replica = runtime.shared_replica
    app.state.shared_memory_writes = runtime.shared_memory_writes
    app.state.shared_memory_write_outbox = runtime.shared_memory_write_outbox
    app.state.memory_replication_service = runtime.memory_replication_service
    app.state.device_connection_service = runtime.device_connection_service
    app.state.remote_execution_service = runtime.remote_execution_service
    app.add_middleware(DatabaseTimingMiddleware)
    app.add_middleware(RemoteSessionBoundaryMiddleware)
    app.add_middleware(AccountAuthMiddleware)
    app.add_middleware(DeviceConnectionAuthMiddleware)

    @app.exception_handler(SessionBusyError)
    async def session_busy(request, exc):
        return JSONResponse(status_code=409, content={
            "detail": str(exc), "session_id": exc.session_id, "run_id": exc.run_id,
        })
    cors_origins = settings.parsed_cors_origins()
    if runtime.local_app_config.auth.enabled:
        cors_origins = list(dict.fromkeys([
            *cors_origins, *runtime.local_app_config.auth.allowed_origins,
        ]))
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=runtime.local_app_config.auth.enabled,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    app.include_router(accounts_router)
    app.include_router(devices_router)
    app.include_router(device_node_router)
    app.include_router(sync_router)
    app.include_router(sync_node_router)
    app.include_router(cloud_node_router)
    app.include_router(connections_router)
    app.include_router(connection_hub_router)
    app.include_router(connection_node_router)
    app.include_router(node_workspaces_router)
    app.include_router(shared_memory_writes_router)
    app.include_router(shared_memory_writes_node_router)
    app.include_router(memory_replication_node_router)
    app.include_router(memory_replication_hub_router)
    app.include_router(execution_router)
    app.include_router(execution_node_router)
    app.include_router(distributed_client_router)
    app.include_router(health_router)
    app.include_router(workspaces_router)
    app.include_router(capabilities_router)
    app.include_router(runtime_debug_router)
    app.include_router(agent_router)
    app.include_router(ui_preferences_router)
    app.include_router(sessions_router)
    app.include_router(session_inputs_router)
    app.include_router(managers_router)
    app.include_router(session_files_router)
    app.include_router(session_attachments_router)
    app.include_router(knowledge_router)
    app.include_router(mail_router)
    app.include_router(messages_router)
    app.include_router(message_reading_router)
    app.include_router(message_metadata_router)
    app.include_router(memories_router)
    app.include_router(memory_settings_router)
    app.include_router(projects_router)
    app.include_router(background_controls_router)
    app.include_router(matters_router)
    app.include_router(watches_router)

    return app


app = create_app()
