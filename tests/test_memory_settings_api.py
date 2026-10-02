from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.api.main import create_app
from app.core.config import get_settings


def make_app(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    if not config.exists():
        config.write_text("[memory]\ngeneration_output_tokens=4096\n", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    return create_app()


def call(app, method, url, *, host="127.0.0.1", **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 1234)),
                                     base_url="http://test") as client:
            return await client.request(method, url, **kwargs)
    return asyncio.run(run())


def test_config_persists_without_touching_toml_and_applies_only_after_restart(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    config = tmp_path / "config.toml"
    before = config.read_bytes()
    initial = call(app, "GET", "/background/config").json()
    assert initial["revision"] == 0
    saved = call(app, "PATCH", "/background/config", json={
        "expected_revision": 0, "memory": {"generation_output_tokens": 6000, "background_enabled": False},
        "background": {"request_timeout_seconds": 45.0},
    })
    assert saved.status_code == 200
    data = saved.json()
    assert data["active"]["memory"]["generation_output_tokens"] == 4096
    assert data["desired"]["memory"]["generation_output_tokens"] == 6000
    assert data["restart_required"] is True
    assert app.state.runtime.memory_background.generation_output_tokens == 4096
    assert config.read_bytes() == before
    restarted = make_app(tmp_path, monkeypatch)
    state = call(restarted, "GET", "/background/config").json()
    assert state["revision"] == 1 and state["restart_required"] is False
    assert restarted.state.runtime.memory_background.generation_output_tokens == 6000
    assert restarted.state.runtime.local_app_config.memory.background_enabled is False
    assert call(restarted, "DELETE", "/background/config?expected_revision=0").status_code == 409
    reset = call(restarted, "DELETE", "/background/config?expected_revision=1").json()
    assert reset["revision"] == 2 and reset["restart_required"] is True
    assert reset["desired"]["memory"]["generation_output_tokens"] == 4096
    assert call(make_app(tmp_path, monkeypatch), "GET", "/background/config").json()["restart_required"] is False


@pytest.mark.parametrize("changes", [
    {"memory": {"generation_output_tokens": 0}},
    {"memory": {"enabled": "yes"}},
    {"memory": {"api_key": "secret"}},
    {"memory": {"background_client_name": "unknown"}},
    {"memory": {"background_model": "unknown"}},
    {"background": {"max_llm_concurrency": 1}},
    {"background": {"daily_cost_limit": 1.0}},
])
def test_invalid_config_does_not_persist(tmp_path, monkeypatch, changes):
    app = make_app(tmp_path, monkeypatch)
    response = call(app, "PATCH", "/background/config", json={"expected_revision": 0, **changes})
    assert response.status_code == 422
    assert "secret" not in response.text
    assert call(app, "GET", "/background/config").json()["revision"] == 0


def test_revision_conflicts_preserve_other_settings(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert call(app, "PATCH", "/background/config", json={"expected_revision": 0,
                "memory": {"allow_remote_extraction": True}}).status_code == 200
    assert call(app, "PATCH", "/background/config", json={"expected_revision": 0,
                "memory": {"enabled": False}}).status_code == 409
    saved = call(app, "PATCH", "/background/config", json={"expected_revision": 1,
                "memory": {"generation_output_tokens": 5000}}).json()
    assert saved["desired"]["memory"]["allow_remote_extraction"] is True
    assert saved["desired"]["memory"]["enabled"] is True


def test_auth_for_configuration_read_and_write(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert call(app, "GET", "/background/config", host="192.0.2.1").status_code == 403
    monkeypatch.setenv("LKA_MEMORY_API_TOKEN", "test-secret")
    assert call(app, "GET", "/background/config").status_code == 401
    assert call(app, "PATCH", "/background/config", json={"expected_revision": 0}).status_code == 401
    authorized = call(app, "GET", "/background/config", headers={"Authorization": "Bearer test-secret"})
    assert authorized.status_code == 200
    assert "test-secret" not in authorized.text


def test_settings_only_override_saved_fields_after_toml_changes(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    call(app, "PATCH", "/background/config", json={"expected_revision": 0,
         "memory": {"generation_output_tokens": 6000}})
    (tmp_path / "config.toml").write_text("[memory]\nmax_recalled_items=3\n", encoding="utf-8")
    restarted = make_app(tmp_path, monkeypatch)
    assert restarted.state.runtime.local_app_config.memory.max_recalled_items == 3
    assert restarted.state.runtime.local_app_config.memory.generation_output_tokens == 6000
    assert json.loads(json.dumps(call(restarted, "GET", "/background/config").json()))["restart_required"] is False


def test_schema_exposes_limits_but_no_provider_credentials(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    response = call(app, "GET", "/background/config/schema")
    assert response.status_code == 200
    assert response.json()["memory"]["properties"]["generation_output_tokens"]["minimum"] == 256
    assert "api_key" not in response.text
    assert call(app, "GET", "/background/config/schema", host="192.0.2.1").status_code == 403


def test_invalid_saved_values_do_not_prevent_config_recovery(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.state.runtime.memory_settings_store.save({"memory": {"generation_output_tokens": 0}}, expected_revision=0)
    restarted = make_app(tmp_path, monkeypatch)
    response = call(restarted, "GET", "/background/config")
    assert response.status_code == 200
    assert response.json()["config_load_error"] == "invalid_saved_configuration"
    assert restarted.state.runtime.memory_background.generation_output_tokens == 4096
    assert call(restarted, "DELETE", "/background/config?expected_revision=1").status_code == 200


def test_corrupt_json_can_be_reset_without_breaking_startup(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with app.state.runtime._conn() as conn:
        conn.execute("UPDATE memory_background_settings SET overrides='not json'")
    restarted = make_app(tmp_path, monkeypatch)
    state = call(restarted, "GET", "/background/config").json()
    assert state["config_load_error"] == "invalid_saved_configuration"
    assert call(restarted, "DELETE", "/background/config?expected_revision=0").status_code == 200


def test_real_app_routes_control_jobs_and_expose_pending_config_in_health(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.api.routes.background_controls import _health

    app = make_app(tmp_path, monkeypatch)
    store = app.state.runtime.background_job_store
    item = store.enqueue("memory_extract", "frontend-session", "frontend-test", {"message_id": "private-id"})
    claimed = store.claim("test", 60)
    assert store.fail(item["job_id"], "test", claimed["lease_epoch"], "provider_timeout", False)
    failed = store.get(item["job_id"])
    retried = call(app, "POST", f"/background/jobs/{item['job_id']}/retry",
                   json={"expected_updated_at": failed["updated_at"]})
    assert retried.status_code == 200 and retried.json()["status"] == "queued"
    assert "private-id" not in retried.text
    cancelled = call(app, "POST", f"/background/jobs/{item['job_id']}/cancel",
                     json={"expected_updated_at": retried.json()["updated_at"]})
    assert cancelled.status_code == 200
    call(app, "PATCH", "/background/config", json={"expected_revision": 0, "memory": {"enabled": False}})
    health = _health(SimpleNamespace(app=app))
    assert health["config_revision"] == 1 and health["config_restart_required"] is True
    assert "private-id" not in json.dumps(health)
    assert call(app, "GET", "/sessions/unknown/context-status").status_code == 404


def test_real_context_status_for_fresh_and_compacted_sessions(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    runtime.session_service.ensure_session(session_id="context-ui")
    fresh = call(app, "GET", "/sessions/context-ui/context-status")
    assert fresh.status_code == 200
    assert fresh.json()["method"] == "none"
    runtime.session_service.record_context_exchange(
        session_id="context-ui", user_input="private user body", agent_answer="private answer",
        trace_id="t", token_budget=1000,
    )
    with runtime._conn() as conn:
        conn.execute("UPDATE agent_session_context_state SET summary_revision=7,summary_metadata=? WHERE session_id=?",
                     (json.dumps({"method": "local_fallback", "input_trace_ids": ["private-source"]}), "context-ui"))
    state = call(app, "GET", "/sessions/context-ui/context-status")
    assert state.status_code == 200
    assert state.json()["degraded"] is True
    assert state.json()["summary_revision"] == 7
    assert "private" not in state.text
    assert "token_count_method" in state.json()
