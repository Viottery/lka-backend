from __future__ import annotations

import copy
import sqlite3

import pytest
from pydantic import ValidationError

from app.domains.shared_sync import (
    PROJECTIONS,
    NodeProposal,
    SharedSyncConfig,
    SharedSyncService,
    SyncConflictError,
)
from app.storage.shared_replica import SharedReplicaStore


@pytest.fixture
def sync(tmp_path):
    path = tmp_path / "hub.sqlite3"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    with connect() as conn:
        for kind, (keys, fields) in PROJECTIONS.items():
            columns = ",".join(
                f"{f} " + ("INTEGER" if f in ("slot", "revision", "version") else "TEXT")
                for f in fields
            )
            conn.execute(f"CREATE TABLE {kind}({columns},PRIMARY KEY({','.join(keys)}))")
    service = SharedSyncService(connect, SharedSyncConfig(enabled=True, hub_id="test-hub"))
    replica = SharedReplicaStore(tmp_path / "replica.sqlite3", "test-hub")
    return service, replica, connect


def insert(conn, table, **values):
    fields = PROJECTIONS[table][1]
    data = dict.fromkeys(fields)
    data.update(values)
    conn.execute(
        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        [data[k] for k in fields],
    )


def populate(connect):
    with connect() as conn:
        insert(conn, "memory_projects", project_id="p")
        insert(conn, "memory_sources", source_id="s", status="active", source_type="user")
        insert(
            conn,
            "memory_entries",
            memory_id="m",
            content="PRIVATE MEMORY",
            status="active",
            scope="global",
            version=1,
        )
        insert(conn, "memory_entry_sources", memory_id="m", source_id="s")
        insert(
            conn,
            "agent_sessions",
            session_id="manager",
            kind="manager",
            status="active",
            title="Assistant",
        )
        insert(
            conn,
            "agent_sessions",
            session_id="ordinary",
            kind="ordinary",
            status="active",
            title="PRIVATE TRANSCRIPT TITLE",
        )
        insert(conn, "manager_controllers", session_id="manager", status="active", revision=1)
        insert(
            conn,
            "manager_goals",
            goal_id="g",
            session_id="manager",
            status="active",
            objective="Goal",
            revision=1,
        )


def drain(service, replica, limit=2):
    pages = []
    while True:
        page = service.pull(after=replica.status()["cursor"], limit=limit)
        replica.apply_page(page)
        pages.append(page)
        if not page["has_more"]:
            return pages


def test_transaction_rollback_bootstrap_and_restart(sync):
    service, replica, connect = sync
    populate(connect)
    initial = service.status()["cursor"]
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    insert(conn, "memory_projects", project_id="rollback")
    conn.rollback()
    conn.close()
    assert service.status()["cursor"] == initial
    restarted = SharedSyncService(connect, service.config)
    assert restarted.status() == service.status()
    drain(service, replica)
    assert len(replica.list_records("memory_projects")["items"]) == 1
    assert "PRIVATE TRANSCRIPT TITLE" not in str(service.pull())
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM shared_sync_heads WHERE kind='agent_sessions'"
            ).fetchone()[0]
            == 1
        )


def test_existing_data_bootstrap_once(tmp_path):
    path = tmp_path / "existing.sqlite3"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    with connect() as conn:
        conn.execute("CREATE TABLE memory_projects(project_id TEXT PRIMARY KEY, created_at TEXT)")
        conn.execute("INSERT INTO memory_projects VALUES('old','then')")
    config = SharedSyncConfig(enabled=True, hub_id="hub")
    service = SharedSyncService(connect, config)
    assert service.pull()["changes"][0]["record"]["project_id"] == "old"
    assert SharedSyncService(connect, config).status()["cursor"] == 1


def test_pagination_replay_gap_and_tamper_are_atomic(sync):
    service, replica, connect = sync
    populate(connect)
    first = service.pull(limit=2)
    replica.apply_page(first)
    assert replica.apply_page(first)["cursor"] == first["cursor"]
    forged = copy.deepcopy(first)
    forged["changes"][0]["version"] += 1
    with pytest.raises(SyncConflictError):
        replica.apply_page(forged)
    following = service.pull(after=replica.status()["cursor"], limit=2)
    bad = copy.deepcopy(following)
    bad["changes"][0]["sequence"] += 1
    with pytest.raises(ValueError):
        replica.apply_page(bad)
    bad = copy.deepcopy(following)
    bad["changes"][-1]["record"]["trusted_source"] = True
    with pytest.raises(ValueError):
        replica.apply_page(bad)
    assert replica.status()["cursor"] == first["cursor"]
    drain(service, replica)
    assert replica.list_records("memory_entries")["items"][0]["content"] == "PRIVATE MEMORY"
    with pytest.raises(SyncConflictError):
        service.pull(after=service.status()["cursor"] + 1)
    with pytest.raises(ValueError):
        service.pull(after=-1)


