"""Real process exits, not exceptions that automatically roll transactions back."""

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from app.core.background_jobs import BackgroundJobStore
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput


def _child(db, phase):
    code = """
import json, os, sqlite3, sys
from app.core.background_jobs import BackgroundJobStore
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput
db, phase = sys.argv[1:]
store = BackgroundJobStore(db)
if phase == 'before_commit':
    conn = sqlite3.connect(db)
    conn.execute('BEGIN IMMEDIATE')
    conn.execute("INSERT INTO raw_events VALUES('uncommitted')")
    store.enqueue_grouped('memory_extract', 's', 'answer', conn=conn)
    os._exit(7)
job = store.enqueue('memory_extract', 's', 'answer', {'message_id':'answer'})
if phase == 'after_enqueue':
    os._exit(7)
claimed = store.claim('crashed-worker', 60)
if phase == 'after_publication':
    memory = MemoryService(db)
    source = memory.register_source(MemorySourceInput(source_type='user_message', source_ref='msg', trusted_source=True))
    memory.create(MemoryInput(content='回答提供来源', source_id=source),
                  publication_lease=(claimed['job_id'], 'crashed-worker', claimed['lease_epoch']))
print(json.dumps(claimed), flush=True)
os._exit(7)
"""
    result = subprocess.run([sys.executable, "-c", code, str(db), phase], capture_output=True,
                            text=True, timeout=10, check=False)
    assert result.returncode == 7, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def _stores(tmp_path):
    db = tmp_path / "crash.sqlite3"
    memory = MemoryService(db)
    memory.ensure_schema()
    store = BackgroundJobStore(db)
    store.ensure_schema()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE raw_events(value TEXT)")
    return db, memory, store


def test_process_exit_before_commit_has_no_partial_outbox(tmp_path):
    db, _, store = _stores(tmp_path)
    _child(db, "before_commit")
    assert store.list() == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 0


def test_process_exit_after_enqueue_leaves_claimable_committed_job(tmp_path):
    db, _, store = _stores(tmp_path)
    _child(db, "after_enqueue")
    job = store.claim("restart", 60)
    assert job is not None and job["status"] == "running"
    assert store.complete(job["job_id"], "restart", job["lease_epoch"])


def test_two_live_processes_cannot_claim_same_job(tmp_path):
    db, _, store = _stores(tmp_path)
    store.enqueue("memory_extract", "s", "answer", {"message_id": "answer"})
    code = """
import json, sys
from app.core.background_jobs import BackgroundJobStore
print('ready', flush=True)
sys.stdin.readline()
job = BackgroundJobStore(sys.argv[1]).claim(sys.argv[2], 60)
print(json.dumps(job), flush=True)
"""
    children = [subprocess.Popen([sys.executable, "-c", code, str(db), owner], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for owner in ("process-a", "process-b")]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("claim\n")
            child.stdin.flush()
        results = [child.communicate(timeout=10) for child in children]
        assert all(child.returncode == 0 for child in children), results
        claims = [json.loads(out) for out, _ in results]
        assert sum(claim is not None for claim in claims) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()


def test_sqlite_backup_restores_memory_versions_sources_and_pending_jobs(tmp_path):
    db, memory, store = _stores(tmp_path)
    source = memory.register_source(MemorySourceInput(source_type="user_message", source_ref="original", trusted_source=True))
    record = memory.create(MemoryInput(content="先给结论", source_id=source))
    job = store.enqueue("memory_extract", "s", "later", {"message_id": "later"})
    backup = tmp_path / "restored.sqlite3"
    with sqlite3.connect(db) as original, sqlite3.connect(backup) as restored:
        original.backup(restored)
    restored_memory = MemoryService(backup)
    restored_memory.ensure_schema()
    restored_memory.ensure_schema()
    assert restored_memory.get(record.memory_id) == record
    assert restored_memory.sources_for(record.memory_id)[0]["source_ref"] == "original"
    restored_store = BackgroundJobStore(backup)
    restored_store.ensure_schema()
    claim = restored_store.claim("restored", 30)
    assert claim["job_id"] == job["job_id"]


def test_process_exit_during_work_or_after_publication_is_fenced_and_idempotent(tmp_path):
    for phase in ("during_model_work", "after_publication"):
        directory = tmp_path / phase
        directory.mkdir()
        db, memory, store = _stores(directory)
        old = _child(db, phase)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE background_jobs SET lease_expires_at=? WHERE job_id=?",
                         ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), old["job_id"]))
        new = store.claim("restart", 60)
        assert new["lease_epoch"] > old["lease_epoch"]
        assert not store.heartbeat(old["job_id"], "crashed-worker", old["lease_epoch"], lease_seconds=60)
        assert not store.complete(old["job_id"], "crashed-worker", old["lease_epoch"])
        source = memory.register_source(MemorySourceInput(source_type="user_message", source_ref="msg", trusted_source=True))
        record = memory.create(MemoryInput(content="回答提供来源", source_id=source),
                               publication_lease=(new["job_id"], "restart", new["lease_epoch"]))
        assert record.version == 1
        assert len(memory.list(scope="global")) == len(memory.events(record.memory_id)) == 1
        assert store.complete(new["job_id"], "restart", new["lease_epoch"])
