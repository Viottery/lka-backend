"""Offline work assignment/readiness/result-review contracts, with no task execution."""
from contextlib import closing

import pytest
from pydantic import ValidationError

from app.core.agent_runs import AgentRunStatus
from app.domains.management import ManagementConflictError
from app.domains.management_assignments import (
    AssignmentInput,
    ManagementAssignmentService,
    ReviewInput,
    StartWorkInput,
)
from app.domains.matters import MatterUpdateInput
from tests.test_management_work import autonomous, count, create
from tests.test_management_work import state as work_state


@pytest.fixture
def state(tmp_path, monkeypatch):
    generator = work_state.__wrapped__(tmp_path, monkeypatch)
    runtime, context, work = next(generator)
    service = ManagementAssignmentService(runtime._conn, tasks=runtime.managed_tasks,
                                        matters=runtime.matter_service, work=work, goals=runtime.management_goals)
    service.ensure_schema()
    yield runtime, context, work, service
    try:
        next(generator)
    except StopIteration:
        pass


def matter(state, command="new", **fields):
    _, context, work, _ = state
    return work.organize_work(context, command, [create(**fields)])["refs"]["a"]


def task(state, command="task", status="completed"):
    runtime, context, _, _ = state
    receipt = runtime.managed_tasks.start_task(context, command_id=command, purpose="Read bounded evidence")
    runtime.agent_run_manager._update_run(receipt["run_id"], status=AgentRunStatus(status))
    if status == "completed":
        runtime.session_service.append_message(session_id=receipt["session_id"], role="agent", content="Offline checked result",
            message_id="result-" + receipt["run_id"], payload={"run_id": receipt["run_id"]})
    return receipt["run_id"]


def assign(state, matter_id, run_id, **overrides):
    _, context, _, service = state
    return service.assign_existing(context, command_id="bind-" + matter_id, matter_id=matter_id,
                                   expected_revision=1, run_id=run_id, **overrides)


def review(state, matter_id, run_id, command="review", **overrides):
    _, context, _, service = state
    args = {"command_id": command, "matter_id": matter_id, "expected_revision": 1, "run_id": run_id,
            "verdict": "accepted", "evidence": [{"source_type": "session_message", "source_id": "result-" + run_id, "reason": "Checked requested outcome"}],
            "summary": "Result satisfies the requested business condition", **overrides}
    return service.review(context, **args)


def test_bind_existing_does_not_start_execution_and_receipt_survives_reopen(state):
    runtime, context, work, service = state
    mid, rid = matter(state), task(state)
    before = {table: count(runtime, table) for table in ("agent_runs", "agent_sessions", "background_jobs")}
    receipt = assign(state, mid, rid, expected_run_status="completed")
    assert receipt["status"] == "applied"
    assert receipt["result"]["run"]["run_id"] == rid
    assert receipt["result"]["readiness"]["ready"]
    assert all(count(runtime, table) == value for table, value in before.items())
    reopened = ManagementAssignmentService(runtime._conn, tasks=runtime.managed_tasks,
                                          matters=runtime.matter_service, work=work, goals=runtime.management_goals)
    reopened.ensure_schema()
    assert reopened.assign_existing(context, command_id="bind-" + mid, matter_id=mid, expected_revision=1,
                                    run_id=rid, expected_run_status="completed") == receipt
    assert count(runtime, "manager_work_assignments") == 1
    view = service.result_view(context, mid)
    assert view["business_status"] == "open" and not view["business_complete"]
    assert "user_input" not in view["assignments"][0]["run"]
    assert "metadata" not in view["assignments"][0]["run"]


def test_cas_run_snapshot_and_foreign_session_are_enforced(state):
    runtime, context, _, service = state
    mid, rid = matter(state), task(state)
    with pytest.raises(ManagementConflictError, match="status changed"):
        assign(state, mid, rid, expected_run_status="queued")
    with pytest.raises(ManagementConflictError, match="revision changed"):
        service.assign_existing(context, command_id="stale", matter_id=mid, expected_revision=2, run_id=rid)
    sid = runtime.session_service.create_session().session.session_id
    foreign = runtime.create_agent_run(session_id=sid, user_input="Unmanaged", resumable=True)
    with pytest.raises(ManagementConflictError, match="managed by this session"):
        assign(state, mid, foreign.run_id)
    assert count(runtime, "manager_work_assignments") == 0


