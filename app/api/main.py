from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.agent import router as agent_router
from app.api.routes.capabilities import router as capabilities_router
from app.api.routes.health import router as health_router
from app.api.routes.knowledge import router as knowledge_router
from app.api.routes.mail import router as mail_router
from app.api.routes.matters import router as matters_router
from app.api.routes.runtime_debug import router as runtime_debug_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.workspaces import router as workspaces_router
from app.core.config import get_settings
from app.core.runtime import LocalKnowledgeAgentRuntime


def create_app() -> FastAPI:
    settings = get_settings()
    runtime = LocalKnowledgeAgentRuntime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    app.state.runtime = runtime
    cors_origins = settings.parsed_cors_origins()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    app.include_router(health_router)
    app.include_router(workspaces_router)
    app.include_router(capabilities_router)
    app.include_router(runtime_debug_router)
    app.include_router(agent_router)
    app.include_router(sessions_router)
    app.include_router(knowledge_router)
    app.include_router(mail_router)
    app.include_router(matters_router)

    return app


app = create_app()
