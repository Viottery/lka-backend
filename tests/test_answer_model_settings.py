"""Separate answer-model preferences survive saves, queues and controller revisions."""

import asyncio
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes import ui_preferences
from app.api.routes.managers import router as managers_router
from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.domains.management import ManagementConflictError
from tests.test_ui_preferences import _app


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = LocalAppConfig()
    config.management.enabled = True
    config.agent.unified_entry_enabled = True
    config.memory.enabled = False
    config.message_history.enabled = False
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    instance = LocalKnowledgeAgentRuntime(Settings(
        LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path),
    ))
    yield instance
    instance.stop()


def test_shared_defaults_preserve_answer_choice_on_legacy_save_and_clear(tmp_path, monkeypatch):
    request = SimpleNamespace(app=_app(tmp_path, monkeypatch))
    try:
        saved = ui_preferences.put_ui_defaults(ui_preferences.UIDefaults(
            llm_client="planner", llm_model="reasoner", llm_answer_client="renderer",
            llm_answer_model="writer", reasoning_level="high",
        ), request)
        assert saved["defaults"]["llm_answer_model"] == "writer"
        ui_preferences.put_ui_defaults(ui_preferences.UIDefaults(stream=False), request)
        saved = ui_preferences.get_ui_defaults(request)["defaults"]
        assert saved["llm_answer_client"] == "renderer"
        assert saved["llm_answer_model"] == "writer"
        assert saved["reasoning_level"] == "high"
        ui_preferences.put_ui_defaults(ui_preferences.UIDefaults(
            llm_answer_client="", llm_answer_model="",
        ), request)
        saved = ui_preferences.get_ui_defaults(request)["defaults"]
        assert saved["llm_answer_client"] == saved["llm_answer_model"] == ""
    finally:
        request.app.state.runtime.stop()


def test_manager_answer_preference_is_independent_and_revision_checked(runtime):
    service = runtime.management_service
    created = service.create(llm_model="reasoner", llm_answer_model="writer")
    sid = created["session"]["session_id"]
    controller = service.update(sid, expected_revision=1, llm_answer_model="writer-v2")["controller"]
    assert controller["llm_model"] == "reasoner"
    assert controller["llm_answer_model"] == "writer-v2"
    with pytest.raises(ManagementConflictError):
        service.update(sid, expected_revision=1, llm_answer_model="stale")
    policy = runtime.session_service.run_policy(sid)
    assert policy.llm_answer_model == "writer-v2"
    service.update(sid, expected_revision=2, llm_answer_model=None, llm_answer_client_name=None)
    assert runtime.session_service.run_policy(sid).llm_answer_model is None


def test_manager_api_rejects_unknown_answer_client_and_supports_clear(runtime):
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(managers_router)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=("127.0.0.1", 9000)), base_url="http://test") as client:
            created = await client.post("/managers", json={"llm_model": "reasoner", "llm_answer_model": "writer"})
            assert created.status_code == 201, created.text
            sid = created.json()["session"]["session_id"]
            rejected = await client.patch(f"/managers/{sid}", json={
                "expected_revision": 1, "llm_answer_client_name": "unknown",
            })
            assert rejected.status_code == 422
            cleared = await client.patch(f"/managers/{sid}", json={
                "expected_revision": 1, "llm_answer_model": None, "llm_answer_client_name": None,
            })
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["controller"]["llm_answer_model"] is None
            assert cleared.json()["controller"]["llm_model"] == "reasoner"

    asyncio.run(exercise())


def test_queue_freezes_answer_model_and_null_options_keep_legacy_idempotency(runtime):
    sid = runtime.management_service.create(make_default=True, llm_answer_model="writer")["session"]["session_id"]
    queue = runtime.session_inputs
    first = queue.submit(sid, client_message_id="legacy", user_input="Hello", attachment_ids=[])
    replay = queue.submit(sid, client_message_id="legacy", user_input="Hello", attachment_ids=[], llm={
        "response_mode": "text", "answer_client_name": None, "answer_model": None,
    })
    assert replay["input"] == first["input"]
    runtime.management_service.update(sid, expected_revision=1, llm_answer_model="new-writer")
    queue.tick()
    run = runtime.agent_run_manager.get_run(queue.snapshot(sid)["active_run_id"])
    assert run.metadata["session_policy"]["llm_answer_model"] == "writer"
    assert runtime.agent_run_manager.get_run(run.run_id).metadata["session_policy"]["llm_answer_model"] == "writer"


def test_queue_does_not_batch_messages_accepted_with_different_answer_models(runtime):
    sid = runtime.management_service.create(make_default=True, llm_answer_model="writer")["session"]["session_id"]
    first = runtime.session_inputs.submit(sid, client_message_id="first", user_input="First", attachment_ids=[])
    runtime.management_service.update(sid, expected_revision=1, llm_answer_model="new-writer")
    runtime.session_inputs.submit(sid, client_message_id="second", user_input="Second", attachment_ids=[])
    runtime.session_inputs.tick()
    run = runtime.agent_run_manager.get_run(runtime.session_inputs.snapshot(sid)["active_run_id"])
    frozen = runtime.session_inputs.freeze_for_dispatch(run, SimpleNamespace(check=lambda **_kwargs: None))
    assert [item["input_id"] for item in frozen.metadata["session_input_batch"]] == [first["input"]["input_id"]]
    assert frozen.metadata["session_policy"]["llm_answer_model"] == "writer"


def test_queue_explicit_answer_choice_overrides_controller_at_admission(runtime):
    sid = runtime.management_service.create(make_default=True, llm_answer_model="writer")["session"]["session_id"]
    runtime.session_inputs.submit(sid, client_message_id="override", user_input="Hello", attachment_ids=[], llm={
        "response_mode": "text", "answer_model": "request-writer",
    })
    runtime.session_inputs.tick()
    run = runtime.agent_run_manager.get_run(runtime.session_inputs.snapshot(sid)["active_run_id"])
    assert run.metadata["session_policy"]["llm_answer_model"] == "request-writer"
    assert runtime.management_service.get(sid)["controller"]["llm_answer_model"] == "writer"


def test_manager_answer_schema_migration_keeps_existing_preferences(runtime):
    sid = runtime.management_service.create(llm_model="existing")["session"]["session_id"]
    with closing(runtime._conn()) as conn, conn:
        conn.execute("ALTER TABLE manager_controllers DROP COLUMN llm_answer_client_name")
        conn.execute("ALTER TABLE manager_controllers DROP COLUMN llm_answer_model")
    runtime.management_service.ensure_schema()
    runtime.management_service.ensure_schema()
    controller = runtime.management_service.get(sid)["controller"]
    assert controller["llm_model"] == "existing"
    assert controller["llm_answer_model"] is None
