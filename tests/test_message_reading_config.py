"""Small offline checks for message-reading configuration persistence."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.api.main import create_app
from app.core.config import get_settings


def _app(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("[memory]\nbackground_enabled=false\n", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.delenv("LKA_MEMORY_API_TOKEN", raising=False)
    get_settings.cache_clear()
    return create_app()


def _call(app, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                     base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def test_message_config_is_versioned_restart_only_and_does_not_enable_policies(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    before = (tmp_path / "config.toml").read_bytes()
    state = _call(app, "GET", "/background/config").json()
    assert state["active"]["message_history"]["schema_version"] == 1
    saved = _call(app, "PATCH", "/background/config", json={"expected_revision": 0,
        "message_history": {"worker_count": 2, "background_enabled": False}})
    assert saved.status_code == 200
    result = saved.json()
    assert result["active"]["message_history"]["worker_count"] == 1
    assert result["desired"]["message_history"]["worker_count"] == 2
    assert "message_history.worker_count" in result["pending_restart_fields"]
    assert runtime.message_history.list_policies() == []
    assert (tmp_path / "config.toml").read_bytes() == before
    get_settings.cache_clear()
    restarted = create_app()
    assert restarted.state.runtime.local_app_config.message_history.worker_count == 2
    assert restarted.state.runtime.local_app_config.message_history.background_enabled is False
    assert _call(restarted, "GET", "/background/config").json()["restart_required"] is False
    assert _call(restarted, "PATCH", "/background/config", json={"expected_revision": 0,
        "message_history": {"enabled": False}}).status_code == 409


@pytest.mark.parametrize("changes", [{"schema_version": 99}, {"api_key": "private-secret"},
    {"enabled": "yes"}, {"background_client_name": "unknown"}, {"background_model": "unknown"}])
def test_invalid_message_config_is_not_saved_or_echoed(tmp_path, monkeypatch, changes):
    app = _app(tmp_path, monkeypatch)
    response = _call(app, "PATCH", "/background/config", json={"expected_revision": 0,
        "message_history": changes})
    assert response.status_code == 422
    assert "private-secret" not in response.text
    assert _call(app, "GET", "/background/config").json()["revision"] == 0


def test_message_schema_and_invalid_saved_profile_allow_safe_recovery(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    schema = _call(app, "GET", "/background/config/schema").json()
    assert schema["message_history"]["properties"]["worker_count"]["maximum"] == 4
    assert "api_key" not in json.dumps(schema)
    app.state.runtime.memory_settings_store.save({"message_history": {"background_client_name": "unknown"}},
                                                 expected_revision=0)
    get_settings.cache_clear()
    restarted = create_app()
    state = _call(restarted, "GET", "/background/config").json()
    assert state["config_load_error"] == "invalid_saved_configuration"
    assert restarted.state.runtime.local_app_config.message_history.background_client_name is None
    assert _call(restarted, "DELETE", "/background/config?expected_revision=1").status_code == 200
