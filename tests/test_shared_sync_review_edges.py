"""Adversarial sync pagination and snapshot regressions on synthetic data."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore
from uuid import uuid4

import pytest

from app.domains import shared_sync_read
from app.domains.shared_sync import PROJECTIONS, SharedSyncConfig, SharedSyncService, canonical
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.mysql import MySQLConfig, MySQLPoolTimeout, close_mysql_pools
from tests.test_mysql_access_path_live import mysql_access_factory  # noqa: F401


@pytest.mark.skipif(not os.environ.get("LKA_MYSQL_TEST_CONFIG"), reason="isolated MySQL lab required")
def test_live_mysql_one_slot_pool_can_reauthorize_and_serve_concurrent_readers(tmp_path):
    import json

    values = json.loads(Path(os.environ["LKA_MYSQL_TEST_CONFIG"]).read_text())
    values.update(pool_size=1, pool_wait_seconds=.5)
    config = MySQLConfig(**values)
    identity = tmp_path / "one-slot.sqlite3"
    configure_mysql(identity, config)

    def factory():
        return connect_database(identity)

    class Devices:
        def require_node(self, conn, principal, scope):
            assert conn.execute("SELECT 1").fetchone()[0] == 1

    try:
        service = SharedSyncService(factory, SharedSyncConfig(enabled=True), Devices())
        principal = {"node_id": "synthetic-one-slot"}
        after = service.status(principal)["cursor"]

        def read(_index):
            return service.pull(after=after, principal=principal)

        with ThreadPoolExecutor(max_workers=2) as pool:
            pages = list(pool.map(read, range(4)))
        assert len(pages) == 4
        assert all(page["cursor"] == after and page["changes"] == [] for page in pages)
    finally:
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)


@pytest.fixture
def sqlite_sync_factory(tmp_path):
    path = tmp_path / "sync.sqlite3"

    def factory():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    with factory() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        for kind, (keys, fields) in PROJECTIONS.items():
            columns = ",".join(
                f"{field} " + ("INTEGER" if field in ("slot", "revision", "version") else "TEXT")
                for field in fields
            )
            conn.execute(f"CREATE TABLE {kind}({columns},PRIMARY KEY({','.join(keys)}))")
    return factory


def _cleanup_projects(factory, projects):
    with factory() as conn:
        for project in projects:
            conn.execute("DELETE FROM memory_projects WHERE project_id=?", (project,))
            for table in ("shared_sync_journal", "shared_sync_heads"):
                conn.execute(
                    f"DELETE FROM {table} WHERE kind='memory_projects' AND entity_key=?",
                    (canonical([project]),),
                )


@pytest.mark.parametrize("endpoint", ["status", "pull"])
@pytest.mark.parametrize("revoke_on_release", [False, True])
def test_sync_releases_page_connection_before_final_authorization(
    sqlite_sync_factory, endpoint, revoke_on_release
):
    service = SharedSyncService(sqlite_sync_factory, SharedSyncConfig(enabled=True))
    slot = BoundedSemaphore(1)
    authorizations = []
    released = False

    class Lease:
        def __init__(self, conn):
            self.conn = conn

        @property
        def row_factory(self):
            return self.conn.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.conn.row_factory = value

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def close(self):
            nonlocal released
            self.conn.close()
            slot.release()
            released = True

    def one_slot_factory():
        if not slot.acquire(blocking=False):
            raise MySQLPoolTimeout("one-slot pool exhausted by nested checkout")
        return Lease(sqlite_sync_factory())

    class Devices:
        def require_node(self, conn, principal, scope):
            authorizations.append(released)
            if released and revoke_on_release:
                raise PermissionError("revoked after page transaction closed")

    service._conn_factory = one_slot_factory
    service.devices = Devices()
    call = getattr(service, endpoint)
    if revoke_on_release:
        with pytest.raises(PermissionError, match="revoked after page transaction closed"):
            call(principal={"node_id": "synthetic-node"})
    else:
        assert call(principal={"node_id": "synthetic-node"})["hub_id"]
    assert authorizations == [False, True]


@pytest.mark.parametrize("backend", ["sqlite", "mysql"])
def test_pull_head_and_record_share_one_snapshot(backend, request, monkeypatch):
    if backend == "mysql" and not os.environ.get("LKA_MYSQL_TEST_CONFIG"):
        pytest.skip("isolated MySQL lab required")
    fixture = "mysql_access_factory" if backend == "mysql" else "sqlite_sync_factory"
    factory = request.getfixturevalue(fixture)
    service = SharedSyncService(factory, SharedSyncConfig(enabled=True))
    project = "snapshot-project-" + uuid4().hex
    before = service.status()["cursor"]
    try:
        with factory() as conn:
            conn.execute(
                "INSERT INTO memory_projects(project_id,created_at) VALUES(?,'before')", (project,)
            )
        original = shared_sync_read._select_by_keys
        committed = False
        next_state = "after"

        def commit_between_head_and_record(conn, table, *args):
            nonlocal committed
            if table == "memory_projects" and not committed:
                committed = True
                with factory() as writer:
                    writer.execute(
                        "UPDATE memory_projects SET created_at=? WHERE project_id=?",
                        (next_state, project),
                    )
            return original(conn, table, *args)

        monkeypatch.setattr(shared_sync_read, "_select_by_keys", commit_between_head_and_record)
        authorizations = []

        class Devices:
            def require_node(self, conn, principal, scope):
                state = conn.execute(
                    "SELECT created_at FROM memory_projects WHERE project_id=?", (project,)
                ).fetchone()[0]
                authorizations.append(state)
                if state == "revoked":
                    raise PermissionError("synthetic credential revoked")

        service.devices = Devices()
        page = service.pull(after=before, limit=500, principal={"node_id": "synthetic-node"})
        assert authorizations == ["before", "after"]
        changes = [change for change in page["changes"] if change["key"] == [project]]
        assert committed
        assert len(changes) == 1
        assert changes[0]["version"] == 1
        assert changes[0]["record"]["created_at"] == "before"
        following = service.pull(after=page["cursor"], limit=500)
        changes = [change for change in following["changes"] if change["key"] == [project]]
        assert len(changes) == 1
        assert changes[0]["version"] == 2
        assert changes[0]["record"]["created_at"] == "after"
        # The end-of-page authorization must reject a revocation committed
        # after the original authorization, even though the page is pinned.
        committed, next_state = False, "revoked"
        with pytest.raises(PermissionError, match="synthetic credential revoked"):
            service.pull(after=before, limit=500, principal={"node_id": "synthetic-node"})
        if backend == "mysql":
            with factory() as conn:
                isolation = conn.execute_native("SELECT @@transaction_isolation").fetchone()[0]
                assert isolation == "READ-COMMITTED"
    finally:
        _cleanup_projects(factory, [project])


def test_pull_byte_cursor_at_batch_boundary_and_latest_deleted_heads(sqlite_sync_factory):
    factory = sqlite_sync_factory
    service = SharedSyncService(factory, SharedSyncConfig(enabled=True))
    with factory() as conn:
        for index in range(35):
            conn.execute(
                "INSERT INTO memory_projects(project_id,created_at) VALUES(?,?)",
                (f"project-{index:02}", "é" * 2200),
            )
        conn.execute("UPDATE memory_projects SET created_at='new' WHERE project_id='project-00'")
        conn.execute("DELETE FROM memory_projects WHERE project_id='project-01'")
    # Compute the exact budget needed for the first 32 events, including UTF-8.
    complete = service.pull(limit=500)
    events = complete["changes"]
    budget = sum(len(canonical(event).encode()) for event in events[:32])
    service.config = SharedSyncConfig(enabled=True, max_page_bytes=budget + 8192)
    first = service.pull(limit=500)
    assert len(first["changes"]) == 32
    assert first["cursor"] == events[31]["sequence"]
    assert first["has_more"]
    following = service.pull(after=first["cursor"], limit=500)
    assert first["changes"] + following["changes"] == events
    assert all(
        event["deleted"] and event["record"] is None
        for event in events
        if event["key"] == ["project-01"]
    )
    assert all(
        event["version"] == 2 and event["record"]["created_at"] == "new"
        for event in events
        if event["key"] == ["project-00"]
    )
