"""Synthetic remote-execution locking tests against the opt-in MySQL lab."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.local_config import (
    AgentConfig,
    EmbeddingConfig,
    LLMProviderConfig,
    LocalAppConfig,
    ManagementConfig,
    MemoryConfig,
    MessageHistoryConfig,
    RerankerConfig,
)
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.domains.devices import DeviceConfig, DeviceService
from app.domains.remote_execution import ExecutionReport, RemoteExecutionService
from app.storage.connections import unregister_mysql
from tests.test_mysql_owner_live import _config


@pytest.fixture
def remote_hub(tmp_path, monkeypatch):
    config = _config()
    local = LocalAppConfig(
        llm=LLMProviderConfig(provider="mock"),
        agent=AgentConfig(unified_entry_enabled=True, orchestrator="langgraph", checkpoint_backend="sqlite"),
        management=ManagementConfig(enabled=True, autonomous_enabled=False),
        memory=MemoryConfig(enabled=False, background_enabled=False),
        message_history=MessageHistoryConfig(enabled=False, background_enabled=False),
        embedding=EmbeddingConfig(enabled=False),
        reranker=RerankerConfig(enabled=False),
    )
    monkeypatch.setattr(Settings, "load_local_config", lambda _: local)
    data = tmp_path / "mysql-remote-execution"
    settings = Settings(
        _env_file=None, data_dir=data, storage_backend="mysql",
        mysql_host=config.host, mysql_port=config.port, mysql_user=config.user,
        mysql_password=config.password, mysql_database=config.database,
        workspace_roots=str(tmp_path), db_timing_enabled=False,
    )
    runtime = LocalKnowledgeAgentRuntime(settings)
    assert runtime._mysql_config == settings.mysql_config()
    devices = DeviceService(runtime._conn, DeviceConfig(enabled=True))
    enrolled = devices.enroll({
        "device_id": "mysql.remote." + uuid4().hex,
        "name": "Synthetic MySQL worker",
        "platform": "linux",
        "systems": [{"environment_id": "synthetic", "workspace_ids": []}],
        "workspace_ids": [],
        "capabilities": [],
        "scopes": ["heartbeat", "execution.poll", "execution.run", "execution.result"],
    })
    principal = devices.authenticate(enrolled["token"])
    service = RemoteExecutionService(
        runtime._conn, devices, hub_id="mysql-synthetic-hub",
        managed_tasks=runtime.managed_tasks, sessions=runtime.session_service,
        run_store=runtime.agent_run_store, run_manager=runtime.agent_run_manager,
        tool_executor=runtime.tool_executor, assignment_scope="general",
    )
    service.ensure_schema()
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    child = runtime.session_service.create_session(
        title="Synthetic remote execution child",
        creation_source="assistant", creator_session_id=sid,
    )
    run = runtime.create_agent_run(session_id=child.session.session_id,
                                   user_input="Synthetic remote execution test.", resumable=True)
    runtime.agent_run_manager.mark_running(run.run_id)
    task_id = "remote_test_" + uuid4().hex
    lease_token = uuid4().hex + uuid4().hex
    target = {"device_id": enrolled["device_id"], "environment_id": "synthetic",
              "workspace_id": None, "capabilities": [], "resource_kind": None}
    payload = {"hub_id": "mysql-synthetic-hub", "task_id": task_id, "owner_session_id": sid,
               "node_id": enrolled["device_id"], "environment_id": "synthetic",
               "target": target, "run": run.model_dump(mode="json"), "source_view": None,
               "effect_constraints": {}}
    now = datetime.now(UTC).isoformat()
    with runtime._conn() as conn:
        conn.execute(
            "INSERT INTO remote_execution_tasks(task_id,owner_session_id,command_id,fingerprint,node_id,"
            "environment_id,workspace_id,target,payload,state,lease_epoch,lease_hash,credential_version,"
            "lease_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,NULL,?,?,?,1,?,?,?, ?,?)",
            (task_id, sid, "synthetic-command-" + uuid4().hex, uuid4().hex,
             enrolled["device_id"], "synthetic", json.dumps(target), json.dumps(payload), "running",
             hashlib.sha256(lease_token.encode()).hexdigest(), principal["credential_version"],
             (datetime.now(UTC) + timedelta(minutes=2)).isoformat(), now, now),
        )
    try:
        yield {"runtime": runtime, "devices": devices, "principal": principal,
               "device_id": enrolled["device_id"], "service": service,
               "task_id": task_id, "session_id": sid,
               "run_session_id": child.session.session_id, "run": run,
               "lease_token": lease_token}
    finally:
        with runtime._conn() as conn:
            conn.execute("DELETE FROM remote_execution_reports WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM remote_execution_tasks WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM remote_execution_workers WHERE node_id=?", (enrolled["device_id"],))
            conn.execute("DELETE FROM cloud_devices WHERE device_id=?", (enrolled["device_id"],))
        runtime.stop()
        unregister_mysql(runtime.db_path, expected=runtime._mysql_config)


def test_two_workers_cannot_claim_the_same_queued_task(remote_hub):
    e = remote_hub
    task_id = e["task_id"]
    with e["runtime"]._conn() as conn:
        conn.execute("UPDATE remote_execution_tasks SET state='queued',lease_epoch=0,lease_hash=NULL,"
                     "credential_version=NULL,lease_expires_at=NULL WHERE task_id=?", (task_id,))
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=5)
        return e["service"].claim(e["principal"], environment_id="synthetic")["task"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, range(2)))
    assert sum(item is not None for item in outcomes) == 1
    with e["runtime"]._conn() as conn:
        row = conn.execute("SELECT state,lease_epoch FROM remote_execution_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert (row["state"], row["lease_epoch"]) == ("leased", 1)


def test_report_delivery_is_idempotent_and_stale_or_revoked_authority_is_denied(remote_hub):
    e = remote_hub
    task_id = e["task_id"]
    message = {"message_id": "synthetic-message-" + uuid4().hex,
               "role": "agent", "content": "Synthetic remote result delivered once."}
    report = ExecutionReport(
        task_id=task_id, lease_epoch=1, lease_token=e["lease_token"],
        environment_id="synthetic", report_id="synthetic-report-" + uuid4().hex,
        status="completed", messages=[message], result={"answer": "synthetic"},
    ).model_dump(mode="json")
    barrier = Barrier(2)

    def publish(_):
        barrier.wait(timeout=5)
        return e["service"].report(e["principal"], report)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(publish, range(2)))
    assert receipts[0] == receipts[1] and receipts[0]["accepted"]
    with e["runtime"]._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM remote_execution_reports WHERE task_id=?", (task_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_session_messages WHERE session_id=? AND message_id=?",
                            (e["run_session_id"], message["message_id"])).fetchone()[0] == 1
        stored_run = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?",
                                  (e["run"].run_id,)).fetchone()
        row = conn.execute("SELECT state,lease_epoch FROM remote_execution_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert (row["state"], row["lease_epoch"]) == ("completed", 1)
    assert json.loads(stored_run[0])["status"] == "completed"

    stale = {**report, "report_id": "synthetic-stale-" + uuid4().hex, "lease_epoch": 2}
    with pytest.raises(PermissionError, match="result authority changed"):
        e["service"].report(e["principal"], stale)

    revision = e["devices"].get(e["device_id"])["revision"]
    e["devices"].revoke(e["device_id"], revision)
    revoked = {**report, "report_id": "synthetic-revoked-" + uuid4().hex}
    with pytest.raises(PermissionError, match="no longer active"):
        e["service"].report(e["principal"], revoked)
