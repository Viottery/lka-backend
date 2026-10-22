from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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
    assert initial["defaults"]["reasoning_level"] == "low"
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
    assert loaded["defaults"]["reasoning_level"] == "low"
    # A legacy desktop PUT that omits the newer field must keep its saved value.
    legacy = ui_preferences.UIDefaults.model_validate({
        "llm_client": "primary", "llm_model": "chosen", "safety_mode": "manual",
        "stream": False, "workspace_parent": "updated",
    })
    ui_preferences.put_ui_defaults(legacy, request)
    assert ui_preferences.get_ui_defaults(request)["defaults"]["reasoning_level"] == "low"
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
    assert "reasoning_capabilities" in payload["clients"][0]
    assert payload["clients"][0]["reasoning_capabilities"]["base-model"]["controlled"] is False
    assert "secret-test-key" not in str(payload)

    async def failed(_client):
        raise ValueError("not available")

    monkeypatch.setattr(ui_preferences, "_fetch_provider_model_ids", failed)
    ui_preferences._model_cache.clear()
    fallback = asyncio.run(ui_preferences.list_agent_models(request, refresh=True))
    assert fallback["clients"][0]["models"] == ["base-model", "configured-model"]
    assert fallback["clients"][0]["source"] == "configured"


def test_unsupported_reasoning_is_rejected_before_run_admission(tmp_path, monkeypatch):
    from types import MethodType

    from app.api.routes.agent import (
        _preflight_reasoning,
        run_agent_turn_endpoint,
        stream_agent_turn,
    )
    from app.api.schemas import AgentTurnRequest
    from app.core.runtime import LocalKnowledgeAgentRuntime

    class CapabilityOnlyClient:
        def __init__(self):
            self.capability = {"controlled": False, "supports_off": False, "effort_map": {},
                               "source": "uncontrolled", "protocol": "openai_chat"}

        def reasoning_capability(self, *, client_name=None, model=None):
            return self.capability

        def validate_reasoning_level(self, *, client_name=None, model=None, level=None):
            return self.capability

    class Runtime:
        def __init__(self):
            self.admissions = 0
            self.agent_llm_client = CapabilityOnlyClient()
            self.resolve_agent_reasoning_level = lambda *, session_id, requested, existing_run_id=None: (
                requested or "low", "request" if requested else "default")
            self.resolve_agent_input_model = lambda **_kwargs: (None, None)
            self.validate_agent_reasoning_selection = MethodType(
                LocalKnowledgeAgentRuntime.validate_agent_reasoning_selection, self)

        def create_agent_run(self, **_kwargs):
            self.admissions += 1
            raise AssertionError("Unsupported reasoning must be rejected before run admission.")

    runtime = Runtime()
    app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))
    async def inline_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("app.api.routes.agent.asyncio.to_thread", inline_thread)

    request = SimpleNamespace(app=app)
    for endpoint, level, capability in (
        (run_agent_turn_endpoint, "off", {"controlled": False, "supports_off": False, "effort_map": {}}),
        (run_agent_turn_endpoint, "low", {"controlled": True, "supports_off": True, "effort_map": {"high": "high"}}),
        (stream_agent_turn, "off", {"controlled": False, "supports_off": False, "effort_map": {}}),
    ):
        runtime.agent_llm_client.capability = capability
        payload = AgentTurnRequest.model_validate({"user_input": "No dispatch", "llm": {"reasoning_level": level}})
        with pytest.raises(HTTPException) as rejected:
            asyncio.run(endpoint(payload, request))
        assert rejected.value.status_code == 422
    runtime.agent_llm_client.capability = {"controlled": False, "supports_off": False, "effort_map": {}}
    low_payload = AgentTurnRequest.model_validate({"user_input": "No dispatch", "llm": {"reasoning_level": "low"}})
    _preflight_reasoning(low_payload, runtime)
    assert runtime.admissions == 0
