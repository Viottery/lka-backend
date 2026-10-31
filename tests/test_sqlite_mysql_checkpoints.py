from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

from scripts.import_sqlite_mysql_checkpoints import (
    CheckpointImportError,
    _iter_checkpoints_by_source_key,
    _target_empty,
    _validate_checkpoint_lengths,
    migrate,
    validate_source_schema,
)


def _source() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    from langgraph.checkpoint.sqlite import SqliteSaver

    saver = SqliteSaver(connection)
    saver.setup()
    saver.put({"configurable": {"thread_id": "thread-a", "checkpoint_ns": "ns-a"}}, {
        "v": 1, "id": "same-id", "ts": "2026-10-30T00:00:00Z",
        "channel_values": {"count": 1, "object": {"nested": [1, 2]}, "bytes": b"\x00\xff"},
        "channel_versions": {"count": 1, "object": "0001", "bytes": "0001"}, "versions_seen": {},
    }, {"source": "input", "step": -1}, {})
    saver.put({"configurable": {"thread_id": "thread-a", "checkpoint_ns": "ns-a", "checkpoint_id": "same-id"}}, {
        "v": 1, "id": "next-id", "ts": "2026-10-30T00:01:00Z",
        "channel_values": {"count": 2, "object": {"nested": [3]}, "bytes": b"\x10\x20"},
        "channel_versions": {"count": 2, "object": "0002", "bytes": "0002"}, "versions_seen": {},
    }, {"source": "loop", "step": 0}, {"count": 2, "object": "0002"})
    saver.put({"configurable": {"thread_id": "thread-a", "checkpoint_ns": "ns-b"}}, {
        "v": 1, "id": "same-id", "ts": "2026-10-30T00:02:00Z",
        "channel_values": {"count": 99}, "channel_versions": {"count": 1}, "versions_seen": {},
    }, {"source": "input", "step": -1}, {})
    connection.execute(
        "INSERT INTO writes VALUES(?,?,?,?,?,?,?,?)",
        ("thread-a", "ns-a", "next-id", "task-z", -17, "__error__", "json", b'{"e":1}'),
    )
    connection.execute(
        "INSERT INTO writes VALUES(?,?,?,?,?,?,?,?)",
        ("thread-a", "ns-a", "next-id", "task-z", 9, "custom", "msgpack", b"\x91\x02"),
    )
    connection.execute(
        "INSERT INTO writes VALUES(?,?,?,?,?,?,?,?)",
        ("thread-a", "ns-a", "next-id", "task-a", 2, "custom", "msgpack", b"\x91\x03"),
    )
    connection.execute(
        "INSERT INTO writes VALUES(?,?,?,?,?,?,?,?)",
        ("thread-a", "ns-a", "next-id", "task-a", 5, "custom", "msgpack", b"\x91\x04"),
    )
    connection.execute(
        "INSERT INTO writes VALUES(?,?,?,?,?,?,?,?)",
        ("thread-a", "ns-a", "next-id", "task-b", 0, "__resume__", "json", b'"resume"'),
    )
    connection.commit()
    return connection


class _Cursor:
    def __init__(self, target):
        self.target = target
        self.db = target.db
        self.cursor = self.db.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cursor.close()

    def execute(self, sql, args=()):
        self.cursor = self.db.cursor()
        if sql.startswith("SELECT 1 FROM `checkpoints`"):
            self.cursor = self.target.cp.execute("SELECT 1 FROM checkpoints LIMIT 1")
            return self
        elif sql.startswith("SELECT 1 FROM `checkpoint_blobs`"):
            sql = "SELECT 1 FROM checkpoint_blobs LIMIT 1"
        elif sql.startswith("SELECT 1 FROM `checkpoint_writes`"):
            sql = "SELECT 1 FROM writes LIMIT 1"
        elif sql.startswith("INSERT INTO checkpoint_writes "):
            sql = "INSERT INTO writes(thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value) VALUES(?,?,?,?,?,?,?,?)"
            args = (args[0], args[1], args[3], args[4], args[5], args[6], args[7], args[8])
            self.target.cp.execute(sql, args)
        elif sql.startswith("SELECT thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,`blob` "):
            sql = "SELECT thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value FROM writes ORDER BY thread_id,checkpoint_ns,checkpoint_id,task_id,idx"
        elif sql.startswith("INSERT INTO lka_checkpoint_imports"):
            self.target.marker = args[1]
            self.cursor.execute("SELECT 1")
            return self
        self.cursor.execute(sql, args)
        return self

    def fetchone(self):
        return self.cursor.fetchone()

    def __iter__(self):
        return iter(self.cursor)


