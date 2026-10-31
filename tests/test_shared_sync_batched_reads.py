from __future__ import annotations

import json
import sqlite3

from app.domains.shared_sync import PROJECTIONS, SharedSyncConfig, SharedSyncService


def test_pull_batches_repeated_entity_reads_per_event_chunk(tmp_path):
    path = tmp_path / "hub.sqlite3"
    tracing = False
    statements = []

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        if tracing:
            conn.set_trace_callback(statements.append)
        return conn

    with connect() as conn:
        for kind, (keys, fields) in PROJECTIONS.items():
            columns = ",".join(
                f"{field} " + ("INTEGER" if field in ("slot", "revision", "version") else "TEXT")
                for field in fields
            )
            conn.execute(f"CREATE TABLE {kind}({columns},PRIMARY KEY({','.join(keys)}))")
    service = SharedSyncService(connect, SharedSyncConfig(enabled=True, hub_id="batch-test"))

    with connect() as conn:
        conn.execute("INSERT INTO memory_projects(project_id,created_at) VALUES('p','v0')")
        for index in range(40):
            conn.execute(
                "UPDATE memory_projects SET created_at=? WHERE project_id='p'", (f"v{index + 1}",)
            )

    tracing = True
    page = service.pull(limit=100)
    tracing = False

    assert len(page["changes"]) == 41
    assert {change["record"]["created_at"] for change in page["changes"]} == {"v40"}
    selects = [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "PRAGMA"))]
    # One page contains two bounded event chunks; reads scale by chunk, not event.
    assert len(selects) <= 10
    assert not any("PRAGMA TABLE_INFO" in sql.upper() for sql in statements)
    assert not any("SQLITE_MASTER" in sql.upper() for sql in statements)


def test_pull_keeps_byte_truncation_cursor_and_duplicate_latest_projection(tmp_path):
    path = tmp_path / "hub.sqlite3"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    with connect() as conn:
        for kind, (keys, fields) in PROJECTIONS.items():
            columns = ",".join(
                f"{field} " + ("INTEGER" if field in ("slot", "revision", "version") else "TEXT")
                for field in fields
            )
            conn.execute(f"CREATE TABLE {kind}({columns},PRIMARY KEY({','.join(keys)}))")
    service = SharedSyncService(
        connect, SharedSyncConfig(enabled=True, hub_id="bounded-test", max_page_bytes=131072)
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO memory_projects(project_id,created_at) VALUES('large-a',?)", ("a" * 65000,)
        )
        conn.execute(
            "INSERT INTO memory_projects(project_id,created_at) VALUES('large-b',?)", ("b" * 65000,)
        )

    first = service.pull(limit=2)
    assert len(first["changes"]) == 1
    assert first["cursor"] == first["changes"][0]["sequence"]
    assert first["has_more"] is True
    second = service.pull(after=first["cursor"], limit=2)
    assert len(second["changes"]) == 1
    assert second["cursor"] > first["cursor"]
    assert second["changes"][0]["record"]["created_at"] == "b" * 65000


def test_empty_pull_does_not_probe_projection_schema(tmp_path):
    path = tmp_path / "hub.sqlite3"
    statements = []
    tracing = False

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        if tracing:
            conn.set_trace_callback(statements.append)
        return conn

    with connect() as conn:
        for kind, (keys, fields) in PROJECTIONS.items():
            columns = ",".join(
                f"{field} " + ("INTEGER" if field in ("slot", "revision", "version") else "TEXT")
                for field in fields
            )
            conn.execute(f"CREATE TABLE {kind}({columns},PRIMARY KEY({','.join(keys)}))")
    service = SharedSyncService(connect, SharedSyncConfig(enabled=True, hub_id="empty-page"))
    tracing = True
    result = service.pull()
    tracing = False
    assert result["changes"] == []
    assert result["cursor"] == result["high_watermark"]
    assert not any("PRAGMA TABLE_INFO" in sql.upper() for sql in statements)
    assert not any(
        "INFORMATION_SCHEMA" in sql.upper() or "SQLITE_MASTER" in sql.upper() for sql in statements
    )


def test_replica_source_activation_matches_strict_json_review_predicate(tmp_path):
    path = tmp_path / "hub.sqlite3"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    with connect() as conn:
        for kind, (keys, fields) in PROJECTIONS.items():
            columns = ",".join(
                f"{field} " + ("INTEGER" if field in ("slot", "revision", "version") else "TEXT")
                for field in fields
            )
            if kind == "memory_sources":
                columns += ",metadata TEXT"
            conn.execute(f"CREATE TABLE {kind}({columns},PRIMARY KEY({','.join(keys)}))")
    service = SharedSyncService(connect, SharedSyncConfig(enabled=True, hub_id="strict-review"))

    cases = {
        "absent": ({"published_status": "active"}, True),
        "false": ({"published_status": "active", "needs_review": False}, True),
        "zero": ({"published_status": "active", "needs_review": 0}, True),
        "float-zero": ({"published_status": "active", "needs_review": 0.0}, True),
        "negative-float-zero": ({"published_status": "active", "needs_review": -0.0}, True),
        "null": ({"published_status": "active", "needs_review": None}, True),
        "empty-list": ({"published_status": "active", "needs_review": []}, False),
        "empty-object": ({"published_status": "active", "needs_review": {}}, False),
        "empty-string": ({"published_status": "active", "needs_review": ""}, False),
        "string-zero": ({"published_status": "active", "needs_review": "0"}, False),
        "tiny-nonzero": ({"published_status": "active", "needs_review": 1e-31}, False),
        "smaller-nonzero": ({"published_status": "active", "needs_review": 1e-40}, False),
        "negative-tiny-nonzero": ({"published_status": "active", "needs_review": -1e-40}, False),
        "large-nonzero": ({"published_status": "active", "needs_review": 1e100}, False),
        "true": ({"published_status": "active", "needs_review": True}, False),
        "malformed": ("{not-json", False),
    }
    with connect() as conn:
        for name, (metadata, _visible) in cases.items():
            source_id, memory_id = f"source-{name}", f"memory-{name}"
            encoded = metadata if isinstance(metadata, str) else json.dumps(metadata)
            conn.execute(
                "INSERT INTO memory_sources(source_id,source_type,checksum,status,expires_at,created_at,updated_at,metadata) "
                "VALUES(?,'node_memory_replica',NULL,'active',NULL,'now','now',?)",
                (source_id, encoded),
            )
            conn.execute(
                "INSERT INTO memory_entries(memory_id,memory_type,content,scope,status,confidence,sensitivity,version,created_at,updated_at) "
                "VALUES(?,'preference',?,'global','active',0.5,'normal',1,'now','now')",
                (memory_id, memory_id),
            )
            conn.execute(
                "INSERT INTO memory_entry_sources(memory_id,source_id) VALUES(?,?)",
                (memory_id, source_id),
            )

    page = service.pull(limit=500)
    projected = {
        change["key"][0]: change["record"]
        for change in page["changes"]
        if change["kind"] == "memory_entries"
    }
    assert set(projected) == {f"memory-{name}" for name in cases}
    for name, (_metadata, visible) in cases.items():
        record = projected[f"memory-{name}"]
        assert bool(record) is visible, name
