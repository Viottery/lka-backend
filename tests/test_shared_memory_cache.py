from __future__ import annotations

import json
import socket
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.core.memory_context import MemoryContextProvider
from app.core.tools import ToolContext, ToolInvocation
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput
from app.domains.shared_memory_cache import SharedMemoryCache
from app.domains.shared_sync import PROJECTIONS, SharedSyncConfig, SharedSyncService
from app.storage.shared_replica import SharedReplicaStore
from app.tool_packages.memory import ReadMemoryTool, SearchMemoryTool


def remember(service, content="Prefer concise answers", **values):
    source = service.register_source(MemorySourceInput(
        source_type="user_api", source_ref=content, trusted_source=True,
    ))
    return service.create(MemoryInput(
        content=content, source_id=source, memory_type="preference",
        sensitivity="normal", **values,
    ))


@pytest.fixture
def stores(tmp_path):
    local = MemoryService(tmp_path / "local.sqlite3")
    hub = MemoryService(tmp_path / "hub.sqlite3")
    local.ensure_schema()
    hub.ensure_schema()

    def connect():
        return sqlite3.connect(hub.db_path)

    sync = SharedSyncService(connect, SharedSyncConfig(enabled=True, hub_id="hub-test"))
    replica = SharedReplicaStore(tmp_path / "replica.sqlite3", "hub-test")
    return local, hub, sync, replica


def pull(sync, replica):
    pages = []
    for _ in range(20):
        page = sync.pull(after=replica.status()["cursor"], limit=100)
        replica.apply_page(page)
        pages.append(page)
        if not page["has_more"]:
            return pages
    raise AssertionError("fixture journal did not drain")


def apply(replica, kind, key, *, record=None, deleted=False, version=100):
    cursor = replica.status()["cursor"]
    replica.apply_page({
        "hub_id": replica.hub_id, "after": cursor, "cursor": cursor + 1,
        "high_watermark": cursor + 1, "has_more": False,
        "changes": [{"sequence": cursor + 1, "kind": kind, "key": key,
                     "version": version, "record": record, "deleted": deleted}],
    })


def invoke(tool, workspace, **values):
    return tool.invoke(
        invocation=ToolInvocation(invocation_id="read", tool=tool.spec,
                                  session_id="s", context_id="c", input=values),
        context=ToolContext(session_id="s", workspace_root=workspace),
    )


def test_offline_ordinary_context_and_read_tools_consume_cache(stores, monkeypatch):
    local, hub, sync, replica = stores
    memory = remember(hub)
    pull(sync, replica)
    cache = SharedMemoryCache(local, replica)

    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary recall must never contact the cloud")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sync, "pull", forbidden)
    # Staleness is diagnostic: old successful pulls remain usable offline.
    with replica._connection(write=True) as conn:
        conn.execute("UPDATE replica_sync_state SET last_pull_at='2000-01-01T00:00:00+00:00'")
    context = MemoryContextProvider(cache)("s", None, "concise")
    assert context["items"][0]["memory_id"] == cache.cloud_id(memory.memory_id)
    status = context["shared_memory_cache"]
    assert status["stale"] and status["available"] and status["reads_allow_stale"]
    assert status["cursor"] > 0 and status["observed_lag"] == 0
    found = invoke(SearchMemoryTool(cache), None, query="concise")
    assert found.output["memories"][0]["metadata"]["origin"] == "cloud_replica"
    expanded = invoke(ReadMemoryTool(cache), None, memory_id=context["items"][0]["memory_id"])
    assert expanded.status == "completed"
    assert expanded.output["memory"]["content"] == memory.content
    assert expanded.output["memory"]["source_ids"] == [cache.cloud_id(memory.source_ids[0])]
    assert local.list() == []
    assert not hasattr(cache, "create") and not hasattr(cache, "retract")


def test_cloud_and_local_ids_coexist_without_overwrite_or_promotion(stores):
    local, hub, sync, replica = stores
    local_memory = remember(local)
    cloud_memory = remember(hub)
    assert local_memory.memory_id == cloud_memory.memory_id
    pull(sync, replica)
    cache = SharedMemoryCache(local, replica)
    ids = {record.memory_id for record in cache.search("concise")}
    assert ids == {local_memory.memory_id, cache.cloud_id(cloud_memory.memory_id)}
    local.retract(local_memory.memory_id, expected_version=local_memory.version)
    assert cache.get_active(local_memory.memory_id) is None
    assert cache.get_active(cache.cloud_id(cloud_memory.memory_id)) is not None
    assert local.get(local_memory.memory_id).status == "retracted"
    assert hub.get(cloud_memory.memory_id).status == "active"