class _Target:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE checkpoint_blobs (dummy INTEGER)")
        self.db.execute("CREATE TABLE writes(thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value)")
        self.marker = None
        self.cp = sqlite3.connect(":memory:")
        from langgraph.checkpoint.sqlite import SqliteSaver
        self.saver = SqliteSaver(self.cp)
        self.saver.setup()

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.db.commit()


def test_source_schema_requires_exact_saver_tables():
    source = _source()
    try:
        validate_source_schema(source)
    finally:
        source.close()
    bad = sqlite3.connect(":memory:")
    bad.execute("CREATE TABLE checkpoints(x)")
    with pytest.raises(CheckpointImportError):
        validate_source_schema(bad)


def test_checkpoint_import_iterator_uses_primary_key_order_without_pending_writes(monkeypatch):
    source = _source()
    from langgraph.checkpoint.sqlite import SqliteSaver

    saver = SqliteSaver(source)
    saver.is_setup = True
    try:
        expected = list(saver.list(None))
        items = list(_iter_checkpoints_by_source_key(source, saver))
        keys = [
            (item.config["configurable"]["thread_id"],
             item.config["configurable"]["checkpoint_ns"],
             item.config["configurable"]["checkpoint_id"])
            for item in items
        ]
        assert keys == [
            ("thread-a", "ns-a", "next-id"),
            ("thread-a", "ns-a", "same-id"),
            ("thread-a", "ns-b", "same-id"),
        ]
        expected_by_key = {
            (item.config["configurable"]["thread_id"],
             item.config["configurable"]["checkpoint_ns"],
             item.config["configurable"]["checkpoint_id"]): item
            for item in expected
        }
        for item in items:
            key = tuple(item.config["configurable"][name]
                        for name in ("thread_id", "checkpoint_ns", "checkpoint_id"))
            reference = expected_by_key[key]
            assert item.checkpoint == reference.checkpoint
            assert item.metadata == reference.metadata
            assert item.parent_config == reference.parent_config
            assert item.pending_writes is None

        def forbidden_list(*_args, **_kwargs):
            raise AssertionError("migration iterator must not call SqliteSaver.list")

        monkeypatch.setattr(saver, "list", forbidden_list)
        assert len(list(_iter_checkpoints_by_source_key(source, saver))) == len(expected)

        query = (
            "SELECT thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,type,checkpoint,metadata "
            "FROM checkpoints ORDER BY thread_id,checkpoint_ns,checkpoint_id"
        )
        plan = [str(row[3]).upper() for row in source.execute("EXPLAIN QUERY PLAN " + query)]
        assert any("INDEX" in detail or "PRIMARY KEY" in detail for detail in plan)
        assert not any("TEMP B-TREE" in detail for detail in plan)
    finally:
        source.close()


def test_checkpoint_length_validation_is_streamed_without_saver_list(monkeypatch):
    source = _source()
    from langgraph.checkpoint.sqlite import SqliteSaver

    saver = SqliteSaver(source)
    saver.is_setup = True

    def forbidden_list(*_args, **_kwargs):
        raise AssertionError("length validation must not call SqliteSaver.list")

    monkeypatch.setattr(saver, "list", forbidden_list)
    try:
        _validate_checkpoint_lengths(source, {
            "thread_id": 150, "checkpoint_ns": 2000, "checkpoint_id": 150,
            "parent_checkpoint_id": 150, "task_id": 150, "channel": 150, "version": 150,
        }, saver)
    finally:
        source.close()


