"""Offline durable queue admission, scheduling, reservation, and fencing contracts."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError

from app.core.agent_runs import AgentRunStatus
from app.domains.management import ManagementConflictError
from app.domains.management_queue import ManagementQueueService, QueueWorkInput
from app.domains.matters import MatterUpdateInput
from tests.test_management_assignments import state as assignment_state
from tests.test_management_tasks import manager_context as manager_fixture
from tests.test_management_work import count, create


@pytest.fixture
def state(tmp_path, monkeypatch):
    generator = assignment_state.__wrapped__(tmp_path, monkeypatch)
    runtime, context, work, assignments = next(generator)
    queue = ManagementQueueService(runtime._conn, tasks=runtime.managed_tasks, assignments=assignments,
                                   dispatcher=runtime.agent_dispatch, config=runtime.local_app_config.management)
    queue.ensure_schema()
    yield runtime, context, work, assignments, queue
    try:
        next(generator)
    except StopIteration:
        pass


def enqueue(state, command="q", **kwargs):
    _, context, _, _, queue = state
    return queue.queue_work(context, command_id=command, purpose="Read bounded source evidence", **kwargs)


def row(state, receipt):
    with closing(state[0]._conn()) as conn:
        return dict(conn.execute("SELECT * FROM manager_work_queue WHERE queue_id=?", (receipt["queue_id"],)).fetchone())


def status(state, receipt, value):
    state[0].agent_run_manager._update_run(receipt["run_id"], status=AgentRunStatus(value))


def test_admission_optional_matter_idempotent_reopen_and_original_authority(state):
    runtime, context, _, _, _ = state
    metadata = {"work_budget_scopes": [{"scope_id": "test-budget", "max_calls": 2, "max_tokens": 90}],
                "manager_cause_paths": {"source": 2}}
    runtime.agent_run_manager._update_run(context.run_id, status=AgentRunStatus.RUNNING, metadata_patch=metadata)
    first = enqueue(state)
    assert count(runtime, "background_jobs") == 0
    assert count(runtime, "manager_work_queue") == 1
    assert enqueue(state) == first
    reopened = ManagementQueueService(runtime._conn, tasks=runtime.managed_tasks, assignments=state[3],
                                      dispatcher=runtime.agent_dispatch, config=runtime.local_app_config.management)
    reopened.ensure_schema()
    assert reopened.queue_work(context, command_id="q", purpose="Read bounded source evidence") == first
    record = runtime.agent_run_store.load_run(first["run_id"])
    assert record["parent_run_id"] is None and record["session_id"] != context.session_id
    assert record["metadata"]["work_budget_scopes"] == metadata["work_budget_scopes"]
    assert record["metadata"]["manager_cause_paths"] == metadata["manager_cause_paths"]
    assert record["metadata"]["input_envelope"]["original_user_message_refs"] == ["server-original"]
    with pytest.raises(ManagementConflictError, match="different arguments"):
        enqueue(state, priority="urgent")
    status(state, {"run_id": context.run_id}, "completed")
    assert reopened.tick() == 1
    assert count(runtime, "background_jobs") == 1
    assert reopened.tick() == 0
    assert runtime.agent_run_store.load_run(first["run_id"])["metadata"]["work_budget_scopes"] == metadata["work_budget_scopes"]


def test_schema_rejects_naive_deadline_unpaired_matter_and_unsafe_write_declarations():
    invalid = [{"deadline": "2026-10-20T00:00:00"}, {"matter_id": "m"}, {"expected_revision": 1},
               {"read_only_work": False}, {"write_resources": ["shared"]}, {"priority": "critical"},
               {"read_only_work": False, "workspace_path": "/repo", "write_resources": [" bad"]}]
    for extra in invalid:
        with pytest.raises(ValidationError):
            QueueWorkInput(command_id="x", purpose="Read", **extra)
    assert QueueWorkInput(command_id="x", purpose="Read", deadline="2000-01-01T08:00:00+08:00").deadline == "2000-01-01T00:00:00+00:00"


def test_concurrent_admission_and_ticks_dispatch_once(state):
    runtime, _, _, _, queue = state
    barrier = Barrier(2)

    def admit(_):
        barrier.wait()
        return enqueue(state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(admit, range(2)))
    assert a == b and count(runtime, "manager_work_queue") == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: queue.tick(), range(2))) == 1
    assert count(runtime, "background_jobs") == 1


def test_preparation_has_no_write_lock_and_dispatch_failure_rolls_back_release(state, tmp_path, monkeypatch):
    runtime, _, _, _, queue = state
    original = runtime.managed_tasks.workspace

    def workspace(path):
        with closing(runtime._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
        return original(path)

    monkeypatch.setattr(runtime.managed_tasks, "workspace", workspace)
    first = enqueue(state, read_only_work=False, workspace_path=str(tmp_path))
    original_enqueue = runtime.agent_dispatch.enqueue

    def broken(*args, **kwargs):
        original_enqueue(*args, **kwargs)
        raise RuntimeError("injected dispatch failure")

    monkeypatch.setattr(runtime.agent_dispatch, "enqueue", broken)
    with pytest.raises(RuntimeError, match="injected"):
        queue.tick()
    assert row(state, first)["state"] == "pending"
    assert count(runtime, "manager_work_claims") == count(runtime, "background_jobs") == 0
    monkeypatch.setattr(runtime.agent_dispatch, "enqueue", original_enqueue)
    assert queue.tick() == 1


def test_assignment_failure_rolls_back_entire_admission(state, monkeypatch):
    runtime, _, _, assignments, _ = state
    before = {table: count(runtime, table) for table in ("agent_runs", "agent_sessions", "management_commands", "managed_session_links")}
    monkeypatch.setattr(assignments, "_matter", lambda *a, **kw: (_ for _ in ()).throw(ManagementConflictError("stale")))
    with pytest.raises(ManagementConflictError):
        enqueue(state, matter_id="missing", expected_revision=1)
    assert all(count(runtime, table) == value for table, value in before.items())
    assert count(runtime, "manager_work_queue") == 0


def test_dependency_requires_published_verified_completion_then_auto_releases(state):
    runtime, context, work, assignments, queue = state
    refs = work.organize_work(context, "graph", [create("dep"), create("work"),
         {"op": "link", "source": "work", "target": "dep", "relation": "depends_on"}])["refs"]
    first = enqueue(state, matter_id=refs["work"], expected_revision=1)
    assert queue.tick() == 0
    assert assignments.result_view(context, refs["work"])["assignments"][0]["run"]["run_id"] == first["run_id"]
    dep = runtime.matter_service.update_matter(matter_id=refs["dep"], payload=MatterUpdateInput(expected_revision=1, status="done"))
    assert queue.tick() == 0
    with closing(runtime._conn()) as conn, conn:
        conn.execute("INSERT INTO manager_work_reviews VALUES(?,?,?,?,?,?,'accepted','[]','Verified',?)",
                     (context.session_id, "verified", dep.matter_id, "review-run", 1, dep.revision, datetime.now(UTC).isoformat()))
    assert queue.tick() == 1
    assert row(state, first)["state"] == "released"


def test_matter_revision_change_blocks_without_rebinding(state):
    runtime, context, work, _, queue = state
    mid = work.organize_work(context, "new", [create()])["refs"]["a"]
    first = enqueue(state, matter_id=mid, expected_revision=1)
    runtime.matter_service.update_matter(matter_id=mid, payload=MatterUpdateInput(expected_revision=1, summary="New requirement"))
    assert queue.tick() == 0
    assert row(state, first)["state"] == "blocked"
    assert row(state, first)["issue"] == "work_source_changed"
    assert count(runtime, "agent_runs") == 2


def test_priority_deadline_and_unbounded_aging(state, monkeypatch):
    runtime, _, _, _, queue = state
    now = datetime.now(UTC)
    low = enqueue(state, "low", priority="low")
    urgent = enqueue(state, "urgent", priority="urgent")
    normal = enqueue(state, "overdue", deadline=(now - timedelta(days=1)).isoformat())
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE manager_work_queue SET created_at=? WHERE queue_id=?",
                     ((now - timedelta(hours=8)).isoformat(), low["queue_id"]))
    order = record_release_order(queue, monkeypatch)
    assert queue.tick(now) == 3
    assert order == [item["run_id"] for item in (low, urgent, normal)]
    assert row(state, normal)["state"] == "released"  # Soft deadlines never expire work.


def test_soft_deadline_orders_equal_priority_work(state, monkeypatch):
    _, _, _, _, queue = state
    normal = enqueue(state, "none")
    soon = enqueue(state, "soon", deadline=(datetime.now(UTC) + timedelta(minutes=1)).isoformat())
    order = record_release_order(queue, monkeypatch)
    assert queue.tick() == 2
    assert order == [soon["run_id"], normal["run_id"]]


def record_release_order(queue, monkeypatch):
    order = []
    original = queue.dispatcher.enqueue

    def enqueue_run(run_id, **kwargs):
        order.append(run_id)
        return original(run_id, **kwargs)

    monkeypatch.setattr(queue.dispatcher, "enqueue", enqueue_run)
    return order


def test_persistent_manager_round_robin_overrides_one_managers_backlog(state, monkeypatch):
    runtime, _, _, assignments, queue = state
    enqueue(state, "a1", priority="urgent")
    enqueue(state, "a2", priority="urgent")
    assert queue.tick() == 2
    third = enqueue(state, "a3", priority="urgent")
    fourth = enqueue(state, "a4", priority="urgent")
    other_context = manager_fixture.__wrapped__(runtime)
    other = queue.queue_work(other_context, command_id="b", purpose="Other line", priority="low")
    reopened = ManagementQueueService(runtime._conn, tasks=runtime.managed_tasks, assignments=assignments,
                                      dispatcher=runtime.agent_dispatch, config=runtime.local_app_config.management)
    order = record_release_order(reopened, monkeypatch)
    assert reopened.tick() == 3
    assert order == [item["run_id"] for item in (other, third, fourth)]


def test_independent_queue_work_releases_despite_existing_dispatch(state):
    runtime, context, _, _, queue = state
    regular = runtime.managed_tasks.start_task(context, command_id="regular", purpose="Existing dispatch")
    queued = [enqueue(state, f"parallel-{index}") for index in range(4)]
    assert queue.tick() == 4
    assert all(row(state, item)["state"] == "released" for item in queued)
    assert runtime.agent_run_manager.get_run(regular["run_id"]).status == AgentRunStatus.QUEUED


def test_write_reservation_survives_wait_pause_and_releases_only_terminal(state, tmp_path):
    _, _, _, _, queue = state
    first = enqueue(state, "write1", read_only_work=False, workspace_path=str(tmp_path))
    second = enqueue(state, "write2", read_only_work=False, workspace_path=str(tmp_path), write_resources=["extra"])
    reader = enqueue(state, "read", workspace_path=str(tmp_path))
    assert queue.tick() == 2
    assert row(state, first)["state"] == row(state, reader)["state"] == "released"
    assert row(state, second)["state"] == "pending"
    status(state, first, "waiting_confirmation")
    assert queue.tick() == 0
    status(state, first, "paused")
    assert queue.tick() == 0
    queue.check_run(first["run_id"])
    status(state, first, "cancelled")
    assert queue.tick() == 1
    assert row(state, second)["state"] == "released"


def test_nested_workspace_and_explicit_resource_conflicts(state, tmp_path):
    _, _, _, _, queue = state
    sub = tmp_path / "nested"
    sub.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    first = enqueue(state, "outer", read_only_work=False, workspace_path=str(sub), write_resources=["project:common"])
    nested = enqueue(state, "parent", read_only_work=False, workspace_path=str(tmp_path))
    explicit = enqueue(state, "explicit", read_only_work=False, workspace_path=str(other), write_resources=["project:common"])
    assert queue.tick() == 1
    assert row(state, first)["state"] == "released"
    assert row(state, nested)["state"] == row(state, explicit)["state"] == "pending"


def test_guard_is_read_only_and_fences_unreleased_source_and_claims(state, tmp_path):
    runtime, context, work, _, queue = state
    mid = work.organize_work(context, "new", [create()])["refs"]["a"]
    first = enqueue(state, matter_id=mid, expected_revision=1, read_only_work=False, workspace_path=str(tmp_path))
    with pytest.raises(ManagementConflictError, match="not been released"):
        queue.check_run(first["run_id"])
    assert queue.tick() == 1
    with closing(runtime._conn()) as conn:
        conn.execute("PRAGMA query_only=ON")
        queue.check_run(first["run_id"], conn=conn)
        queue.check_run("unmanaged", conn=conn)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("DELETE FROM manager_work_claims")
    with pytest.raises(ManagementConflictError, match="reservation"):
        queue.check_run(first["run_id"])
    runtime.matter_service.update_matter(matter_id=mid, payload=MatterUpdateInput(expected_revision=1, summary="Changed scope"))
    with pytest.raises(ManagementConflictError, match="revision"):
        queue.check_run(first["run_id"])


def test_controller_pause_stops_new_release_but_leaves_independent_released_run(state):
    runtime, context, _, _, queue = state
    first = enqueue(state, "first")
    assert queue.tick() == 1
    second = enqueue(state, "second")
    runtime.management_service.update(context.session_id, expected_revision=2, status="paused")
    status(state, first, "waiting_user")
    assert queue.tick() == 0
    assert row(state, second)["state"] == "pending"
    queue.check_run(first["run_id"])
    runtime.management_service.update(context.session_id, expected_revision=3, status="active")
    assert queue.tick() == 1


def test_queue_control_cas_receipt_owner_and_cancelled_pending_cleanup(state):
    runtime, context, _, _, queue = state
    first = enqueue(state)
    paused = queue.control_queue(context, command_id="pause-q", queue_id=first["queue_id"], expected_revision=1, action="pause")
    assert queue.control_queue(context, command_id="pause-q", queue_id=first["queue_id"], expected_revision=1, action="pause") == paused
    assert queue.tick() == 0
    with pytest.raises(ManagementConflictError, match="revision"):
        queue.control_queue(context, command_id="stale", queue_id=first["queue_id"], expected_revision=1, action="resume")
    other = manager_fixture.__wrapped__(runtime)
    with pytest.raises(KeyError):
        queue.control_queue(other, command_id="foreign", queue_id=first["queue_id"], expected_revision=2, action="resume")
    queue.control_queue(context, command_id="resume-q", queue_id=first["queue_id"], expected_revision=2, action="resume")
    status(state, first, "cancelled")
    assert queue.tick() == 0
    assert row(state, first)["state"] == "finished"
    assert queue.list_queue(context, limit=1)["items"][0]["queue_id"] == first["queue_id"]
    assert queue.list_queue(other)["items"] == []


def test_released_queue_control_rejected_and_scan_cursor_wraps(state):
    runtime, context, _, _, queue = state
    first = enqueue(state)
    with closing(runtime._conn()) as conn, conn:
        # Old blocked/unavailable rows cannot permanently hide younger work beyond the scan bound.
        conn.execute("UPDATE manager_queue_scheduler SET scan_cursor=99999 WHERE singleton=1")
    assert queue.tick() == 1
    with pytest.raises(ManagementConflictError, match="unreleased"):
        queue.control_queue(context, command_id="too-late", queue_id=first["queue_id"], expected_revision=2, action="pause")
    with pytest.raises(ValueError):
        queue.tick(datetime.now(UTC).replace(tzinfo=None))


def test_readonly_policy_is_frozen_fail_closed_and_intersects_existing_permissions(state, monkeypatch):
    runtime, _, _, _, queue = state
    first = enqueue(state, "closed")
    queue.guard_replacement("unrelated-run")
    with pytest.raises(ValueError, match="cancelled and requeued"):
        queue.guard_replacement(first["run_id"])
    record = runtime.agent_run_store.load_run(first["run_id"])
    assert record["metadata"]["session_policy"]["allowed_tools"] == []
    assert record["metadata"]["session_policy"]["allow_fork"] is False
    queue.read_only_tools = lambda: ["filesystem.read_file", "web.search"]
    policy = runtime.managed_tasks.policy
    monkeypatch.setattr(runtime.managed_tasks, "policy", lambda sid: policy(sid).model_copy(
        update={"allowed_tools": ["filesystem.read_file", "filesystem.edit_file"], "allow_fork": True}))
    second = enqueue(state, "intersect")
    with closing(runtime._conn()) as conn:
        snapshot = json.loads(conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (second["run_id"],)).fetchone()[0])
    assert snapshot["metadata"]["session_policy"]["allowed_tools"] == ["filesystem.read_file"]
    assert snapshot["metadata"]["session_policy"]["allow_fork"] is False
    assert runtime.agent_run_manager.get_run(second["run_id"]).metadata["session_policy"] == snapshot["metadata"]["session_policy"]


def test_changed_session_workspace_cannot_reuse_old_write_reservation(state, tmp_path):
    runtime, _, _, _, queue = state
    first = enqueue(state, read_only_work=False, workspace_path=str(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    workspace = runtime.managed_tasks.workspace(str(other))
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET metadata=? WHERE session_id=?", (json.dumps({"workspace": workspace}), first["session_id"]))
    assert queue.tick() == 0
    assert row(state, first)["state"] == "blocked"
    assert row(state, first)["issue"] == "work_source_changed"


def test_bounded_scan_advances_past_more_than_one_batch_of_unavailable_records(state):
    runtime, _, _, _, queue = state
    first = enqueue(state)
    with closing(runtime._conn()) as conn, conn:
        original = dict(conn.execute("SELECT * FROM manager_work_queue WHERE queue_id=?", (first["queue_id"],)).fetchone())
        conn.execute("DELETE FROM manager_work_queue")
        columns = list(original)
        placeholders = ",".join("?" for _ in columns)
        for index in range(256):
            stale = {**original, "queue_id": f"stale-{index}", "run_id": f"unavailable-{index}", "command_id": f"stale-{index}"}
            conn.execute(f"INSERT INTO manager_work_queue({','.join(columns)}) VALUES({placeholders})", list(stale.values()))
        conn.execute(f"INSERT INTO manager_work_queue({','.join(columns)}) VALUES({placeholders})", list(original.values()))
    assert queue.tick() == 0
    assert row(state, first)["state"] == "pending"
    assert queue.tick() == 1
    assert row(state, first)["state"] == "released"


def test_user_resumed_root_does_not_block_independent_queue_release(state):
    runtime, _, _, _, queue = state
    first = enqueue(state, "first")
    assert queue.tick() == 1
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE background_jobs SET status='succeeded' WHERE kind='agent_dispatch'")
    status(state, first, "running")
    second = enqueue(state, "second")
    assert queue.tick() == 1
    assert row(state, second)["state"] == "released"


@pytest.mark.parametrize("released", [False, True])
def test_deleted_task_session_cannot_release_or_continue_queued_work(state, released):
    runtime, _, _, _, queue = state
    receipt = enqueue(state)
    if released:
        assert queue.tick() == 1
    assert runtime.session_service.delete_session(session_id=receipt["session_id"])
    if released:
        with pytest.raises(ManagementConflictError, match="session is unavailable"):
            queue.check_run(receipt["run_id"])
    else:
        assert queue.tick() == 0
        assert row(state, receipt)["state"] == "blocked"
