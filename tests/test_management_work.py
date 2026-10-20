"""Offline deterministic organization, staging, and scope/fence contracts."""
import json
from contextlib import closing

import pytest
from pydantic import ValidationError

from app.core.agent_runs import AgentRunStatus
from app.domains.management import ManagementConflictError
from app.domains.management_work import ManagementWorkService, OrganizeWorkInput
from app.domains.matters import MatterCreateInput, MatterUpdateInput
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_tasks import manager_context as context_fixture


@pytest.fixture
def state(tmp_path, monkeypatch):
    generator = runtime_fixture.__wrapped__(tmp_path, monkeypatch)
    runtime = next(generator)
    runtime.local_app_config.management.internal_items_enabled = True
    context = context_fixture.__wrapped__(runtime)
    work = ManagementWorkService(runtime._conn, matters=runtime.matter_service, tasks=runtime.managed_tasks,
                                 management=runtime.management_service, goals=runtime.management_goals,
                                 run_store=runtime.agent_run_store, run_manager=runtime.agent_run_manager)
    work.ensure_schema()
    yield runtime, context, work
    try:
        next(generator)
    except StopIteration:
        pass


def create(ref="a", **fields):
    return {"op": "create", "ref": ref, "fields": {"title": "Explore uncertainty", **fields}}


def count(runtime, table):
    with closing(runtime._conn()) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def autonomous(state, monkeypatch):
    runtime, context, _ = state
    # Contract tests use a synthetic execution binding, with no worker/network.
    monkeypatch.setattr(runtime.managed_tasks, "guard", lambda *args, **kwargs: None)
    runtime.agent_run_manager._update_run(context.run_id, status=AgentRunStatus.RUNNING,
                                          metadata_patch={"manager_execution_id": "execution-work"})
    with closing(runtime._conn()) as conn, conn:
        revision = conn.execute("SELECT revision FROM manager_controllers WHERE session_id=?", (context.session_id,)).fetchone()[0]
        conn.execute("INSERT INTO manager_executions VALUES(?,?,?,'bound',?,'[]','{}','now','now')",
                     ("execution-work", context.session_id, context.run_id, revision))
    return "execution-work"


def test_abstract_internal_without_deadline_and_idempotent_restart(state):
    runtime, context, work = state
    ops = [dict(create(summary="Keep learning"), management={"purpose": "goal"})]
    receipt = work.organize_work(context, "abstract", ops)
    assert receipt["status"] == "applied"
    matter_id = receipt["refs"]["a"]
    record = runtime.matter_service.get_matter(matter_id=matter_id)
    assert record.visibility == "internal" and record.due_at is None and record.status == "open"
    assert work.inspect_work(context, matter_id)["management"]["responsible_party"] == "agent"
    reopened = ManagementWorkService(runtime._conn, matters=runtime.matter_service, tasks=runtime.managed_tasks,
                                     management=runtime.management_service, goals=runtime.management_goals,
                                     run_store=runtime.agent_run_store, run_manager=runtime.agent_run_manager)
    reopened.ensure_schema()
    assert reopened.organize_work(context, "abstract", ops) == receipt
    assert count(runtime, "matters") == 1
    with pytest.raises(ManagementConflictError, match="different arguments"):
        work.organize_work(context, "abstract", [create(title="Other")])


def test_atomic_batch_rolls_back_matter_sidecar_and_command(state):
    runtime, context, work = state
    ops = [dict(create(), management={"purpose": "goal"}),
           {"op": "link", "source": "a", "target": "missing", "relation": "supports"}]
    with pytest.raises(KeyError):
        work.organize_work(context, "rollback", ops)
    for table in ("matters", "matter_management", "management_commands", "manager_work_relations"):
        assert count(runtime, table) == 0


def test_refs_plan_children_and_relationship_cycle(state):
    runtime, context, work = state
    receipt = work.organize_work(context, "plan", [create("plan", kind="plan"), create("task", parent_matter_id="plan"),
        {"op": "link", "source": "task", "target": "plan", "relation": "supports"}])
    plan, task = receipt["refs"]["plan"], receipt["refs"]["task"]
    assert runtime.matter_service.get_matter(matter_id=task).parent_matter_id == plan
    with pytest.raises(ValueError, match="cycle"):
        work.organize_work(context, "cycle", [{"op": "link", "source": plan, "target": task, "relation": "depends_on"}])
    with pytest.raises(ValueError, match="themselves"):
        work.organize_work(context, "self", [{"op": "link", "source": plan, "target": plan, "relation": "supports"}])
    with pytest.raises(ValueError):
        work.organize_work(context, "nested", [create("nested", kind="plan", parent_matter_id=plan)])
    assert count(runtime, "matters") == 2