def test_checkpoint_metadata_legacy_text_storage_is_preserved():
    source = _source()
    from langgraph.checkpoint.sqlite import SqliteSaver

    source.execute("UPDATE checkpoints SET metadata=CAST(metadata AS TEXT)")
    source.commit()
    saver = SqliteSaver(source)
    saver.is_setup = True
    try:
        expected = list(saver.list(None))
        actual = list(_iter_checkpoints_by_source_key(source, saver))
        expected_by_key = {
            (item.config["configurable"]["thread_id"],
             item.config["configurable"]["checkpoint_ns"],
             item.config["configurable"]["checkpoint_id"]): item
            for item in expected
        }
        for item in actual:
            key = tuple(item.config["configurable"][name]
                        for name in ("thread_id", "checkpoint_ns", "checkpoint_id"))
            assert item.metadata == expected_by_key[key].metadata
    finally:
        source.close()


def test_import_preserves_history_namespace_typed_values_and_raw_write_indexes(monkeypatch):
    source = _source()
    target = _Target()
    from langgraph.checkpoint.sqlite import SqliteSaver

    class FakeMySQLSaver:
        def __init__(self, _connection):
            self.saver = target.saver
            self.serde = target.saver.serde

        def put(self, *args, **kwargs):
            return self.saver.put(*args, **kwargs)

        def get_tuple(self, *args, **kwargs):
            return self.saver.get_tuple(*args, **kwargs)

    module = ModuleType("langgraph.checkpoint.mysql.pymysql")
    module.PyMySQLSaver = FakeMySQLSaver
    monkeypatch.setitem(__import__("sys").modules, "langgraph.checkpoint.mysql.pymysql", module)
    limits = {
        "thread_id": 150, "checkpoint_ns": 2000, "checkpoint_id": 150,
        "parent_checkpoint_id": 150, "task_id": 150, "channel": 150, "version": 150,
    }
    monkeypatch.setattr("scripts.import_sqlite_mysql_checkpoints._target_limits", lambda _: limits)
    source_saver = SqliteSaver(source)
    source_saver.is_setup = True
    # Target checkpoint tables are checked through the fake raw target wrapper.
    try:
        result = migrate(source, target, run_id="0123456789abcdef0123456789abcdef")
        assert result == {
            "import_id": "0123456789abcdef0123456789abcdef",
            "status": "verified", "checkpoints": 3, "writes": 5,
        }
        for namespace in ("ns-a", "ns-b"):
            config = {"configurable": {"thread_id": "thread-a", "checkpoint_ns": namespace}}
            old = source_saver.get_tuple(config)
            new = target.saver.get_tuple(config)
            assert old is not None and new is not None
            assert old.config == new.config
            assert old.checkpoint == new.checkpoint
            assert old.metadata == new.metadata
        rows = target.db.execute("SELECT task_id,idx,channel,value FROM writes ORDER BY task_id,idx").fetchall()
        assert rows == [
            ("task-a", 2, "custom", b"\x91\x03"), ("task-a", 5, "custom", b"\x91\x04"),
            ("task-b", 0, "__resume__", b'"resume"'),
            ("task-z", -17, "__error__", b'{"e":1}'), ("task-z", 9, "custom", b"\x91\x02"),
        ]
        assert target.marker == "verified"
    finally:
        source.close()
        target.db.close()
        target.cp.close()