def test_source_revocation_link_changes_and_expiry_hide_hub_and_replica(sync):
    service, replica, connect = sync
    populate(connect)
    drain(service, replica)
    assert replica.list_records("memory_entries")["items"]
    with connect() as conn:
        conn.execute("UPDATE memory_sources SET status='revoked' WHERE source_id='s'")
    assert "PRIVATE MEMORY" not in str(service.pull())
    drain(service, replica)
    assert not replica.list_records("memory_entries")["items"]
    with connect() as conn:
        conn.execute("UPDATE memory_sources SET status='active' WHERE source_id='s'")
    drain(service, replica)
    assert replica.list_records("memory_entries")["items"]
    with connect() as conn:
        conn.execute("DELETE FROM memory_entry_sources WHERE memory_id='m'")
    drain(service, replica)
    assert not replica.list_records("memory_entries")["items"]
    with connect() as conn:
        insert(conn, "memory_entry_sources", memory_id="m", source_id="s")
    drain(service, replica)
    assert replica.list_records("memory_entries")["items"]
    with connect() as conn:
        conn.execute(
            "UPDATE memory_sources SET expires_at='2000-01-01T00:00:00+00:00' WHERE source_id='s'"
        )
    assert "PRIVATE MEMORY" not in str(service.pull())
    drain(service, replica)
    assert not replica.list_records("memory_entries")["items"]


def test_candidate_content_not_exported_and_read_time_expiry(sync):
    service, replica, connect = sync
    populate(connect)
    with connect() as conn:
        conn.execute("UPDATE memory_entries SET status='candidate' WHERE memory_id='m'")
    assert "PRIVATE MEMORY" not in str(service.pull())
    drain(service, replica)
    assert not replica.list_records("memory_entries")["items"]


def test_session_delete_restore_and_bound_goal_projection(sync):
    service, replica, connect = sync
    populate(connect)
    with connect() as conn:
        insert(
            conn,
            "matters",
            matter_id="work",
            title="Current title",
            summary="Current summary",
            revision=1,
        )
        insert(
            conn,
            "manager_goal_matter_links",
            goal_id="g",
            manager_session_id="manager",
            matter_id="work",
        )
    drain(service, replica)
    assert (
        replica.list_records("manager_goals")["items"][0]["objective"]
        == "Current title\nCurrent summary"
    )
    with connect() as conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id='manager'")
    assert "Goal" not in str(service.pull())
    drain(service, replica)
    assert not replica.list_records("manager_goals")["items"]
    with connect() as conn:
        conn.execute("UPDATE agent_sessions SET status='active' WHERE session_id='manager'")
    drain(service, replica)
    assert replica.list_records("manager_goals")["items"]
    with connect() as conn:
        conn.execute("UPDATE matters SET deleted_at='today' WHERE matter_id='work'")
    drain(service, replica)
    assert not replica.list_records("matters")["items"]
    assert not replica.list_records("manager_goals")["items"]
    with connect() as conn:
        conn.execute("UPDATE matters SET deleted_at=NULL,title='Restored' WHERE matter_id='work'")
    drain(service, replica)
    assert replica.list_records("manager_goals")["items"][0]["objective"].startswith("Restored")


def test_hard_delete_tombstone_and_explicit_recreation(sync):
    service, replica, connect = sync
    populate(connect)
    drain(service, replica)
    with connect() as conn:
        conn.execute("DELETE FROM memory_projects WHERE project_id='p'")
    drain(service, replica)
    assert not replica.list_records("memory_projects")["items"]
    with connect() as conn:
        insert(conn, "memory_projects", project_id="p")
    drain(service, replica)
    assert replica.list_records("memory_projects")["items"]


def test_node_scope_rechecked_and_proposals_never_publish(sync):
    service, _replica, connect = sync

    class Devices:
        def __init__(self):
            self.calls = []
            self.denied = False

        def require_node(self, conn, principal, scope):
            assert conn.in_transaction
            self.calls.append(scope)
            if self.denied:
                raise PermissionError("revoked")
            return {"scopes": ["sync.read", "sync.propose"]}

    devices = Devices()
    service.devices = devices
    principal = {"node_id": "node", "credential_version": 1}
    service.pull(principal=principal)
    assert devices.calls == ["sync.read", "sync.read"]
    proposal = {
        "proposal_id": "one",
        "kind": "memory_candidate",
        "text": "untrusted participant observation",
    }
    assert service.propose(principal, proposal) == service.propose(principal, proposal)
    with pytest.raises(SyncConflictError):
        service.propose(principal, {**proposal, "text": "different"})
    for field in ("trusted_source", "user_confirmed", "status", "owner", "allowed_tools"):
        with pytest.raises(ValidationError):
            NodeProposal.model_validate({**proposal, field: True})
    assert service.resolve_proposal("node", "one", "reviewed")["status"] == "reviewed"
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM memory_entries").fetchone()[0] == 0
    devices.denied = True
    with pytest.raises(PermissionError):
        service.pull(principal=principal)