def test_merged_pagination_and_default_scope_are_bounded(stores, tmp_path):
    local, hub, sync, replica = stores
    remember(local, "Local global preference")
    remember(hub, "Cloud global preference")
    project = local.resolve_project(tmp_path / "private")
    remember(local, "Project private preference", scope="project", project_id=project)
    pull(sync, replica)
    cache = SharedMemoryCache(local, replica)
    records = cache.list()
    assert len(records) == 2 and all(record.scope == "global" for record in records)
    assert cache.list(limit=1) + cache.list(limit=1, offset=1) == records
    assert cache.list(offset=100) == []


def test_project_memory_requires_explicit_binding_and_current_workspace(stores, tmp_path):
    local, hub, sync, replica = stores
    workspace = str(tmp_path / "workspace")
    local_project = local.resolve_project(workspace)
    other_local = local.resolve_project(tmp_path / "other")
    cloud_project = hub.resolve_project(tmp_path / "cloud-project")
    other_cloud = hub.resolve_project(tmp_path / "other-cloud")
    memory = remember(hub, "Project launch convention", scope="project", project_id=cloud_project)
    remember(hub, "Other project secret", scope="project", project_id=other_cloud)
    pull(sync, replica)
    unbound = SharedMemoryCache(local, replica)
    assert unbound.search("Project", scope="project", project_id=local_project) == []
    cache = SharedMemoryCache(local, replica, project_bindings={local_project: cloud_project})
    context = MemoryContextProvider(cache)("s", workspace, "Project")
    assert [record["memory_id"] for record in context["items"]] == [cache.cloud_id(memory.memory_id)]
    assert context["items"][0]["project_id"] == local_project
    assert cache.search("Project", scope="project", project_id=other_local) == []
    assert invoke(ReadMemoryTool(cache), str(tmp_path / "other"),
                  memory_id=cache.cloud_id(memory.memory_id)).status == "rejected"
    assert invoke(ReadMemoryTool(cache), None,
                  memory_id=cache.cloud_id(memory.memory_id)).status == "rejected"


@pytest.mark.parametrize("change", [
    "entry_tombstone", "source_tombstone", "link_tombstone", "project_tombstone",
    "retracted", "candidate", "expired_entry", "expired_source", "invalid_expiry",
    "naive_expiry", "revoked_source", "sensitive",
])
def test_invalid_cached_memory_is_withheld_immediately_without_network(stores, change):
    local, hub, sync, replica = stores
    project = hub.resolve_project("/fixture/project") if change == "project_tombstone" else None
    memory = remember(hub, scope="project" if project else "global", project_id=project)
    pull(sync, replica)
    cache = SharedMemoryCache(local, replica, project_bindings={"local-project": project} if project else {})
    cloud_id = cache.cloud_id(memory.memory_id)
    assert cache.get_active(cloud_id) is not None
    if change.endswith("tombstone"):
        kind, key = {
            "entry_tombstone": ("memory_entries", [memory.memory_id]),
            "source_tombstone": ("memory_sources", memory.source_ids),
            "link_tombstone": ("memory_entry_sources", [memory.memory_id, memory.source_ids[0]]),
            "project_tombstone": ("memory_projects", [project]),
        }[change]
        apply(replica, kind, key, deleted=True)
    else:
        source_change = change in {"expired_source", "revoked_source"}
        kind = "memory_sources" if source_change else "memory_entries"
        record = replica.list_records(kind)["items"][0]
        fields = PROJECTIONS[kind][0]
        if change in {"expired_entry", "expired_source"}:
            record["expires_at"] = "2000-01-01T00:00:00+00:00"
        elif change == "invalid_expiry":
            record["expires_at"] = "not-a-date"
        elif change == "naive_expiry":
            record["expires_at"] = "2999-01-01T00:00:00"
        elif change == "sensitive":
            record["sensitivity"] = "personal"
        else:
            record["status"] = "revoked" if source_change else change
        apply(replica, kind, [record[field] for field in fields], record=record)
    assert cache.get_active(cloud_id) is None
    assert invoke(ReadMemoryTool(cache), None, memory_id=cloud_id).status == "rejected"
    assert cache.search("concise") == []