def test_dependency_requires_done_and_accepted_review_supports_never_blocks(state):
    runtime, context, work, service = state
    receipt = work.organize_work(context, "graph", [create("target"), create("source"), create("support"),
        {"op": "link", "source": "source", "target": "target", "relation": "depends_on"},
        {"op": "link", "source": "source", "target": "support", "relation": "supports"}])
    target, source = receipt["refs"]["target"], receipt["refs"]["source"]
    source_run = task(state, "source-run")
    with pytest.raises(ManagementConflictError, match="dependencies"):
        assign(state, source, source_run)
    target_run = task(state, "target-run")
    assign(state, target, target_run)
    reviewed = review(state, target, target_run)
    assert reviewed["result"]["completion_update"] == {"expected_revision": 1, "changes": {"status": "done"}}
    assert runtime.matter_service.get_matter(matter_id=target).status == "open", "acceptance never writes business completion"
    assert not service.readiness(context, source)["ready"]
    # The returned proposal has a real, separately authorized application path.
    update = reviewed["result"]["completion_update"]
    work.organize_work(context, "complete-target", [{"op": "update", "matter_id": target, **update}])
    readiness = service.readiness(context, source)
    assert readiness["ready"] and len(readiness["dependencies"]) == 1
    assert readiness["dependencies"][0]["verified"]
    assert service.result_view(context, target)["business_complete"]
    assert assign(state, source, source_run)["status"] == "applied"
    # Any later edit invalidates the prior review instead of borrowing stale evidence.
    work.organize_work(context, "edit-target", [{"op": "update", "matter_id": target, "expected_revision": 2, "changes": {"summary": "Changed condition"}}])
    assert not service.readiness(context, source)["ready"]


def test_done_without_review_and_needs_review_never_unblock_dependency(state):
    _, context, work, service = state
    receipt = work.organize_work(context, "graph", [create("target"), create("source"),
        {"op": "link", "source": "source", "target": "target", "relation": "depends_on"}])
    target, source = receipt["refs"]["target"], receipt["refs"]["source"]
    rid = task(state)
    assign(state, target, rid)
    review(state, target, rid)
    needs_review = review(state, target, rid, command="needs-review", verdict="needs_review", evidence=[], summary="Evidence still incomplete")
    assert needs_review["result"]["completion_update"] is None
    work.organize_work(context, "manual-done", [{"op": "update", "matter_id": target, "expected_revision": 1, "changes": {"status": "done"}}])
    assert not service.readiness(context, source)["ready"]
    assert not service.result_view(context, target)["business_complete"]


@pytest.mark.parametrize("status", ["queued", "running", "paused", "waiting_user", "waiting_confirmation", "failed", "cancelled", "timed_out"])
def test_noncompleted_run_cannot_be_accepted(state, status):
    runtime, _, _, _ = state
    mid, rid = matter(state), task(state, status=status)
    assign(state, mid, rid)
    with pytest.raises(ManagementConflictError, match="Only completed"):
        review(state, mid, rid)
    assert count(runtime, "manager_work_reviews") == 0
    assert runtime.matter_service.get_matter(matter_id=mid).status == "open"


def test_review_requires_binding_and_current_matter_version(state):
    _, context, work, _ = state
    mid, rid = matter(state), task(state)
    with pytest.raises(ManagementConflictError, match="assigned"):
        review(state, mid, rid)
    assign(state, mid, rid)
    work.organize_work(context, "changed", [{"op": "update", "matter_id": mid, "expected_revision": 1, "changes": {"summary": "New scope"}}])
    with pytest.raises(ManagementConflictError, match="revision changed"):
        review(state, mid, rid)
    with pytest.raises(ManagementConflictError, match="current Matter revision"):
        review(state, mid, rid, expected_revision=2)


