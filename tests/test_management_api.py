"""Offline API and runtime integration for the management-session pilot."""
import asyncio
import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.managers import router as managers_router
from app.api.routes.sessions import router as sessions_router
from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.sessions import SessionService
from app.storage.db import connect, init_db
from tests.test_unified_entry_quality import ScriptedClient, operation


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


def test_legacy_kind_migration_is_additive_and_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_sessions (session_id TEXT PRIMARY KEY,title TEXT NOT NULL,"
                     "status TEXT NOT NULL,metadata TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO agent_sessions VALUES ('legacy','Old','active','{\"kind\":\"manager\"}','a','b')")
    init_db(path)
    init_db(path)
    session = SessionService(lambda: connect(path)).get_session(session_id="legacy").session
    assert session.kind == "ordinary" and session.metadata == {"kind": "manager"}
    assert session.created_at == "a" and session.updated_at == "b"


def test_manager_runtime_chat_uses_configured_defaults_without_workspace(runtime):
    created = runtime.management_service.create(llm_model="pilot-model")
    sid = created["session"]["session_id"]
    client = ScriptedClient([operation("final_answer", reason="Conversation."), "Ready."])
    runtime.agent_turn_loop.llm_client = client
    result = runtime.run_agent_turn(session_id=sid, user_input="Hello")
    assert result.answer == "Ready."
    assert [package["name"] for package in result.package_catalog] == ["matter", "management"]
    assert all(call["model"] == "pilot-model" for call in client.calls)
    assert runtime.get_session(session_id=sid).session.kind == "manager"
    assert runtime.get_session(session_id=sid).session.workspace is None
    assert runtime.project_service.list()["projects"] == []


def test_manager_api_and_generic_lifecycle_are_consistent(runtime):
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(managers_router)
    app.include_router(sessions_router)

    async def exercise():
        transport = ASGITransport(app=app, client=("127.0.0.1", 9000))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            runtime.local_app_config.management.enabled = False
            assert (await client.post("/managers/default")).status_code == 409
            runtime.local_app_config.management.enabled = True
            first = await client.post("/managers/default")
            assert first.status_code == 200, first.text
            item = first.json()
            sid = item["session"]["session_id"]
            assert item["session"]["kind"] == "manager"
            assert item["controller"]["status"] == "paused"
            assert (await client.post("/managers/default")).json()["session"]["session_id"] == sid
            assert (await client.get("/managers")).json()[0]["session"]["session_id"] == sid
            assert (await client.get(f"/sessions/{sid}")).json()["session"]["kind"] == "manager"
            sessions = (await client.get("/sessions")).json()["sessions"]
            assert any(s["session_id"] == sid and s["kind"] == "manager" for s in sessions)
            renamed = await client.patch(f"/sessions/{sid}", json={"title": "My controller"})
            assert renamed.status_code == 200
            assert (await client.get(f"/managers/{sid}")).json()["session"]["title"] == "My controller"
            invalid = await client.patch(f"/managers/{sid}", json={"expected_revision": 1, "llm_client_name": "unknown"})
            assert invalid.status_code == 422
            updated = await client.patch(f"/managers/{sid}", json={"expected_revision": 1, "status": "active", "llm_model": "pilot"})
            assert updated.status_code == 200, updated.text
            stale = await client.patch(f"/managers/{sid}", json={"expected_revision": 1, "status": "paused"})
            assert stale.status_code == 409
            assert (await client.delete(f"/sessions/{sid}")).status_code == 409
            paused = await client.patch(f"/managers/{sid}", json={"expected_revision": 2, "status": "paused", "llm_model": None})
            assert paused.json()["controller"]["llm_model"] is None
            stamp = paused.json()["session"]["updated_at"]
            cleanup = await client.delete(f"/sessions/{sid}", params={"only_if_empty": "true", "expected_updated_at": stamp})
            assert cleanup.status_code == 409
            assert (await client.delete(f"/sessions/{sid}")).status_code == 204
            assert (await client.post("/managers/default")).status_code == 409
            deleted = (await client.get("/sessions/deleted")).json()["sessions"]
            assert next(s for s in deleted if s["session_id"] == sid)["kind"] == "manager"
            assert (await client.post(f"/sessions/{sid}/restore")).status_code == 200
            restored = (await client.get(f"/managers/{sid}")).json()
            assert restored["controller"]["status"] == "paused"
            assert restored["session"]["kind"] == "manager"
            assert (await client.post("/managers/default")).json()["session"]["session_id"] == sid
            ordinary = await client.post("/sessions", json={"metadata": {"kind": "manager"}})
            assert ordinary.json()["session"]["kind"] == "ordinary"
            assert (await client.get(f"/managers/{ordinary.json()['session']['session_id']}")).status_code == 404
            assert (await client.post("/managers", json={"title": "Another"})).status_code == 201
            assert (await client.post("/managers", json={"title": "  "})).status_code == 422
    asyncio.run(exercise())


def test_direct_session_service_also_applies_lifecycle_guard(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    with pytest.raises(ValueError, match="Pause management"):
        runtime.session_service.delete_session(session_id=sid)
    runtime.management_service.update(sid, expected_revision=2, status="paused")
    assert runtime.session_service.delete_session(session_id=sid)
    assert runtime.session_service.restore_session(session_id=sid)
    assert runtime.management_service.get(sid)["controller"]["status"] == "paused"


def test_deleted_session_cannot_gain_a_run_after_policy_was_read(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    resolve = runtime.session_service.run_policy_provider

    def delete_after_policy(session_id):
        policy = resolve(session_id)
        assert runtime.delete_session(session_id=session_id)
        return policy

    runtime.session_service.run_policy_provider = delete_after_policy
    with pytest.raises(ValueError, match="deleted session"):
        runtime.create_agent_run(session_id=sid, user_input="A racing turn")
    assert not any(run.session_id == sid for run in runtime.agent_run_manager._runs.values())
    with runtime._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs WHERE session_id=?", (sid,)).fetchone()[0] == 0
