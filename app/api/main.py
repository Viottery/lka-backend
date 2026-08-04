from fastapi import FastAPI

from app.api.routes.capabilities import router as capabilities_router
from app.api.routes.health import router as health_router
from app.api.routes.mail import router as mail_router
from app.api.routes.runtime_debug import router as runtime_debug_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.workspaces import router as workspaces_router
from app.core.config import get_settings
from app.core.runtime import LocalKnowledgeAgentRuntime


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.state.runtime = LocalKnowledgeAgentRuntime(settings)

    app.include_router(health_router)
    app.include_router(workspaces_router)
    app.include_router(capabilities_router)
    app.include_router(runtime_debug_router)
    app.include_router(sessions_router)
    app.include_router(mail_router)

    return app


app = create_app()