def test_review_rejects_invented_foreign_and_wrong_run_evidence(state):
    runtime, _, _, _ = state
    mid, rid = matter(state), task(state)
    other = task(state, "foreign-result")
    assign(state, mid, rid)
    for ref in ("does-not-exist", "result-" + other):
        with pytest.raises(ManagementConflictError, match="existing result message"):
            review(state, mid, rid, evidence=[{"source_type": "session_message", "source_id": ref}])
    run = runtime.agent_run_manager.get_run(rid)
    runtime.session_service.append_message(session_id=run.session_id, role="agent", content="Prior result in same session",
        message_id="wrong-run-message", payload={"run_id": other})
    with pytest.raises(ManagementConflictError, match="matching run provenance"):
        review(state, mid, rid, evidence=[{"source_type": "session_message", "source_id": "wrong-run-message"}])
    runtime.session_service.append_message(session_id=run.session_id, role="agent", content="Legacy result without provenance",
        message_id="legacy-result-message", payload={})
    with pytest.raises(ManagementConflictError, match="matching run provenance"):
        review(state, mid, rid, evidence=[{"source_type": "session_message", "source_id": "legacy-result-message"}])
    assert count(runtime, "manager_work_reviews") == 0


def test_review_schema_is_bounded_and_extra_content_is_not_accepted():
    base = {"command_id": "r", "matter_id": "m", "expected_revision": 1, "run_id": "run",
            "verdict": "accepted", "summary": "Reviewed", "evidence": [{"source_type": "session_message", "source_id": "a"}]}
    for changes in ({"evidence": []}, {"evidence": base["evidence"] * 11}, {"summary": "x" * 2001},
                    {"raw_run_payload": {}}, {"expected_revision": True},
                    {"evidence": [{"source_type": "session_message", "source_id": "a", "content": "raw"}]}):
        with pytest.raises(ValidationError):
            ReviewInput.model_validate({**base, **changes})
    assert AssignmentInput.model_json_schema()["additionalProperties"] is False
    assert ReviewInput.model_json_schema()["properties"]["evidence"]["maxItems"] == 10


