from __future__ import annotations

import json
import sqlite3

import pytest

from app.domains.memory import MemoryService
from app.domains.projects import ProjectConflictError, ProjectService
from app.storage.db import init_db


@pytest.fixture
def services(tmp_path):
    db_path = tmp_path / "projects.sqlite3"

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    init_db(db_path)
    memory = MemoryService(db_path)
    memory.ensure_schema()
    projects = ProjectService(connect, memory)
    projects.ensure_schema()
    return projects, memory, connect


def test_register_uses_canonical_identity_and_preserves_existing_name(services, tmp_path):
    projects, memory, _ = services
    path = tmp_path / "workspace"
    path.mkdir()

    first = projects.register(path, "Initial name")
    repeated = projects.register(path, "Replacement name")

    assert first["project_id"] == memory.resolve_project(path)
    assert repeated["project_id"] == first["project_id"]
    assert repeated["name"] == "Initial name"
    assert repeated["revision"] == 1
    assert repeated["workspace_path"] == memory._path_key(path)


def test_list_includes_empty_profiles_search_and_paginates(services, tmp_path):
    projects, _, _ = services
    projects.register(tmp_path / "one", "Alpha")
    projects.register(tmp_path / "two", "Beta")
    projects.register(tmp_path / "three", "Gamma")

    first = projects.list(limit=2)
    second = projects.list(limit=2, offset=first["next_offset"])
    filtered = projects.list(q="amm")

    assert [item["name"] for item in first["projects"]] == ["Alpha", "Beta"]
    assert first["next_offset"] == 2
    assert [item["name"] for item in second["projects"]] == ["Gamma"]
    assert second["next_offset"] is None
    assert [item["name"] for item in filtered["projects"]] == ["Gamma"]
    assert all(item["session_count"] == 0 for item in first["projects"])


def test_session_count_matches_project_id_or_active_backend_path(services, tmp_path):
    projects, memory, connect = services
    path = tmp_path / "workspace"
    path.mkdir()
    profile = projects.register(path)
    other_path = tmp_path / "other"
    other_path.mkdir()
    other_profile = projects.register(other_path)
    canonical = memory._path_key(path)
    sessions = [
        ("s1", {"project_id": profile["project_id"]}),
        ("s2", {"workspace": {"backend_path": canonical}}),
        ("s3", {"project_id": other_profile["project_id"]}),
        ("s4", {"project_id": profile["project_id"]}),
    ]
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO agent_sessions(session_id,title,status,metadata,created_at,updated_at) "
            "VALUES(?,?, 'active', ?, 'now', 'now')",
            [(sid, sid, json.dumps(meta)) for sid, meta in sessions],
        )
        conn.execute(
            "UPDATE agent_sessions SET status='deleted' WHERE session_id='s4'"
        )
        conn.commit()
    finally:
        conn.close()

    assert projects.get(profile["project_id"])["session_count"] == 2


def test_rename_uses_revision_cas_and_missing_ids_raise_key_error(services, tmp_path):
    projects, _, _ = services
    profile = projects.register(tmp_path / "workspace", "Before")

    renamed = projects.rename(profile["project_id"], "After", expected_revision=1)
    assert renamed["name"] == "After"
    assert renamed["revision"] == 2
    with pytest.raises(ProjectConflictError):
        projects.rename(profile["project_id"], "Stale", expected_revision=1)
    with pytest.raises(KeyError):
        projects.get("missing")
    with pytest.raises(KeyError):
        projects.rename("missing", "Name", expected_revision=1)


def test_project_metadata_does_not_change_memory_identity_table_shapes(services):
    _, _, connect = services
    conn = connect()
    try:
        projects_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_projects)")}
        paths_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_project_paths)")}
    finally:
        conn.close()
    assert projects_columns == {"project_id", "created_at"}
    assert paths_columns == {"path_key", "project_id", "active", "created_at"}
