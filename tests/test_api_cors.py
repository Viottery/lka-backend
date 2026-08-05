from __future__ import annotations

from app.api.main import create_app
from app.core.config import get_settings


def test_local_pet_frontend_origin_is_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    cors_middleware = [
        middleware
        for middleware in app.user_middleware
        if middleware.cls.__name__ == "CORSMiddleware"
    ]

    assert cors_middleware
    assert "http://127.0.0.1:8780" in cors_middleware[0].kwargs["allow_origins"]