@pytest.mark.parametrize("operation", [
    {"op": "create", "ref": "a", "fields": {"title": "X", "metadata": {"internal": True}}},
    {"op": "create", "ref": "a", "fields": {"title": "X", "unexpected": True}},
    {"op": "create", "ref": "a", "fields": {"title": "X"}, "management": {"purpose": "invented"}},
    {"op": "create", "ref": "a", "fields": {"title": "X"}, "management": {"wait_condition": {"type": "external", "raw_prompt": "X"}}},
    {"op": "update", "matter_id": "a", "expected_revision": 1, "changes": {"metadata": {}}},
    {"op": "delete", "matter_id": "a"},
])
def test_strict_union_rejects_unrecognized_fields_and_operations(state, operation):
    runtime, context, work = state
    with pytest.raises(ValidationError):
        work.organize_work(context, "invalid", [operation])
    assert count(runtime, "management_commands") == 0


def test_operation_bound_and_schema_are_explicit(state):
    _, context, work = state
    with pytest.raises(ValidationError):
        work.organize_work(context, "too-many", [create(str(i)) for i in range(21)])
    schema = OrganizeWorkInput.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["operations"]["maxItems"] == 20


def test_revision_update_preserves_unsupplied_visibility(state):
    runtime, context, work = state
    receipt = work.organize_work(context, "new", [create()])
    matter_id = receipt["refs"]["a"]
    work.organize_work(context, "change", [{"op": "update", "matter_id": matter_id, "expected_revision": 1,
                                           "changes": {"summary": "New finding"}}])
    record = runtime.matter_service.get_matter(matter_id=matter_id)
    assert record.visibility == "internal" and record.revision == 2
    with pytest.raises(ManagementConflictError):
        work.organize_work(context, "stale", [{"op": "update", "matter_id": matter_id, "expected_revision": 1, "changes": {"status": "done"}}])


def test_bind_goal_checks_versions_owner_and_has_one_business_state(state):
    runtime, context, work = state
    goal = runtime.management_goals.create_goal(context.session_id, objective="Original goal")
    receipt = work.organize_work(context, "binding", [create(), {"op": "bind_goal", "goal_id": goal["goal_id"],
        "matter_id": "a", "expected_goal_revision": 1, "expected_matter_revision": 1}])
    assert receipt["results"][-1]["matter_id"] == receipt["refs"]["a"]
    with closing(runtime._conn()) as conn:
        raw = conn.execute("SELECT * FROM manager_goals WHERE goal_id=?", (goal["goal_id"],)).fetchone()
        assert raw["objective"] == "Original goal" and raw["status"] == "active"
    other = runtime.management_service.create()["session"]["session_id"]
    foreign = runtime.management_goals.create_goal(other, objective="Other")
    with pytest.raises(ValueError, match="belong"):
        work.organize_work(context, "foreign", [{"op": "bind_goal", "goal_id": foreign["goal_id"],
            "matter_id": receipt["refs"]["a"], "expected_goal_revision": 1, "expected_matter_revision": 1}])