def test_cached_expiry_elapses_while_offline(stores, monkeypatch):
    local, hub, sync, replica = stores
    expires = datetime.now(UTC) + timedelta(minutes=5)
    memory = remember(hub, expires_at=expires.isoformat())
    pull(sync, replica)
    cache = SharedMemoryCache(local, replica)
    assert cache.get_active(cache.cloud_id(memory.memory_id)) is not None

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return expires + timedelta(seconds=1)

    monkeypatch.setattr("app.storage.shared_replica.datetime", Later)
    assert cache.get_active(cache.cloud_id(memory.memory_id)) is None


def test_context_and_source_lists_stay_bounded(stores):
    local, hub, sync, replica = stores
    memory = remember(hub, "bounded " * 1000)
    for index in range(40):
        source = hub.register_source(MemorySourceInput(
            source_type="user_api", source_ref=f"source-{index}", trusted_source=True,
        ))
        memory = hub.add_source(memory.memory_id, source_id=source,
                                expected_version=memory.version)
    for index in range(12):
        remember(hub, f"bounded preference {index}")
    pull(sync, replica)
    cache = SharedMemoryCache(local, replica)
    assert len(cache.search("bounded", limit=1000)) == 13
    assert len(cache.get_active(cache.cloud_id(memory.memory_id)).source_ids) == 32
    assert cache.get_active(cache.cloud_id(memory.memory_id)).metadata["sources_truncated"]
    view = MemoryContextProvider(cache, max_items=3, max_chars=900)("s", None, "bounded")
    assert len(view["items"]) <= 3
    assert sum(len(item["content"]) for item in view["items"]) <= 900
    assert all(len(item["source_ids"]) <= 8 for item in view["items"])
    with pytest.raises(ValueError):
        replica.read_memories(scope="global", limit=101)


def test_missing_cache_does_not_block_local_memory(stores):
    local, _, _, replica = stores
    memory = remember(local)
    replica.path.unlink()
    cache = SharedMemoryCache(local, replica)
    view = MemoryContextProvider(cache)("s", None, "concise")
    assert view["items"][0]["memory_id"] == memory.memory_id
    assert view["shared_memory_cache"]["available"] is False


def test_unknown_sync_age_is_stale_and_hub_namespaces_are_distinct(stores, tmp_path):
    local, _, _, replica = stores
    cache = SharedMemoryCache(local, replica)
    assert cache.cache_status()["stale"] and cache.cache_status()["last_pull_at"] is None
    other = SharedMemoryCache(local, SharedReplicaStore(tmp_path / "other.sqlite3", "other-hub"))
    assert other.cloud_id("same-id") != cache.cloud_id("same-id")
    assert cache.get_active(other.cloud_id("same-id")) is None
    assert cache.get_active(cache._prefix + "@@invalid") is None
    with pytest.raises(ValueError):
        SharedMemoryCache(local, replica, project_bindings={"a": "cloud", "b": "cloud"})


def test_needs_review_is_not_exported_and_private_metadata_never_leaks(stores):
    _, hub, sync, replica = stores
    memory = remember(hub)
    with hub._connect() as conn:
        conn.execute("UPDATE memory_entries SET metadata=? WHERE memory_id=?",
                     (json.dumps({"private": "not shared"}), memory.memory_id))
    pages = pull(sync, replica)
    assert "not shared" not in json.dumps(pages)
    assert replica.list_records("memory_entries")["items"]
    with hub._connect() as conn:
        conn.execute("UPDATE memory_entries SET metadata=? WHERE memory_id=?",
                     (json.dumps({"needs_review": True, "private": "not shared"}), memory.memory_id))
    pages = pull(sync, replica)
    assert "not shared" not in json.dumps(pages)
    assert any(change["deleted"] for page in pages for change in page["changes"]
               if change["kind"] == "memory_entries")
    assert replica.list_records("memory_entries")["items"] == []
