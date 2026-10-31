"""Live MySQL regressions for batched shared sync and guarded context reads.

Requires an isolated disposable database via ``scripts/run_mysql_tests.py``.
All inserted identities are synthetic and scoped to this module's test run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.sessions import SessionService
from app.domains.shared_sync import SharedSyncConfig, SharedSyncService
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.context_records import ContextRecordConflict, ContextRecordStore
from app.storage.mysql import MySQLConfig, close_mysql_pools
from tests.test_mysql_runtime_live import runtime  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("LKA_MYSQL_TEST_CONFIG"),
    reason="LKA_MYSQL_TEST_CONFIG is required for an isolated MySQL lab database",
)


@pytest.fixture(scope="module")
def mysql_access_factory(tmp_path_factory):
    config_path = Path(os.environ["LKA_MYSQL_TEST_CONFIG"]).expanduser().resolve(strict=True)
    config = MySQLConfig(**json.loads(config_path.read_text(encoding="utf-8")))
    identity = tmp_path_factory.mktemp("mysql-access-path") / "access.sqlite3"
    configure_mysql(identity, config)

    def factory():
        conn = connect_database(identity)
        conn.row_factory = sqlite3.Row
        return conn

    try:
        yield factory
    finally:
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)


def _cleanup_sync(factory, suffix):
    project_ids = [f"sync-project-{suffix}", f"sync-large-a-{suffix}", f"sync-large-b-{suffix}"]
    source_id, memory_id = f"sync-source-{suffix}", f"sync-memory-{suffix}"
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM memory_entry_sources WHERE memory_id=? AND source_id=?",
            (memory_id, source_id),
        )
        conn.execute("DELETE FROM memory_entries WHERE memory_id=?", (memory_id,))
        conn.execute("DELETE FROM memory_sources WHERE source_id=?", (source_id,))
        conn.executemany(
            "DELETE FROM memory_projects WHERE project_id=?", [(item,) for item in project_ids]
        )
        identities = [
            ("memory_projects", json.dumps([item], separators=(",", ":"))) for item in project_ids
        ]
        identities += [("memory_entries", json.dumps([memory_id], separators=(",", ":")))]
        identities += [("memory_sources", json.dumps([source_id], separators=(",", ":")))]
        identities += [
            ("memory_entry_sources", json.dumps([memory_id, source_id], separators=(",", ":")))
        ]
        for kind, key in identities:
            conn.execute(
                "DELETE FROM shared_sync_journal WHERE kind=? AND entity_key=?", (kind, key)
            )
            conn.execute("DELETE FROM shared_sync_heads WHERE kind=? AND entity_key=?", (kind, key))


def test_mysql_shared_sync_batches_mixed_events_and_rechecks_source_visibility(
    mysql_access_factory,
):
    factory = mysql_access_factory
    suffix = uuid4().hex
    project = f"sync-project-{suffix}"
    source_id, memory_id = f"sync-source-{suffix}", f"sync-memory-{suffix}"
    now = "2026-10-30T00:00:00+00:00"
    service = SharedSyncService(factory, SharedSyncConfig(enabled=True, role="hub"))
    try:
        before = service.status()["cursor"]
        with factory() as conn:
            conn.execute(
                "INSERT INTO memory_projects(project_id,created_at) VALUES(?,?)", (project, "v0")
            )
            for index in range(40):
                conn.execute(
                    "UPDATE memory_projects SET created_at=? WHERE project_id=?",
                    (f"v{index + 1}", project),
                )
            conn.execute(
                "INSERT INTO memory_sources(source_id,source_type,source_ref,checksum,excerpt_start,excerpt_end,"
                "trusted_source,status,expires_at,metadata,created_at,updated_at) "
                "VALUES(?,?,?,NULL,NULL,NULL,1,'active',NULL,'{}',?,?)",
                (source_id, "user", suffix, now, now),
            )
            conn.execute(
                "INSERT INTO memory_entries(memory_id,memory_type,content,scope,project_id,status,confidence,"
                "sensitivity,expires_at,dedupe_key,version,supersedes_id,extraction_model,metadata,created_at,updated_at) "
                "VALUES(?, 'preference','synthetic shared sync value','global',NULL,'active',0.5,'normal',NULL,?,1,NULL,NULL,'{}',?,?)",
                (memory_id, suffix, now, now),
            )
            conn.execute(
                "INSERT INTO memory_entry_sources(memory_id,source_id) VALUES(?,?)",
                (memory_id, source_id),
            )

        page = service.pull(after=before, limit=500)
        project_events = [
            change
            for change in page["changes"]
            if change["kind"] == "memory_projects" and change["key"] == [project]
        ]
        memory_events = [
            change
            for change in page["changes"]
            if change["kind"] == "memory_entries" and change["key"] == [memory_id]
        ]
        assert len(project_events) == 41
        assert all(change["record"]["created_at"] == "v40" for change in project_events)
        assert memory_events and all(not change["deleted"] for change in memory_events)
        assert [change["sequence"] for change in page["changes"]] == sorted(
            change["sequence"] for change in page["changes"]
        )

        cursor = page["cursor"]
        with factory() as conn:
            conn.execute(
                "UPDATE memory_sources SET status='revoked' WHERE source_id=?", (source_id,)
            )
        revoked = service.pull(after=cursor, limit=500)
        revoked_entries = [
            change
            for change in revoked["changes"]
            if change["kind"] == "memory_entries" and change["key"] == [memory_id]
        ]
        assert revoked_entries and all(
            change["deleted"] and change["record"] is None for change in revoked_entries
        )
    finally:
        _cleanup_sync(factory, suffix)


def test_mysql_shared_sync_page_byte_cursor_is_resumable(mysql_access_factory):
    factory = mysql_access_factory
    suffix = uuid4().hex
    service = SharedSyncService(
        factory,
        SharedSyncConfig(enabled=True, role="hub", max_page_bytes=131072),
    )
    ids = [f"sync-large-{side}-{suffix}" for side in ("a", "b")]
    try:
        before = service.status()["cursor"]
        with factory() as conn:
            conn.executemany(
                "INSERT INTO memory_projects(project_id,created_at) VALUES(?,?)",
                [(ids[0], "a" * 65000), (ids[1], "b" * 65000)],
            )
        first = service.pull(after=before, limit=2)
        assert len(first["changes"]) == 1
        assert first["cursor"] == first["changes"][0]["sequence"]
        assert first["has_more"] is True
        second = service.pull(after=first["cursor"], limit=2)
        assert len(second["changes"]) == 1
        assert second["changes"][0]["record"]["created_at"] == "b" * 65000
    finally:
        _cleanup_sync(factory, suffix)


def test_mysql_replica_source_activation_rejects_unusual_review_values(mysql_access_factory):
    factory = mysql_access_factory
    suffix = uuid4().hex
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
    }
    source_ids = {name: f"review-source-{name}-{suffix}" for name in cases}
    memory_ids = {name: f"review-memory-{name}-{suffix}" for name in cases}
    now = "2026-10-30T00:00:00+00:00"
    service = SharedSyncService(factory, SharedSyncConfig(enabled=True, role="hub"))
    try:
        before = service.status()["cursor"]
        with factory() as conn:
            for name, (metadata, _visible) in cases.items():
                conn.execute(
                    "INSERT INTO memory_sources(source_id,source_type,source_ref,checksum,excerpt_start,excerpt_end,"
                    "trusted_source,status,expires_at,metadata,created_at,updated_at) "
                    "VALUES(?,'node_memory_replica',?,NULL,NULL,NULL,1,'active',NULL,?,?,?)",
                    (source_ids[name], suffix, json.dumps(metadata), now, now),
                )
                conn.execute(
                    "INSERT INTO memory_entries(memory_id,memory_type,content,scope,project_id,status,confidence,"
                    "sensitivity,expires_at,dedupe_key,version,supersedes_id,extraction_model,metadata,created_at,updated_at) "
                    "VALUES(?, 'preference','synthetic shared source','global',NULL,'active',0.5,'normal',NULL,?,1,NULL,NULL,'{}',?,?)",
                    (memory_ids[name], suffix + name, now, now),
                )
                conn.execute(
                    "INSERT INTO memory_entry_sources(memory_id,source_id) VALUES(?,?)",
                    (memory_ids[name], source_ids[name]),
                )
        page = service.pull(after=before, limit=500)
        entries = {
            change["key"][0]: change["record"]
            for change in page["changes"]
            if change["kind"] == "memory_entries"
        }
        for name, (_metadata, visible) in cases.items():
            assert bool(entries[memory_ids[name]]) is visible, name
    finally:
        with factory() as conn:
            conn.execute("BEGIN")
            for name in cases:
                memory_id, source_id = memory_ids[name], source_ids[name]
                conn.execute(
                    "DELETE FROM memory_entry_sources WHERE memory_id=? AND source_id=?",
                    (memory_id, source_id),
                )
                conn.execute("DELETE FROM memory_entries WHERE memory_id=?", (memory_id,))
                conn.execute("DELETE FROM memory_sources WHERE source_id=?", (source_id,))
                for kind, values in (
                    ("memory_entries", [memory_id]),
                    ("memory_sources", [source_id]),
                    ("memory_entry_sources", [memory_id, source_id]),
                ):
                    key = json.dumps(values, separators=(",", ":"))
                    conn.execute(
                        "DELETE FROM shared_sync_journal WHERE kind=? AND entity_key=?", (kind, key)
                    )
                    conn.execute(
                        "DELETE FROM shared_sync_heads WHERE kind=? AND entity_key=?", (kind, key)
                    )


def test_mysql_context_record_snapshot_uses_current_valid_sources(mysql_access_factory):
    factory = mysql_access_factory
    suffix = uuid4().hex
    session_id = f"context-source-{suffix}"
    store = ContextRecordStore(factory)
    rows = [
        (session_id, 1, "user", "Synthetic source quote", "2026-10-30T00:00:01+00:00", "turn-a"),
        (
            session_id,
            2,
            "agent",
            "Synthetic assistant reply",
            "2026-10-30T00:00:02+00:00",
            "turn-a",
        ),
        (session_id, 3, "user", "Uncovered new message", "2026-10-30T00:00:03+00:00", "turn-b"),
    ]
    with factory() as conn:
        conn.execute(
            "INSERT INTO agent_sessions(session_id,title,status,kind,creation_source,metadata,created_at,updated_at) "
            "VALUES(?,?,'active','ordinary','user','{}',?,?)",
            (session_id, "Synthetic MySQL context snapshot", rows[0][4], rows[-1][4]),
        )
        conn.executemany(
            "INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) "
            "VALUES(?,?,?,?,?,?)",
            rows,
        )
        conn.execute(
            "INSERT INTO context_record_heads VALUES(?,1,2,NULL,?)", (session_id, rows[-1][4])
        )
    try:
        empty = store.snapshot(session_id, include_history=False)
        assert [row["seq"] for row in empty["messages"]] == [3]
        assert empty["source_digest"] is None

        source = {
            "seq": 1,
            "quote": rows[0][3],
            "message_sha256": hashlib.sha256(rows[0][3].encode()).hexdigest(),
            "role": rows[0][2],
            "trace_id": rows[0][5],
            "created_at": rows[0][4],
        }
        record = {
            "record_id": f"ctx-{suffix}",
            "sources": [source, dict(source)],
            "revision": 1,
            "kind": "intent",
            "subject": "conversation",
            "topic": "synthetic",
            "content": "Synthetic current conversation direction",
            "evidence": "user_statement",
            "validity": "current",
            "task_status": None,
            "last_source_seq": 1,
            "priority": 3,
            "related_ids": [],
        }
        with factory() as conn:
            conn.execute(
                "INSERT INTO context_records VALUES(?,?,1,?)",
                (session_id, record["record_id"], json.dumps(record)),
            )
        included = store.snapshot(session_id, include_history=False)
        assert [row["seq"] for row in included["messages"]] == [1, 3]
        assert [
            row["seq"] for row in store.snapshot(session_id, include_history=True)["messages"]
        ] == [1, 2, 3]

        sessions = SessionService(factory)
        sessions.atomic_context_store = store
        sessions.atomic_context_mode = "active"
        sessions.atomic_context_profile = "intent"
        prompt = sessions.get_prompt_context_window(session_id=session_id, token_budget=2048)
        assert "Synthetic current conversation direction" in prompt.summary
        assert prompt.summary_metadata["state_error"] is None
        assert prompt.summary_metadata["projection_revision"] == 1

        with factory() as conn:
            conn.execute(
                "UPDATE agent_session_context_messages SET content=? WHERE session_id=? AND seq=1",
                ("withdrawn source", session_id),
            )
        withdrawn_prompt = sessions.get_prompt_context_window(
            session_id=session_id, token_budget=2048
        )
        assert "Synthetic current conversation direction" not in withdrawn_prompt.summary
        assert withdrawn_prompt.summary_metadata["state_error"] == "ContextRecordConflict"
        with factory() as conn:
            conn.execute(
                "UPDATE agent_session_context_messages SET content=? WHERE session_id=? AND seq=1",
                (rows[0][3], session_id),
            )

        mutations = [
            (
                "content",
                "UPDATE agent_session_context_messages SET content=? WHERE session_id=? AND seq=1",
                "edited source",
            ),
            (
                "role",
                "UPDATE agent_session_context_messages SET role=? WHERE session_id=? AND seq=1",
                "agent",
            ),
            (
                "trace",
                "UPDATE agent_session_context_messages SET trace_id=? WHERE session_id=? AND seq=1",
                "turn-changed",
            ),
            (
                "time",
                "UPDATE agent_session_context_messages SET created_at=? WHERE session_id=? AND seq=1",
                "2026-10-30T01:00:00+00:00",
            ),
        ]
        for _label, sql, replacement in mutations:
            with factory() as conn:
                conn.execute(sql, (replacement, session_id))
            with pytest.raises(ContextRecordConflict, match="changed or was withdrawn"):
                store.snapshot(session_id, include_history=False)
            with factory() as conn:
                conn.execute(
                    "UPDATE agent_session_context_messages SET role=?,content=?,created_at=?,trace_id=? "
                    "WHERE session_id=? AND seq=1",
                    (rows[0][2], rows[0][3], rows[0][4], rows[0][5], session_id),
                )
        with factory() as conn:
            conn.execute(
                "UPDATE agent_session_context_messages SET seq=10 WHERE session_id=? AND seq=1",
                (session_id,),
            )
        with pytest.raises(ContextRecordConflict, match="changed or was withdrawn"):
            store.snapshot(session_id, include_history=False)
        with factory() as conn:
            conn.execute(
                "UPDATE agent_session_context_messages SET seq=1 WHERE session_id=? AND seq=10",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM agent_session_context_messages WHERE session_id=? AND seq=1",
                (session_id,),
            )
        with pytest.raises(ContextRecordConflict, match="changed or was withdrawn"):
            store.snapshot(session_id, include_history=False)
    finally:
        with closing(factory()) as conn, conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM context_record_events WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM context_records WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM context_record_heads WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM context_record_pending WHERE session_id=?", (session_id,))
            conn.execute(
                "DELETE FROM agent_session_context_messages WHERE session_id=?", (session_id,)
            )
            conn.execute("DELETE FROM agent_sessions WHERE session_id=?", (session_id,))


def test_mysql_management_guard_snapshot_tracks_live_delegate_and_queue_relations(runtime):  # noqa: F811
    suffix = uuid4().hex
    stamp = "2026-10-30T00:00:00+00:00"
    manager = runtime.create_session(title="Synthetic guard owner " + suffix).session.session_id
    task = runtime.create_session(title="Synthetic guard task " + suffix).session.session_id
    task_run, manager_run = "guard-task-" + suffix, "guard-manager-" + suffix

    def insert_run(conn, run_id, session_id):
        payload = json.dumps(
            {"run_id": run_id, "session_id": session_id, "status": "queued", "metadata": {}}
        )
        conn.execute(
            "INSERT INTO agent_runs VALUES(?,?,?, ?,?,?)",
            (run_id, session_id, "queued", payload, stamp, stamp),
        )

    try:
        with runtime._conn() as conn:
            insert_run(conn, task_run, task)
            insert_run(conn, manager_run, manager)
            assert runtime.management_coordinator._run_guard_snapshot(conn, task_run)[1] == {
                "is_delegate": False,
                "has_delegate_owner": False,
                "is_queued": False,
            }
            conn.execute(
                "INSERT INTO manager_delegates(delegate_id,session_id,parent_session_id,objective,allowed_tools,"
                "workspace,kickoff_envelope,state,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,NULL,?,'active',1,?,?)",
                (
                    "delegate-" + suffix,
                    manager,
                    manager,
                    "synthetic task",
                    "[]",
                    "{}",
                    stamp,
                    stamp,
                ),
            )
            conn.execute(
                "INSERT INTO managed_session_links(manager_session_id,task_session_id,run_id,command_id,created_at) "
                "VALUES(?,?,?,?,?)",
                (manager, task, task_run, "command-" + suffix, stamp),
            )
            conn.execute(
                "INSERT INTO manager_work_queue(queue_id,manager_session_id,command_id,run_id,matter_id,matter_revision,"
                "priority,deadline,claim_keys,state,revision,issue,created_at,updated_at) "
                "VALUES(?,?,?, ?,NULL,NULL,'normal',NULL,'[]','queued',1,NULL,?,?)",
                ("queue-" + suffix, manager, "queue-command-" + suffix, task_run, stamp, stamp),
            )
            task_flags = runtime.management_coordinator._run_guard_snapshot(conn, task_run)[1]
            assert task_flags["has_delegate_owner"] and task_flags["is_queued"]
            assert runtime.management_coordinator._run_guard_snapshot(conn, manager_run)[1][
                "is_delegate"
            ]
            conn.execute("DELETE FROM manager_work_queue WHERE queue_id=?", ("queue-" + suffix,))
            conn.execute(
                "DELETE FROM managed_session_links WHERE manager_session_id=? AND command_id=?",
                (manager, "command-" + suffix),
            )
            conn.execute(
                "DELETE FROM manager_delegates WHERE delegate_id=?", ("delegate-" + suffix,)
            )
            task_flags = runtime.management_coordinator._run_guard_snapshot(conn, task_run)[1]
            assert task_flags == {
                "is_delegate": False,
                "has_delegate_owner": False,
                "is_queued": False,
            }
            assert (
                runtime.management_coordinator._run_guard_snapshot(conn, manager_run)[1][
                    "is_delegate"
                ]
                is False
            )
    finally:
        with runtime._conn() as conn:
            conn.execute(
                "DELETE FROM manager_work_queue WHERE run_id IN (?,?)", (task_run, manager_run)
            )
            conn.execute(
                "DELETE FROM managed_session_links WHERE manager_session_id=? OR task_session_id=?",
                (manager, task),
            )
            conn.execute(
                "DELETE FROM manager_delegates WHERE session_id=? OR parent_session_id=?",
                (manager, manager),
            )
            conn.execute("DELETE FROM agent_runs WHERE run_id IN (?,?)", (task_run, manager_run))
            conn.execute(
                "DELETE FROM session_admissions WHERE session_id IN (?,?)",
                (manager, task),
            )
            conn.execute("DELETE FROM agent_sessions WHERE session_id IN (?,?)", (manager, task))
