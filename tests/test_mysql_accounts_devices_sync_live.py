"""Actual InnoDB race tests for account, device and shared-sync invariants.

Opt in with ``LKA_MYSQL_TEST_CONFIG`` pointing to a dedicated test/lab database.
Rows are uniquely named synthetic records; deletions target only test-created rows.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4

import pytest

from app.domains.accounts import AccountConfig, AccountError, AccountService
from app.domains.devices import (
    DeviceConfig,
    DeviceConflictError,
    DeviceEnrollment,
    DeviceService,
)
from app.domains.shared_sync import SharedSyncConfig, SharedSyncService
from app.domains.shared_sync_mysql import mysql_entity_key
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.mysql import close_mysql_pools
from tests.test_mysql_owner_live import _config


@pytest.fixture(scope="module")
def mysql_runtime():
    config = _config()
    identity = Path(gettempdir()) / ("lka_mysql_domain_test_" + uuid4().hex + ".sqlite3")
    configure_mysql(identity, config)
    try:
        yield config, lambda: connect_database(identity)
    finally:
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)


def test_first_admin_bootstrap_is_one_winner(mysql_runtime):
    _config, factory = mysql_runtime
    service = AccountService(factory, AccountConfig(enabled=True))
    bootstrap_key = "__lka_account_bootstrap__"
    with factory() as conn:
        conn.execute_native(
            "INSERT IGNORE INTO auth_login_throttle VALUES(%s,%s,17)",
            (bootstrap_key, 123),
        )
        bootstrap_before = conn.execute(
            "SELECT window_start,attempts FROM auth_login_throttle WHERE throttle_key=?",
            (bootstrap_key,),
        ).fetchone()
    with factory() as conn:
        account_count = conn.execute("SELECT COUNT(*) FROM auth_accounts").fetchone()[0]
    username = "lka" + uuid4().hex[:24]
    barrier = threading.Barrier(2, timeout=10)

    def create():
        barrier.wait()
        try:
            return service.create_admin(username, "synthetic live password 123")
        except AccountError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: create(), range(2)))
    if account_count:
        # Preserve the lab's existing account; both contenders must observe
        # the bootstrap closure while serializing on the reserved anchor row.
        assert results == [409, 409]
    else:
        assert sum(isinstance(item, dict) for item in results) == 1
        assert sorted(item for item in results if isinstance(item, int)) == [409]
    with factory() as conn:
        bootstrap_after = conn.execute(
            "SELECT window_start,attempts FROM auth_login_throttle WHERE throttle_key=?",
            (bootstrap_key,),
        ).fetchone()
    assert tuple(bootstrap_after) == tuple(bootstrap_before)


def test_login_throttle_reservation_is_atomic(mysql_runtime):
    _config, factory = mysql_runtime
    service = AccountService(
        factory,
        AccountConfig(enabled=True, login_max_attempts=1, login_window_seconds=900),
    )
    username = "missing" + uuid4().hex[:20]
    source = "test-source-" + uuid4().hex
    now = int(time.time())
    existing_keys = (
        hashlib.sha256(("user:" + username).encode()).hexdigest(),
        hashlib.sha256(("source:" + source).encode()).hexdigest(),
    )
    with factory() as conn:
        for key in existing_keys:
            conn.execute_native(
                "INSERT IGNORE INTO auth_login_throttle VALUES(%s,%s,0)",
                (key, now),
            )
    barrier = threading.Barrier(2, timeout=10)

    def attempt():
        barrier.wait()
        try:
            service.login(username, "synthetic wrong password", source)
        except AccountError as exc:
            return exc.status_code
        raise AssertionError("an unknown account unexpectedly authenticated")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: attempt(), range(2)))
    assert sorted(results) == [401, 429]
    with factory() as conn:
        attempts = [conn.execute(
            "SELECT attempts FROM auth_login_throttle WHERE throttle_key=?", (key,),
        ).fetchone()[0] for key in existing_keys]
    assert attempts == [1, 1]


def test_device_credential_rotation_uses_revision_cas(mysql_runtime):
    _config, factory = mysql_runtime
    service = DeviceService(factory, DeviceConfig(enabled=True))
    device_id = "lka-test-" + uuid4().hex[:24]
    enrolled = service.enroll(DeviceEnrollment(
        device_id=device_id,
        name="Synthetic MySQL concurrency test",
        platform="linux",
        environment_id="test",
    ))
    barrier = threading.Barrier(2, timeout=10)

    def rotate():
        barrier.wait()
        try:
            return service.rotate(device_id, 1)
        except DeviceConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: rotate(), range(2)))
    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert service.authenticate(enrolled["token"]) is None
    assert service.authenticate(winners[0]["token"])["credential_version"] == 2
    assert service.get(device_id)["revision"] == 2


def test_shared_sync_journal_sequences_follow_commit_order(mysql_runtime):
    _config, factory = mysql_runtime
    service = SharedSyncService(
        factory, SharedSyncConfig(enabled=True, role="hub"),
    )
    before = service.status()["cursor"]
    suffix = uuid4().hex
    first, second = "lka-sync-a-" + suffix, "lka-sync-b-" + suffix
    first_inserted = threading.Event()
    release_first = threading.Event()
    second_inserted = threading.Event()

    def write_first():
        with factory() as conn:
            conn.execute("INSERT INTO memory_projects(project_id,created_at) VALUES(?,?)",
                         (first, "synthetic"))
            first_inserted.set()
            assert release_first.wait(10)

    def write_second():
        assert first_inserted.wait(10)
        with factory() as conn:
            conn.execute("INSERT INTO memory_projects(project_id,created_at) VALUES(?,?)",
                         (second, "synthetic"))
            second_inserted.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_job = pool.submit(write_first)
        try:
            assert first_inserted.wait(10)
            second_job = pool.submit(write_second)
            time.sleep(0.15)
            assert not second_inserted.is_set(), "journal append bypassed the held anchor row"
            release_first.set()
            first_job.result(timeout=10)
            second_job.result(timeout=10)
        finally:
            release_first.set()

    page = service.pull(after=before, limit=100)
    events = [event for event in page["changes"] if event["key"] in ([first], [second])]
    assert [event["key"] for event in events] == [[first], [second]]
    assert events[0]["sequence"] < events[1]["sequence"]


def test_shared_sync_entity_keys_match_sqlite_and_tombstones(mysql_runtime):
    _config, factory = mysql_runtime
    service = SharedSyncService(factory, SharedSyncConfig(enabled=True, role="hub"))
    before = service.status()["cursor"]
    expression = mysql_entity_key(
        "memory_entry_sources", ("memory_id", "source_id"), null_safe=True,
    )
    with factory() as conn:
        for values in (("id with space 雪", 'path\\with\\"quote'), (None, "tail ")):
            sqlite_key = sqlite3.connect(":memory:").execute(
                "SELECT json_array(?,?)", values,
            ).fetchone()[0]
            mysql_key = conn.execute(
                f"SELECT {expression} FROM (SELECT ? AS memory_id, ? AS source_id) AS synthetic",
                values,
            ).fetchone()[0]
            assert mysql_key == sqlite_key

    suffix = uuid4().hex
    project_id = "lka-tombstone-" + suffix
    source_id, memory_id = "lka-source-" + suffix, "lka-memory-" + suffix
    now = "2026-10-29T00:00:00+00:00"
    with factory() as conn:
        conn.execute("INSERT INTO memory_projects(project_id,created_at) VALUES(?,?)",
                     (project_id, now))
        conn.execute(
            "INSERT INTO memory_sources(source_id,source_type,source_ref,checksum,excerpt_start,excerpt_end,"
            "trusted_source,status,expires_at,metadata,created_at,updated_at) "
            "VALUES(?,?,?,NULL,NULL,NULL,1,'active',NULL,'{}',?,?)",
            (source_id, "user", suffix, now, now),
        )
        conn.execute(
            "INSERT INTO memory_entries(memory_id,memory_type,content,scope,project_id,status,confidence,"
            "sensitivity,expires_at,dedupe_key,version,supersedes_id,extraction_model,metadata,created_at,updated_at) "
            "VALUES(?, 'preference','synthetic','global',NULL,'active',0.5,'normal',NULL,?,1,NULL,NULL,'{}',?,?)",
            (memory_id, suffix, now, now),
        )
        conn.execute("INSERT INTO memory_entry_sources(memory_id,source_id) VALUES(?,?)",
                     (memory_id, source_id))
        before_version = conn.execute(
            "SELECT version FROM shared_sync_heads WHERE kind='memory_entries' AND entity_key=?",
            ('["' + memory_id + '"]',),
        ).fetchone()[0]
        conn.execute("UPDATE memory_entries SET updated_at=? WHERE memory_id=?", ("changed", memory_id))
        after_version = conn.execute(
            "SELECT version FROM shared_sync_heads WHERE kind='memory_entries' AND entity_key=?",
            ('["' + memory_id + '"]',),
        ).fetchone()[0]
        assert after_version == before_version + 1
        conn.execute("UPDATE memory_sources SET status='revoked' WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM memory_projects WHERE project_id=?", (project_id,))

    changes = service.pull(after=before, limit=500)["changes"]
    source_revocation = [item for item in changes
                         if item["kind"] == "memory_entries" and item["key"] == [memory_id]]
    tombstone = [item for item in changes
                 if item["kind"] == "memory_projects" and item["key"] == [project_id]]
    assert source_revocation and all(item["deleted"] for item in source_revocation)
    assert tombstone and tombstone[-1]["deleted"]
