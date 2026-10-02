from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.main import create_app
from app.api.routes import ui_preferences
from app.core.config import get_settings


def _app(tmp_path, monkeypatch, *, config_text: str = ""):
    config = tmp_path / "local.toml"
    config.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    return create_app()


def test_ui_defaults_persist_across_runtime_restarts(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    request = SimpleNamespace(app=app)
    initial = ui_preferences.get_ui_defaults(request)
    assert initial["configured"] is False
    assert initial["defaults"]["workspace_parent"] == ""
    saved = ui_preferences.put_ui_defaults(ui_preferences.UIDefaults.model_validate({
        "llm_client": "primary", "llm_model": "chosen",
        "safety_mode": "manual", "stream": False,
        "workspace_parent": "C:/Users/test/Documents/My Workspaces",
    }), request)
    assert saved["configured"] is True

    loaded = ui_preferences.get_ui_defaults(SimpleNamespace(app=_app(tmp_path, monkeypatch)))
    assert loaded["configured"] is True
    assert loaded["defaults"]["llm_model"] == "chosen"
    assert loaded["defaults"]["stream"] is False
    assert loaded["defaults"]["workspace_parent"] == "C:/Users/test/Documents/My Workspaces"
    with pytest.raises(ValidationError):
        ui_preferences.UIDefaults(safety_mode="unsafe")


def test_model_catalog_uses_provider_list_and_configured_fallback(tmp_path, monkeypatch):
    config = '''[llm]
default_client = "primary"
[[llm.clients]]
name = "primary"
provider = "openai_compatible"
base_url = "https://models.example/v1"
api_key_env = "TEST_MODEL_CATALOG_KEY"
default_model = "base-model"
available_models = ["base-model", "configured-model"]
'''
    monkeypatch.setenv("TEST_MODEL_CATALOG_KEY", "secret-test-key")
    app = _app(tmp_path, monkeypatch, config_text=config)

    async def discovered(_client):
        return ["remote-model"]

    monkeypatch.setattr(ui_preferences, "_fetch_provider_model_ids", discovered)
    ui_preferences._model_cache.clear()
    request = SimpleNamespace(app=app)
    payload = asyncio.run(ui_preferences.list_agent_models(request, refresh=True))
    assert payload["default_client"] == "primary"
    assert payload["clients"][0]["models"] == ["base-model", "configured-model", "remote-model"]
    assert "secret-test-key" not in str(payload)

    async def failed(_client):
        raise ValueError("not available")

    monkeypatch.setattr(ui_preferences, "_fetch_provider_model_ids", failed)
    ui_preferences._model_cache.clear()
    fallback = asyncio.run(ui_preferences.list_agent_models(request, refresh=True))
    assert fallback["clients"][0]["models"] == ["base-model", "configured-model"]
    assert fallback["clients"][0]["source"] == "configured"
