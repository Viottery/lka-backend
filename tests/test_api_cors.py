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


def test_cross_origin_state_etag_and_conditional_response_are_browser_readable(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv('LKA_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('LKA_LOCAL_CONFIG', str(tmp_path / 'missing-local.toml'))
    get_settings.cache_clear()
    app = create_app()
    origin = 'http://127.0.0.1:8780'
    try:
        session_id = app.state.runtime.create_session(title='State CORS').session.session_id
        client = TestClient(app)
        first = client.get(f'/sessions/{session_id}/state', headers={'Origin': origin})
        assert first.status_code == 200
        assert first.headers['access-control-allow-origin'] == origin
        assert 'etag' in first.headers['access-control-expose-headers'].lower()
        second = client.get(f'/sessions/{session_id}/state', headers={
            'Origin': origin, 'If-None-Match': first.headers['etag'],
        })
        assert second.status_code == 304
        assert second.content == b''
        assert second.headers['access-control-allow-origin'] == origin
        assert 'etag' in second.headers['access-control-expose-headers'].lower()
    finally:
        app.state.runtime.stop()
        get_settings.cache_clear()