def test_wrong_hub_and_forbidden_entity(sync):
    service, replica, connect = sync
    populate(connect)
    page = service.pull()
    page["hub_id"] = "intruder"
    with pytest.raises(SyncConflictError):
        replica.apply_page(page)
    page["hub_id"] = "test-hub"
    page["changes"][0]["kind"] = "agent_runs"
    with pytest.raises(ValueError):
        replica.apply_page(page)
    assert replica.status()["cursor"] == 0


def test_expiry_is_checked_without_new_sync_page(sync, monkeypatch):
    from datetime import UTC, datetime

    import app.storage.shared_replica as module

    service, replica, connect = sync
    populate(connect)
    with connect() as conn:
        conn.execute("UPDATE memory_sources SET expires_at='2999-01-01T00:00:00+00:00'")
    drain(service, replica)
    assert replica.list_records("memory_entries")["items"]

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(3000, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(module, "datetime", Future)
    assert not replica.list_records("memory_entries")["items"]


def test_routes_require_authenticated_account_and_node_scope(sync):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from app.api.routes.shared_sync import node_router, router

    service, replica, connect = sync
    populate(connect)
    app = FastAPI()
    app.state.sync_service, app.state.shared_replica = service, replica

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.headers.get("x-test-account") == "account":
            request.state.account = {"user_id": "owner"}
        return await call_next(request)

    app.include_router(router)
    app.include_router(node_router)
    with TestClient(app) as client:
        assert client.get("/sync/status").status_code == 401
        assert client.get("/node/sync/status").status_code == 401
        assert client.get("/sync/replica/records?kind=memory_entries").status_code == 401
        assert client.get("/sync/status", headers={"x-test-account": "account"}).status_code == 200
        assert (
            client.post(
                "/sync/proposals/n/p/resolve",
                headers={"x-test-account": "account"},
                json={"status": "accepted"},
            ).status_code
            == 422
        )


def test_real_domain_schemas_install_without_schema_replication(tmp_path):
    from app.domains.management import ManagementService
    from app.domains.management_delegates import ManagementDelegateService
    from app.domains.management_goals import ManagementGoalService
    from app.domains.management_work import ManagementWorkService
    from app.domains.matters import MatterCreateInput, MatterService
    from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput
    from app.storage.db import connect, init_db

    path = tmp_path / "real.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    memory = MemoryService(path)
    memory.ensure_schema()
    matters = MatterService(factory)
    management = ManagementService(factory, None)
    goals = ManagementGoalService(factory, management)
    goals.ensure_schema()
    work = ManagementWorkService(
        factory,
        matters=matters,
        tasks=None,
        management=management,
        goals=goals,
        run_store=None,
        run_manager=None,
    )
    work.ensure_schema()
    ManagementDelegateService(
        factory, tasks=None, management=management, goals=goals, registry=None
    ).ensure_schema()
    service = SharedSyncService(factory, SharedSyncConfig(enabled=True, hub_id="real"))
    source = memory.register_source(
        MemorySourceInput(
            source_type="user",
            source_ref="local-secret-ref",
            trusted_source=True,
            metadata={"secret": "PRIVATE METADATA"},
        )
    )
    memory.create(MemoryInput(content="Preference", source_id=source))
    matters.create_matter(MatterCreateInput(title="Work", metadata={"secret": "PRIVATE METADATA"}))
    page = service.pull()
    assert "PRIVATE METADATA" not in str(page) and "local-secret-ref" not in str(page)
    assert "Preference" in str(page)
    replica = SharedReplicaStore(tmp_path / "readonly.sqlite3", "real")
    replica.apply_page(page)
    assert replica.list_records("memory_entries")["items"][0]["content"] == "Preference"


def test_configured_page_limit_includes_envelope(sync):
    from app.domains.shared_sync import canonical

    service, replica, connect = sync
    service.config.max_page_bytes = 131072
    with connect() as conn:
        for number in range(8):
            insert(conn, "matters", matter_id=f"w{number}", title="Work", summary="a" * 60000)
    pages = drain(service, replica)
    assert len(pages) > 1
    assert all(len(canonical(page).encode()) <= service.config.max_page_bytes for page in pages)
    assert len(replica.list_records("matters")["items"]) == 8