def test_autonomous_validation_is_invisible_and_commit_is_atomic_and_idempotent(state, monkeypatch):
    runtime, context, work = state
    execution = autonomous(state, monkeypatch)
    receipt = work.organize_work(context, "proposal", [dict(create(), management={"purpose": "goal"})])
    assert receipt["status"] == "proposed" and count(runtime, "matters") == 0
    with closing(runtime._conn()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        work.apply_proposals(conn, execution, context.session_id)
        assert conn.execute("SELECT count(*) FROM matters").fetchone()[0] == 1
        conn.rollback()
    assert count(runtime, "matters") == 0
    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        work.apply_proposals(conn, execution, context.session_id)
        work.apply_proposals(conn, execution, context.session_id)
    assert count(runtime, "matters") == count(runtime, "matter_management") == 1
    assert work.organize_work(context, "proposal", [dict(create(), management={"purpose": "goal"})])["status"] == "applied"


def test_later_proposal_validates_against_prior_staged_work(state, monkeypatch):
    runtime, context, work = state
    execution = autonomous(state, monkeypatch)
    first = work.organize_work(context, "first", [create()])
    matter_id = first["refs"]["a"]
    work.organize_work(context, "second", [{"op": "update", "matter_id": matter_id,
        "expected_revision": 1, "changes": {"summary": "Incremental"}}])
    with pytest.raises(ManagementConflictError, match="revision"):
        work.organize_work(context, "third", [{"op": "update", "matter_id": matter_id,
            "expected_revision": 1, "changes": {"status": "done"}}])
    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        work.apply_proposals(conn, execution, context.session_id)
    assert runtime.matter_service.get_matter(matter_id=matter_id).summary == "Incremental"
    assert count(runtime, "manager_work_proposals") == 2


def test_publish_rechecks_revisions_and_rolls_back_all(state, monkeypatch):
    runtime, context, work = state
    matter = runtime.matter_service.create_matter(MatterCreateInput(title="User task"))
    execution = autonomous(state, monkeypatch)
    work.organize_work(context, "stale-proposal", [create(), {"op": "update", "matter_id": matter.matter_id,
        "expected_revision": 1, "changes": {"summary": "Agent"}}])
    runtime.matter_service.update_matter(matter_id=matter.matter_id,
                                         payload=MatterUpdateInput(summary="Human", expected_revision=1))
    with closing(runtime._conn()) as conn, pytest.raises(ManagementConflictError), conn:
        conn.execute("BEGIN IMMEDIATE")
        work.apply_proposals(conn, execution, context.session_id)
    assert count(runtime, "matters") == 1
    assert runtime.matter_service.get_matter(matter_id=matter.matter_id).summary == "Human"


def test_proposed_command_cannot_replay_in_new_execution(state, monkeypatch):
    runtime, context, work = state
    execution = autonomous(state, monkeypatch)
    ops = [create()]
    work.organize_work(context, "old", ops)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE manager_executions SET status='superseded' WHERE execution_id=?", (execution,))
    runtime.agent_run_manager._update_run(context.run_id, status=AgentRunStatus.RUNNING,
                                          metadata_patch={"manager_execution_id": "new-execution"})
    with pytest.raises(ManagementConflictError, match="another execution"):
        work.organize_work(context, "old", ops)
    assert count(runtime, "matters") == 0


@pytest.mark.parametrize("invalid", ["child", "cancel", "controller", "ordinary"])
def test_only_running_active_manager_root_can_mutate(state, invalid):
    runtime, context, work = state
    if invalid == "child":
        runtime.agent_run_manager._update_run(context.run_id, status=AgentRunStatus.RUNNING, parent_run_id="parent")
    elif invalid == "cancel":
        runtime.agent_run_manager.request_cancel(context.run_id)
    elif invalid == "controller":
        runtime.management_service.update(context.session_id, expected_revision=2, status="paused")
    else:
        with closing(runtime._conn()) as conn, conn:
            conn.execute("UPDATE agent_sessions SET kind='ordinary' WHERE session_id=?", (context.session_id,))
    with pytest.raises(ManagementConflictError):
        work.organize_work(context, "denied", [create()])
    assert count(runtime, "matters") == 0


def test_internal_feature_flag_and_message_source_safety_are_preserved(state):
    runtime, context, work = state
    runtime.local_app_config.management.internal_items_enabled = False
    with pytest.raises(PermissionError):
        work.organize_work(context, "disabled", [create()])
    runtime.local_app_config.management.internal_items_enabled = True
    with pytest.raises(PermissionError):
        work.organize_work(context, "message", [create(source_links=[{"source_type": "message_history_message", "source_id": "private"}])])
    assert count(runtime, "matters") == 0


def test_overview_is_bounded_and_inspect_reads_full_text_without_metadata(state):
    _, context, work = state
    receipt = work.organize_work(context, "pages", [create("a", summary="x" * 900), create("b")])
    page = work.overview(context, limit=1)
    assert page["total"] == 2 and page["next_offset"] == 1 and len(page["matters"]) == 1
    complete = work.inspect_work(context, receipt["refs"]["a"])
    assert len(complete["matter"]["summary"]) == 900
    assert "metadata" not in json.dumps(page) and "raw_prompt" not in json.dumps(complete)
    assert all(len(item["summary"]) <= 500 for item in page["matters"])


def test_business_fence_uses_persistent_versions_without_recursing_to_caller(state, monkeypatch):
    runtime, context, work = state
    receipt = work.organize_work(context, "fence-create", [dict(create(), management={"purpose": "goal"})])
    matter_id = receipt["refs"]["a"]
    runtime.agent_run_manager._update_run(context.run_id, status=AgentRunStatus.RUNNING,
                                          metadata_patch={"manager_matter_versions": {matter_id: 1}})
    def unexpected(*args, **kwargs):
        raise AssertionError("Business fence must not call the run manager or caller")
    monkeypatch.setattr(runtime.managed_tasks, "caller", unexpected)
    monkeypatch.setattr(runtime.agent_run_manager, "get_run", unexpected)
    with closing(runtime._conn()) as conn:
        work.check_run(context.run_id, conn=conn)
        assert not conn.in_transaction
    runtime.matter_service.update_matter(matter_id=matter_id, payload=MatterUpdateInput(summary="Human", expected_revision=1))
    with closing(runtime._conn()) as conn, pytest.raises(ManagementConflictError, match="changed"):
        work.check_run(context.run_id, conn=conn)


def test_business_fence_rejects_paused_management_even_if_matter_revision_unchanged(state):
    runtime, context, work = state
    receipt = work.organize_work(context, "paused-work", [dict(create(), management={"management_state": "paused"})])
    matter_id = receipt["refs"]["a"]
    runtime.agent_run_manager._update_run(context.run_id, status=AgentRunStatus.RUNNING,
                                          metadata_patch={"manager_matter_versions": {matter_id: 1}})
    with pytest.raises(ManagementConflictError, match="paused"):
        work.check_run(context.run_id)


def test_staged_goal_creation_can_bind_work_before_publication(state, monkeypatch):
    runtime, context, work = state
    execution = autonomous(state, monkeypatch)
    goal_receipt = runtime.managed_tasks.set_goal(context, command_id="new-goal", objective="Explore")
    receipt = work.organize_work(context, "goal-work", [create(), {"op": "bind_goal",
        "goal_id": goal_receipt["goal"]["goal_id"], "matter_id": "a",
        "expected_goal_revision": 1, "expected_matter_revision": 1}])
    assert count(runtime, "matters") == count(runtime, "manager_goals") == 0
    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime.managed_tasks.apply_goal_proposals(conn, execution, context.session_id)
        def already_checked(*args, **kwargs):
            raise AssertionError("Finalization must not re-fence versions it just changed")
        monkeypatch.setattr(runtime.managed_tasks, "caller", already_checked)
        work.apply_proposals(conn, execution, context.session_id, authority_checked=True)
    with closing(runtime._conn()) as conn:
        link = conn.execute("SELECT * FROM manager_goal_matter_links").fetchone()
        assert link["goal_id"] == goal_receipt["goal"]["goal_id"] and link["matter_id"] == receipt["refs"]["a"]


def test_sidecar_partial_update_preserves_goal_pause_and_responsibility(state):
    _, context, work = state
    receipt = work.organize_work(context, "sidecar", [dict(create(), management={
        "purpose": "goal", "responsible_party": "external", "management_state": "paused",
        "wait_condition": {"type": "external", "description": "Await evidence"}})])
    matter_id = receipt["refs"]["a"]
    work.organize_work(context, "sidecar-update", [{"op": "update", "matter_id": matter_id,
        "expected_revision": 1, "changes": {}, "management": {"next_action": "Read when available"}}])
    sidecar = work.inspect_work(context, matter_id)["management"]
    assert sidecar["purpose"] == "goal" and sidecar["management_state"] == "paused"
    assert sidecar["responsible_party"] == "external" and sidecar["wait_condition"]["type"] == "external"
    assert sidecar["revision"] == 2


def test_lease_recheck_before_commit_rolls_back_work_and_receipt(state, monkeypatch):
    runtime, context, work = state
    calls = 0
    def guard(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ManagementConflictError("Lease expired before commit")
    monkeypatch.setattr(runtime.managed_tasks, "guard", guard)
    with pytest.raises(ManagementConflictError, match="Lease expired"):
        work.organize_work(context, "lease-expired", [dict(create(), management={"purpose": "goal"})])
    assert count(runtime, "matters") == count(runtime, "management_commands") == count(runtime, "matter_management") == 0


def test_overview_bounds_legacy_source_previews_while_inspect_preserves_full_evidence(state):
    runtime, context, work = state
    record = runtime.matter_service.create_matter(MatterCreateInput(title="Legacy evidence", source_links=[
        {"source_type": "local_note", "source_id": f"note-{i}", "reason": "x" * 4000} for i in range(10)]))
    item = work.overview(context)["matters"][0]
    assert item["source_link_count"] == 10 and len(item["source_links"]) == 5
    assert all(len(link["reason"]) <= 200 for link in item["source_links"])
    assert item["source_links_truncated"] and item["read_more"] == {"matter_id": record.matter_id}
    assert len(work.inspect_work(context, record.matter_id)["matter"]["source_links"]) == 10