def test_autonomous_assignment_and_review_are_invisible_until_atomic_publish(state, monkeypatch):
    runtime, context, work, service = state
    mid, rid = matter(state), task(state)
    execution = autonomous((runtime, context, work), monkeypatch)
    assigned = assign(state, mid, rid)
    reviewed = review(state, mid, rid)
    assert assigned["status"] == reviewed["status"] == "proposed"
    assert count(runtime, "manager_work_assignments") == count(runtime, "manager_work_reviews") == 0
    with closing(runtime._conn()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        service.apply_proposals(conn, execution, context.session_id)
        assert conn.execute("SELECT count(*) FROM manager_work_reviews").fetchone()[0] == 1
        conn.rollback()
    assert count(runtime, "manager_work_reviews") == 0
    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        service.apply_proposals(conn, execution, context.session_id, authority_checked=True)
        service.apply_proposals(conn, execution, context.session_id, authority_checked=True)
    assert count(runtime, "manager_work_assignments") == count(runtime, "manager_work_reviews") == 1
    assert runtime.matter_service.get_matter(matter_id=mid).status == "open"
    assert review(state, mid, rid)["status"] == "applied"


def test_assignment_staging_replays_work_and_commit_revalidates_matter(state, monkeypatch):
    runtime, context, work, service = state
    rid = task(state)
    execution = autonomous((runtime, context, work), monkeypatch)
    mid = matter(state)
    assign(state, mid, rid)
    assert count(runtime, "matters") == count(runtime, "manager_work_assignments") == 0
    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        work.apply_proposals(conn, execution, context.session_id, authority_checked=True)
        service.apply_proposals(conn, execution, context.session_id, authority_checked=True)
    assert count(runtime, "matters") == count(runtime, "manager_work_assignments") == 1


def test_assignment_publish_fails_closed_after_cancellation_or_late_revision(state, monkeypatch):
    runtime, context, work, service = state
    mid, rid = matter(state), task(state)
    execution = autonomous((runtime, context, work), monkeypatch)
    assign(state, mid, rid)
    runtime.agent_run_manager.request_cancel(context.run_id, reason="User stopped")
    with closing(runtime._conn()) as conn, conn, pytest.raises(ManagementConflictError):
        conn.execute("BEGIN IMMEDIATE")
        service.apply_proposals(conn, execution, context.session_id)
    assert count(runtime, "manager_work_assignments") == 0
    # Even after an outer caller has checked authority, the execution binding remains mandatory.
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE manager_executions SET status='superseded' WHERE execution_id=?", (execution,))
    with closing(runtime._conn()) as conn, conn, pytest.raises(ManagementConflictError):
        conn.execute("BEGIN IMMEDIATE")
        service.apply_proposals(conn, execution, context.session_id, authority_checked=True)


def test_late_matter_edit_rolls_back_staged_assignment_at_publication(state, monkeypatch):
    runtime, context, work, service = state
    mid, rid = matter(state), task(state)
    execution = autonomous((runtime, context, work), monkeypatch)
    assign(state, mid, rid)
    runtime.matter_service.update_matter(matter_id=mid, payload=MatterUpdateInput(expected_revision=1, summary="User changed scope"))
    with closing(runtime._conn()) as conn, conn, pytest.raises(ManagementConflictError, match="revision changed"):
        conn.execute("BEGIN IMMEDIATE")
        service.apply_proposals(conn, execution, context.session_id)
    assert count(runtime, "manager_work_assignments") == 0


def test_paused_work_can_be_inspected_but_cannot_be_assigned(state):
    _, context, work, service = state
    receipt = work.organize_work(context, "paused-work", [dict(create(), management={"management_state": "paused"})])
    mid, rid = receipt["refs"]["a"], task(state)
    assert service.result_view(context, mid)["readiness"]["management_paused"]
    assert not service.readiness(context, mid)["ready"]
    with pytest.raises(ManagementConflictError, match="paused"):
        assign(state, mid, rid)


def start(state, matter_id, command="start-ready", **overrides):
    _, context, _, service = state
    return service.start_work(context, command_id=command, matter_id=matter_id, expected_revision=1,
                              purpose="Read evidence for this business action", **overrides)


def test_start_work_accepts_one_ready_run_with_atomic_business_binding_and_idempotency(state):
    runtime, context, work, service = state
    mid = matter(state)
    tables = ("agent_runs", "agent_sessions", "background_jobs", "managed_session_links", "manager_work_assignments", "management_commands")
    before = {table: count(runtime, table) for table in tables}
    receipt = start(state, mid)
    assert receipt["action"] == "start_work" and receipt["status"] == "accepted"
    assert receipt["matter_id"] == mid and receipt["matter_revision"] == 1
    assert receipt["assignment"]["run"]["run_id"] == receipt["run_id"]
    assert all(count(runtime, table) == value + 1 for table, value in before.items())
    assert runtime.agent_run_manager.get_run(receipt["run_id"]).status == AgentRunStatus.QUEUED
    assert service.result_view(context, mid)["assignments"][0]["run"]["run_id"] == receipt["run_id"]
    work.organize_work(context, "later-edit", [{"op": "update", "matter_id": mid, "expected_revision": 1,
                                               "changes": {"summary": "User clarified the goal"}}])
    after = {table: count(runtime, table) for table in tables}
    assert start(state, mid) == receipt, "accepted command retries do not recheck changed business state"
    assert all(count(runtime, table) == value for table, value in after.items())
    with pytest.raises(ManagementConflictError, match="different arguments"):
        service.start_work(context, command_id="start-ready", matter_id=mid, expected_revision=2,
                           purpose="Read evidence for this business action")


def test_start_work_blocks_unverified_dependency_before_any_task_is_accepted(state):
    runtime, context, work, _ = state
    receipt = work.organize_work(context, "blocked-graph", [create("target"), create("source"),
        {"op": "link", "source": "source", "target": "target", "relation": "depends_on"}])
    tables = ("agent_runs", "agent_sessions", "background_jobs", "managed_session_links", "manager_work_assignments", "management_commands")
    before = {table: count(runtime, table) for table in tables}
    with pytest.raises(ManagementConflictError, match="dependencies"):
        start(state, receipt["refs"]["source"])
    assert all(count(runtime, table) == value for table, value in before.items())


def test_start_work_rolls_back_task_and_dispatch_when_binding_fails(state, monkeypatch):
    runtime, _, _, service = state
    mid = matter(state)
    tables = ("agent_runs", "agent_sessions", "background_jobs", "managed_session_links", "manager_work_assignments", "management_commands")
    before = {table: count(runtime, table) for table in tables}
    def reject_binding(*args, **kwargs):
        raise ManagementConflictError("Synthetic binding failure after task creation")
    monkeypatch.setattr(service, "_apply", reject_binding)
    with pytest.raises(ManagementConflictError, match="binding failure"):
        start(state, mid)
    assert all(count(runtime, table) == value for table, value in before.items())


def test_start_work_fences_late_parent_cancellation_and_never_restores_uncommitted_run(state, monkeypatch):
    runtime, context, _, service = state
    mid = matter(state)
    tables = ("agent_runs", "agent_sessions", "background_jobs", "managed_session_links", "manager_work_assignments", "management_commands")
    before = {table: count(runtime, table) for table in tables}
    original_apply = service._apply
    caller = runtime.managed_tasks.caller
    restored = []
    def bind_then_lose_authority(conn, *args):
        result = original_apply(conn, *args)
        def reject_final_fence(*args, **kwargs):
            raise ManagementConflictError("Parent control changed before commit")
        monkeypatch.setattr(runtime.managed_tasks, "caller", reject_final_fence)
        return result
    monkeypatch.setattr(service, "_apply", bind_then_lose_authority)
    monkeypatch.setattr(runtime.agent_run_manager, "restore_run", lambda record: restored.append(record))
    with pytest.raises(ManagementConflictError, match="control changed"):
        start(state, mid)
    assert not restored
    assert all(count(runtime, table) == value for table, value in before.items())
    monkeypatch.setattr(runtime.managed_tasks, "caller", caller)
    runtime.agent_run_manager.request_cancel(context.run_id, reason="User stopped")
    with pytest.raises(ManagementConflictError):
        start(state, mid)


def test_autonomous_start_work_acceptance_is_independent_of_later_parent_publication(state, monkeypatch):
    runtime, context, work, _ = state
    mid = matter(state)
    autonomous((runtime, context, work), monkeypatch)
    receipt = start(state, mid)
    assert receipt["status"] == "accepted"
    assert count(runtime, "manager_assignment_proposals") == 0
    runtime.agent_run_manager.fail_run(context.run_id, error_type="synthetic_parent_failure", error="Offline test")
    assert runtime.agent_run_manager.get_run(receipt["run_id"]).status == AgentRunStatus.QUEUED
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM manager_work_assignments WHERE run_id=?", (receipt["run_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch' AND scope_id=?", (receipt["session_id"],)).fetchone()[0] == 1
    assert runtime.matter_service.get_matter(matter_id=mid).status == "open"


def test_start_work_input_is_strict_and_has_bounded_fields():
    args = {"command_id": "s", "matter_id": "m", "expected_revision": 1, "purpose": "Inspect"}
    assert StartWorkInput.model_validate(args).title == "管理任务"
    for changes in ({"expected_revision": True}, {"run_id": "invented"}, {"purpose": "x" * 16001},
                    {"title": "x" * 41}, {"workspace_path": "x" * 4097}):
        with pytest.raises(ValidationError):
            StartWorkInput.model_validate({**args, **changes})


def test_start_work_rechecks_persisted_cancellation_after_workspace_preparation(state, monkeypatch, tmp_path):
    runtime, context, _, _ = state
    mid = matter(state)
    tables = ("agent_runs", "agent_sessions", "background_jobs", "managed_session_links", "manager_work_assignments", "management_commands")
    before = {table: count(runtime, table) for table in tables}
    before_cached = set(runtime.agent_run_manager._runs)
    resolve = runtime.managed_tasks.workspace
    def resolve_then_cancel(path):
        workspace = resolve(path)
        runtime.agent_run_manager.request_cancel(context.run_id, reason="User cancelled during workspace preparation")
        return workspace
    monkeypatch.setattr(runtime.managed_tasks, "workspace", resolve_then_cancel)
    with pytest.raises(ManagementConflictError, match="executing management root"):
        start(state, mid, workspace_path=str(tmp_path))
    assert all(count(runtime, table) == value for table, value in before.items())
    assert set(runtime.agent_run_manager._runs) == before_cached
    assert runtime.agent_run_store.load_run(context.run_id)["metadata"]["cancel_requested"] is True