def test_real_mysql_checkpoint_import_roundtrip_once(tmp_path):
    """Opt-in CLI integration test; retains imported rows in its dedicated lab schema."""
    regular_file = os.environ.get("LKA_MYSQL_TEST_CONFIG")
    migration_file = os.environ.get("LKA_MYSQL_TEST_MIGRATION_CONFIG")
    if not regular_file or not migration_file:
        pytest.skip("real MySQL checkpoint lab credentials are not configured")

    import pymysql
    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
    from langgraph.checkpoint.sqlite import SqliteSaver

    regular = json.loads(Path(regular_file).read_text(encoding="utf-8"))
    migration = json.loads(Path(migration_file).read_text(encoding="utf-8"))
    same_database = (regular["host"], regular["port"], regular["database"]) == (
        migration["host"], migration["port"], migration["database"],
    )
    assert same_database, "lab runtime and migration connection files target different databases"
    source = _source()
    raw = None
    try:
        snapshot = tmp_path / "checkpoint-snapshot.sqlite3"
        with closing(sqlite3.connect(snapshot)) as backup:
            source.backup(backup)
        raw = pymysql.connect(
            host=regular["host"], port=regular["port"], user=regular["user"], password=regular["password"],
            database=regular["database"], charset="utf8mb4", autocommit=True,
            connect_timeout=int(regular.get("connect_timeout", 5)), read_timeout=int(regular.get("read_timeout", 30)),
            cursorclass=pymysql.cursors.Cursor,
        )

        # Drive the public CLI, including snapshot confirmation, coordinator
        # ownership, marker DDL credentials, preflight bootstrap and final verify.
        command = [
            sys.executable, "scripts/import_sqlite_mysql_checkpoints.py",
            "--source", str(snapshot), "--snapshot-confirmed",
            "--connection-file", regular_file,
            "--migration-connection-file", migration_file,
        ]
        source_saver = SqliteSaver(source)
        source_saver.is_setup = True
        try:
            _target_empty(raw)
            target_was_empty = True
        except CheckpointImportError:
            target_was_empty = False

        if not target_was_empty:
            first = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
            assert first.returncode != 0
            assert "prior checkpoint import" in first.stderr or "not empty" in first.stderr
            with raw.cursor() as cursor:
                cursor.execute("SELECT 1 FROM lka_checkpoint_imports WHERE status <> 'verified' LIMIT 1")
                assert cursor.fetchone() is None
            return

        first = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        assert first.returncode == 0, first.stderr
        result = json.loads(first.stdout)
        import_id = result["import_id"]
        assert result["status"] == "verified"
        assert result["checkpoints"] == 3
        assert result["writes"] == 5
        with raw.cursor() as cursor:
            cursor.execute("SELECT status,checkpoint_count,write_count FROM lka_checkpoint_imports WHERE import_id=%s", (import_id,))
            assert cursor.fetchone() == ("verified", 3, 5)

        target_saver = PyMySQLSaver(raw)
        for namespace in ("ns-a", "ns-b"):
            config = {"configurable": {"thread_id": "thread-a", "checkpoint_ns": namespace}}
            source_latest = source_saver.get_tuple(config)
            target_latest = target_saver.get_tuple(config)
            assert source_latest is not None and target_latest is not None
            assert target_latest.config == source_latest.config
            assert target_latest.checkpoint == source_latest.checkpoint
            assert target_latest.metadata == source_latest.metadata
        for item in source_saver.list(None):
            config = {
                "configurable": {
                    "thread_id": item.config["configurable"]["thread_id"],
                    "checkpoint_ns": item.config["configurable"]["checkpoint_ns"],
                    "checkpoint_id": item.config["configurable"]["checkpoint_id"],
                }
            }
            imported = target_saver.get_tuple(config)
            assert imported is not None
            assert imported.checkpoint == item.checkpoint
            assert imported.metadata == item.metadata
            assert imported.parent_config == item.parent_config
            assert imported.pending_writes == item.pending_writes

        second = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        assert second.returncode != 0
        assert "prior checkpoint import" in second.stderr
        with raw.cursor() as cursor:
            cursor.execute("SELECT status FROM lka_checkpoint_imports WHERE import_id=%s", (import_id,))
            assert cursor.fetchone() == ("verified",)
    finally:
        source.close()
        if raw is not None:
            raw.close()
