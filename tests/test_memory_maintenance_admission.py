"""Idle maintenance checks remain readable while another writer holds the DB."""
import pytest
from test_contextual_memory import runtime as runtime_fixture
from test_memory_consolidation import Organizer, add, proposal, run_maintenance

from app.storage.sqlite_timing import connect_sqlite
from app.storage.sqlite_write_queue import SqliteWriteAdmissionError, SqliteWriteQueueError


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("debounce, expected", [(60, "waiting"), (0, "already_scheduled")])
def test_unchanged_maintenance_does_not_request_write_permission(runtime, debounce, expected):
    add(runtime, "Prefer concise replies", "one")
    add(runtime, "Keep explanations clear", "two")
    coordinator = runtime.memory_background
    coordinator.consolidation_debounce_seconds = debounce
    coordinator.enqueue_maintenance()
    blocker = connect_sqlite(coordinator.db_path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        result = coordinator.enqueue_maintenance()
        assert result == {"status": expected, "job_id": None}
        # Explicit force still follows the original serialized enqueue path.
        with pytest.raises(SqliteWriteQueueError, match="nested"):
            coordinator.enqueue_maintenance(force=True)
    finally:
        blocker.rollback()
        blocker.close()


def test_changed_signature_still_updates_debounce_under_write_lock(runtime):
    add(runtime, "Prefer concise replies", "one")
    add(runtime, "Keep explanations clear", "two")
    coordinator = runtime.memory_background
    coordinator.consolidation_debounce_seconds = 60
    assert coordinator.enqueue_maintenance()["status"] == "waiting"
    with connect_sqlite(coordinator.db_path) as conn:
        before = conn.execute("SELECT observed_signature FROM memory_maintenance_state").fetchone()[0]
    conn.close()
    add(runtime, "Include evidence when useful", "three")
    assert coordinator.enqueue_maintenance()["status"] == "waiting"
    with connect_sqlite(coordinator.db_path) as conn:
        after = conn.execute("SELECT observed_signature FROM memory_maintenance_state").fetchone()[0]
    conn.close()
    assert after != before


def test_merge_admission_retry_keeps_generated_proposal_without_calling_model_again(runtime, monkeypatch):
    first = add(runtime, "日常交流希望语气自然。", "one")
    second = add(runtime, "平时聊天少一些正式措辞。", "two")
    connect = runtime.memory_service._connect
    failures = []

    def arm_publication_fault():
        def fault_connection():
            conn = connect()
            execute = conn.execute

            def execute_once(sql, *args, **kwargs):
                if sql == "BEGIN IMMEDIATE" and not failures:
                    failures.append("before_merge")
                    raise SqliteWriteAdmissionError("wait_timeout")
                return execute(sql, *args, **kwargs)

            conn.execute = execute_once
            return conn
        monkeypatch.setattr(runtime.memory_service, "_connect", fault_connection)

    client = Organizer(
        lambda _: [proposal([first, second], "日常交流语气自然、少一些正式措辞。")],
        hook=arm_publication_fault,
    )
    job = run_maintenance(runtime, client)
    assert job["status"] == "succeeded"
    assert failures == ["before_merge"]
    assert len(client.prompts) == 1
    assert len(runtime.memory_service.list()) == 1
