"""Offline task-command contracts, including atomic receipts and root isolation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from app.core.agent_dispatch import AgentDispatchLeaseLost
from app.core.agent_runs import AgentRunStatus
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import ToolContext, ToolInvocation
from app.domains.management import ManagementConflictError
from app.tool_packages.management import management_tools
from tests.test_management_api import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    run = runtime.create_agent_run(session_id=sid, user_input="Server original: investigate only, do not deploy.", resumable=True)
    runtime.agent_run_manager.mark_running(run.run_id)
    runtime.session_service.append_message(session_id=sid, role="user", content=run.user_input,
                                           payload={"trace_id": run.trace_id}, message_id="server-original")
    return ToolContext(session_id=sid, run_id=run.run_id, trace_id=run.trace_id, safety_review_approved=True)


def counts(runtime):
    with closing(runtime._conn()) as conn:
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
            "agent_sessions", "agent_runs", "management_commands", "managed_session_links", "background_jobs",
        )}


def new_target(runtime, *, status="queued"):
    sid = runtime.session_service.create_session(title="Ordinary task").session.session_id
    run = runtime.create_agent_run(session_id=sid, user_input="Original task", resumable=True)
    if status != "queued":
        runtime.agent_run_manager._update_run(run.run_id, status=AgentRunStatus(status))
    return runtime.agent_run_manager.get_run(run.run_id)


def test_start_is_independent_root_with_server_quote_and_idempotent_receipt(runtime, manager_context):
    service = runtime.managed_tasks
    first = service.start_task(manager_context, command_id="start-1", purpose="Find cause; model-invented quote is not original")
    initial_counts = counts(runtime)
    assert service.start_task(manager_context, command_id="start-1", purpose="Find cause; model-invented quote is not original") == first
    assert counts(runtime) == initial_counts
    run = runtime.agent_run_store.load_run(first["run_id"])
    assert run["parent_run_id"] is None and run["session_id"] != manager_context.session_id
    assert run["metadata"]["orchestrator"] == "langgraph"
    envelope = run["metadata"]["input_envelope"]
    assert envelope["origin"] == "manager"
    assert envelope["original_user_message_refs"] == ["server-original"]
    assert envelope["original_user_quotes"] == ["Server original: investigate only, do not deploy."]
    with closing(runtime._conn()) as conn:
        job = conn.execute("SELECT payload_json FROM background_jobs WHERE scope_id=?", (first["session_id"],)).fetchone()
        assert first["run_id"] in job[0]
        assert conn.execute("SELECT kind FROM agent_sessions WHERE session_id=?", (first["session_id"],)).fetchone()[0] == "ordinary"
    with pytest.raises(ManagementConflictError, match="different arguments"):
        service.start_task(manager_context, command_id="start-1", purpose="Different purpose")


def test_task_freezes_callers_effective_reasoning_over_controller_default(runtime, manager_context):
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE manager_controllers SET reasoning_level='low' WHERE session_id=?",
                     (manager_context.session_id,))
    caller = manager_context.model_copy(update={"llm_reasoning_level": "high", "llm_reasoning_source": "request"})
    receipt = runtime.managed_tasks.start_task(caller, command_id="reasoning", purpose="Inspect under caller policy")
    frozen = runtime.agent_run_manager.get_run(receipt["run_id"]).metadata
    assert frozen["llm_reasoning_level"] == "high"
    assert frozen["llm_reasoning_source"] == "request"
    assert frozen["session_policy"]["llm_reasoning_level"] == "high"


def test_start_receipt_deduplicates_after_runtime_reopen(runtime, manager_context):
    first = runtime.managed_tasks.start_task(manager_context, command_id="restart", purpose="Read current state")
    baseline = counts(runtime)
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        assert reopened.managed_tasks.start_task(manager_context, command_id="restart", purpose="Read current state") == first
        assert counts(reopened) == baseline
    finally:
        reopened.stop()


def test_concurrent_start_retries_commit_one_task_and_dispatch(runtime, manager_context):
    before = counts(runtime)
    barrier = Barrier(2)

    def start(_):
        barrier.wait(timeout=5)
        return runtime.managed_tasks.start_task(manager_context, command_id="concurrent", purpose="Inspect one task")

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(start, range(2)))
    assert receipts[0] == receipts[1]
    after = counts(runtime)
    assert all(after[table] == before[table] + 1 for table in before)


def test_explicit_workspace_is_validated_and_task_never_inherits_management_tools(runtime, manager_context, tmp_path):
    receipt = runtime.managed_tasks.start_task(manager_context, command_id="workspace", purpose="Inspect directory", workspace_path=str(tmp_path))
    session = runtime.get_session(session_id=receipt["session_id"]).session
    assert session.workspace.backend_path == str(tmp_path)
    record = runtime.agent_run_store.load_run(receipt["run_id"])
    assert not any(name.startswith("management.") for name in record["metadata"]["session_policy"]["allowed_tools"])
    baseline = counts(runtime)
    with pytest.raises(ValueError):
        runtime.managed_tasks.start_task(manager_context, command_id="bad-workspace", purpose="Inspect", workspace_path=str(tmp_path / "missing"))
    assert counts(runtime) == baseline


@pytest.mark.parametrize("failure_point", ["enqueue", "receipt"])
def test_start_failure_rolls_back_session_run_slot_receipt_and_job(runtime, manager_context, monkeypatch, failure_point):
    before = counts(runtime)
    before_cached = set(runtime.agent_run_manager._runs)

    def fail(*args, **kwargs):
        raise OSError("Injected command failure")

    monkeypatch.setattr(runtime.managed_tasks, "enqueue" if failure_point == "enqueue" else "_receipt", fail)
    with pytest.raises(OSError, match="Injected command failure"):
        runtime.managed_tasks.start_task(manager_context, command_id="rollback", purpose="Do work")
    assert counts(runtime) == before
    assert set(runtime.agent_run_manager._runs) == before_cached


def test_start_rechecks_persisted_cancellation_after_workspace_resolution(runtime, manager_context, monkeypatch, tmp_path):
    service = runtime.managed_tasks
    before = counts(runtime)
    before_cached = set(runtime.agent_run_manager._runs)
    resolve = service.workspace

    def resolve_then_cancel(path):
        workspace = resolve(path)
        runtime.agent_run_manager.request_cancel(manager_context.run_id, reason="User cancelled during workspace resolution")
        return workspace

    monkeypatch.setattr(service, "workspace", resolve_then_cancel)
    with pytest.raises(ManagementConflictError, match="executing management root"):
        service.start_task(manager_context, command_id="cancel-during-prepare", purpose="Inspect", workspace_path=str(tmp_path))
    assert counts(runtime) == before
    assert set(runtime.agent_run_manager._runs) == before_cached
    assert runtime.agent_run_store.load_run(manager_context.run_id)["metadata"]["cancel_requested"] is True


def test_start_rechecks_dispatch_fence_in_its_write_transaction(runtime, manager_context, monkeypatch):
    before = counts(runtime)
    before_cached = set(runtime.agent_run_manager._runs)
    checks = []

    def lose_at_commit(run_id, *, conn=None):
        assert run_id == manager_context.run_id
        checks.append(conn is not None)
        if conn is not None:
            assert conn.in_transaction
            raise AgentDispatchLeaseLost("dispatch_lease_lost")

    monkeypatch.setattr(runtime.managed_tasks, "guard", lose_at_commit)
    with pytest.raises(AgentDispatchLeaseLost, match="dispatch_lease_lost"):
        runtime.managed_tasks.start_task(manager_context, command_id="lose-during-prepare", purpose="Inspect")
    assert checks == [False, True]
    assert counts(runtime) == before
    assert set(runtime.agent_run_manager._runs) == before_cached


def test_ordinary_child_and_paused_controller_cannot_issue_task_commands(runtime, manager_context):
    service = runtime.managed_tasks
    ordinary = new_target(runtime, status="running")
    bad_context = ToolContext(session_id=ordinary.session_id, run_id=ordinary.run_id)
    with pytest.raises(ManagementConflictError):
        service.start_task(bad_context, command_id="ordinary", purpose="Forbidden")
    child = runtime.agent_run_manager.create_child_run(parent_run_id=manager_context.run_id, plan_id="analysis", step_id="child", attempt=1, user_input="Child")
    runtime.agent_run_manager.mark_child_running(child.run_id)
    with pytest.raises(ManagementConflictError):
        service.start_task(ToolContext(session_id=child.session_id, run_id=child.run_id), command_id="child", purpose="Forbidden")
    runtime.management_service.update(manager_context.session_id, expected_revision=2, status="paused")
    with pytest.raises(ManagementConflictError, match="Activate"):
        service.start_task(manager_context, command_id="paused", purpose="Forbidden")


def test_send_rejects_stale_run_and_running_target_then_replaces_paused(runtime, manager_context):
    service = runtime.managed_tasks
    old = new_target(runtime, status="running")
    with pytest.raises(ManagementConflictError, match="Target run changed"):
        service.send_instruction(manager_context, command_id="stale", session_id=old.session_id, expected_run_id="other", purpose="New")
    with pytest.raises(ManagementConflictError, match="Pause the target"):
        service.send_instruction(manager_context, command_id="busy", session_id=old.session_id, expected_run_id=old.run_id, purpose="New")
    runtime.agent_run_manager.request_pause(old.run_id)
    runtime.agent_run_manager.acknowledge_pause(old.run_id)
    receipt = service.send_instruction(manager_context, command_id="replace", session_id=old.session_id, expected_run_id=old.run_id, purpose="New")
    assert runtime.agent_run_manager.get_run(old.run_id).status == AgentRunStatus.CANCELLED
    replacement = runtime.agent_run_store.load_run(receipt["run_id"])
    assert replacement["session_id"] == old.session_id and replacement["parent_run_id"] is None
    assert replacement["metadata"]["input_envelope"]["origin"] == "manager"
    after = counts(runtime)
    assert service.send_instruction(manager_context, command_id="replace", session_id=old.session_id, expected_run_id=old.run_id, purpose="New") == receipt
    assert counts(runtime) == after


def test_replacement_receipt_failure_preserves_original_run_and_slot(runtime, manager_context, monkeypatch):
    old = new_target(runtime, status="paused")
    before = counts(runtime)
    monkeypatch.setattr(runtime.managed_tasks, "_receipt", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("receipt failed")))
    with pytest.raises(OSError, match="receipt failed"):
        runtime.managed_tasks.send_instruction(manager_context, command_id="failed-replace", session_id=old.session_id,
                                                expected_run_id=old.run_id, purpose="New")
    assert counts(runtime) == before
    assert runtime.agent_run_manager.get_run(old.run_id).status == AgentRunStatus.PAUSED
    assert runtime.agent_run_store.load_run(old.run_id)["status"] == "paused"
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT run_id FROM session_admissions WHERE session_id=?", (old.session_id,)).fetchone()[0] == old.run_id


@pytest.mark.parametrize("status", ["waiting_user", "waiting_confirmation"])
def test_resume_control_does_not_bypass_waiting_question_or_approval(runtime, manager_context, status):
    target = new_target(runtime, status=status)
    before = counts(runtime)
    with pytest.raises(ManagementConflictError, match="original confirmation/answer"):
        runtime.managed_tasks.control_run(manager_context, command_id="resume-waiting", run_id=target.run_id, action="resume")
    assert counts(runtime) == before
    assert runtime.agent_run_manager.get_run(target.run_id).status.value == status


@pytest.mark.parametrize("waiting_status", ["waiting_user", "waiting_confirmation"])
def test_send_cannot_replace_paused_question_or_confirmation(runtime, manager_context, waiting_status):
    target = new_target(runtime, status=waiting_status)
    runtime.agent_run_manager.request_pause(target.run_id)
    runtime.agent_run_manager.acknowledge_pause(target.run_id)
    paused = runtime.agent_run_manager.get_run(target.run_id)
    assert paused.status == AgentRunStatus.PAUSED
    assert paused.metadata["paused_from_status"] == waiting_status
    before = counts(runtime)
    with pytest.raises(ManagementConflictError, match="original confirmation or user question"):
        runtime.managed_tasks.send_instruction(manager_context, command_id="do-not-replace-waiting", session_id=target.session_id,
                                                expected_run_id=target.run_id, purpose="Another instruction")
    assert counts(runtime) == before
    assert runtime.agent_run_manager.get_run(target.run_id).status == AgentRunStatus.PAUSED


@pytest.mark.parametrize("action", ["start", "send", "resume"])
def test_memory_checkpoint_rejects_new_dispatch_commands(runtime, manager_context, action):
    target = new_target(runtime, status="paused") if action != "start" else None
    runtime.local_app_config.agent.checkpoint_backend = "memory"
    before = counts(runtime)
    with pytest.raises(ManagementConflictError, match="checkpoint_backend=sqlite"):
        if action == "start":
            runtime.managed_tasks.start_task(manager_context, command_id="memory-start", purpose="Inspect")
        elif action == "send":
            runtime.managed_tasks.send_instruction(manager_context, command_id="memory-send", session_id=target.session_id,
                                                    expected_run_id=target.run_id, purpose="New instruction")
        else:
            runtime.managed_tasks.control_run(manager_context, command_id="memory-resume", run_id=target.run_id, action="resume")
    assert counts(runtime) == before


def test_memory_checkpoint_setting_does_not_hide_prior_start_receipt(runtime, manager_context):
    receipt = runtime.managed_tasks.start_task(manager_context, command_id="saved-before-memory", purpose="Inspect")
    before = counts(runtime)
    runtime.local_app_config.agent.checkpoint_backend = "memory"
    assert runtime.managed_tasks.start_task(manager_context, command_id="saved-before-memory", purpose="Inspect") == receipt
    assert counts(runtime) == before


def test_read_pages_and_run_views_exclude_metadata_and_raw_run_input(runtime, manager_context):
    target = new_target(runtime)
    runtime.agent_run_manager._update_run(target.run_id, status=AgentRunStatus.RUNNING,
                                         metadata_patch={"raw_prompt": "DO_NOT_DISCLOSE", "secret_debug": "PRIVATE"})
    runtime.session_service.append_message(session_id=target.session_id, role="user", content="x" * 4500,
                                           payload={"raw_prompt": "DO_NOT_DISCLOSE"}, message_id="long")
    runtime.session_service.append_message(session_id=target.session_id, role="agent", content="Answer", message_id="reply")
    service = runtime.managed_tasks
    page = service.read_session(manager_context, session_id=target.session_id, limit=1)
    assert page["next_offset"] == 1 and len(page["messages"][0]["content"]) == 4000
    assert page["messages"][0]["next_char"] == 4000
    tail = service.read_session(manager_context, session_id=target.session_id, message_id="long", start_char=4000)
    assert tail["messages"][0]["content"] == "x" * 500
    assert tail["messages"][0]["next_char"] is None
    outputs = [page, service.list_sessions(manager_context, limit=1), service.list_runs(target.session_id), service.inspect_run(manager_context, run_id=target.run_id)]
    assert all("DO_NOT_DISCLOSE" not in str(output) and "raw_prompt" not in str(output) and "user_input" not in str(output) for output in outputs)


def test_management_write_tool_requires_existing_review_gate(runtime, manager_context):
    tool = next(tool for tool in management_tools(runtime.managed_tasks) if tool.action == "start_task")
    invocation = ToolInvocation(invocation_id="inv", tool=tool.spec, session_id=manager_context.session_id,
                                 context_id="context", input={"command_id": "review", "purpose": "Inspect"})
    with pytest.raises(ValueError, match="review gate"):
        tool.invoke(invocation=invocation, context=manager_context.model_copy(update={"safety_review_approved": False}))
    result = tool.invoke(invocation=invocation, context=manager_context)
    assert result.status == "completed" and result.output["action"] == "start_task"
