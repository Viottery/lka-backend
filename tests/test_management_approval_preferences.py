"""Approval preferences survive independent dispatch without bypassing blacklists."""
from contextlib import closing

import pytest

from app.core.agent_graph import AgentTurnWaitingForConfirmation
from app.core.agent_runs import AgentRunStatus
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewDecision, SafetyReviewMode
from app.domains.management_delegates import ManagementDelegateService
from app.storage.db import connect
from tests.test_management_delegate_approval import register
from tests.test_management_delegates import child_context
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_management_tasks import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def approve(runtime, review):
    return runtime.agent_run_manager.decide_safety_review(
        review_id=review.review_id, decision=SafetyReviewDecision.APPROVE,
        decided_by="user.synthetic-test", reason="Explicit isolated approval",
    )


@pytest.mark.parametrize("mode,resumable", [
    ("skip", False), ("llm", False), ("skip", True), ("llm", True), ("manual", True),
])
def test_actual_manager_turn_carries_preference_into_execution(runtime, tmp_path, monkeypatch, mode, resumable):
    effects = []
    register(runtime, effects, risk="high", confirmation=True)
    manager = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(manager, expected_revision=1, status="active")
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.MANUAL
    monkeypatch.setattr(runtime.agent_turn_loop, "_decide_safety_review_with_llm",
                        lambda review, **kwargs: approve(runtime, review))
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.start_task", tool_input={
            "command_id": "inherit", "purpose": "Run a bounded synthetic operation", "workspace_path": str(tmp_path),
        }),
        operation("final_answer", reason="Work delegated"), "Delegated.",
    ])
    try:
        runtime.run_agent_turn(session_id=manager, user_input="Delegate this operation",
                               safety_review_mode=SafetyReviewMode(mode), resumable=resumable)
    except AgentTurnWaitingForConfirmation as waiting:
        assert mode == "manual"
        approve(runtime, runtime.agent_run_manager.get_safety_review(waiting.review_id))
        runtime.resume_agent_run(waiting.run_id)
    with closing(runtime._conn()) as conn:
        row = conn.execute("SELECT run_id FROM managed_session_links WHERE manager_session_id=?", (manager,)).fetchone()
    run = runtime.agent_run_manager.get_run(row[0])
    assert run.metadata["session_policy"]["safety_review_mode"] == mode
    # A stricter global default is a fallback, not an override of this inherited preference.
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="approval_probe"),
        operation("tool_call", tool_name="approval_probe.write", tool_input={}),
        operation("final_answer", reason="Operation recorded"), "Recorded.",
    ])
    assert runtime.agent_dispatch.run_one()
    [review] = runtime.agent_run_manager.list_safety_reviews(run.run_id)
    assert review.mode.value == mode
    if mode == "manual":
        assert effects == []
        assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.WAITING_CONFIRMATION
        approve(runtime, review)
        runtime.resume_agent_run(run.run_id)
    assert effects == [run.run_id]
    assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED


@pytest.mark.parametrize("mode", ["skip", "llm", "manual"])
def test_delegate_mode_survives_restart_nested_dispatch_and_followup(runtime, mode):
    parent = context_fixture.__wrapped__(runtime)
    parent.safety_review_mode = mode
    delegate = runtime.management_delegates.delegate_work(parent, command_id="line", objective="Bounded work")
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        with closing(reopened._conn()) as conn:
            stored = reopened.management_delegates.get_for_session(conn, delegate["session_id"])
        assert stored["safety_review_mode"] == mode
        assert reopened._session_run_policy(delegate["session_id"]).safety_review_mode == mode
        # Coordinator-generated kickoff is asynchronous and has no foreground request options.
        reopened.management_coordinator.tick()
        assert reopened.management_coordinator.run_one()
        with closing(reopened._conn()) as conn:
            row = conn.execute("SELECT run_id FROM agent_runs WHERE session_id=?", (delegate["session_id"],)).fetchone()
        kickoff = reopened.agent_run_manager.get_run(row[0])
        assert kickoff.metadata["session_policy"]["safety_review_mode"] == mode
        reopened.agent_run_manager.mark_running(kickoff.run_id)
        reopened.agent_run_manager.complete_run(kickoff.run_id)
        caller = child_context(reopened, delegate["session_id"])
        first = reopened.managed_tasks.start_task(caller, command_id="nested", purpose="Inspect state")
        task = reopened.agent_run_manager.get_run(first["run_id"])
        assert task.metadata["session_policy"]["safety_review_mode"] == mode
        reopened.agent_run_manager.mark_running(task.run_id)
        reopened.agent_run_manager.complete_run(task.run_id)
        # A later parent preference does not silently mutate an already-open task session.
        caller.safety_review_mode = "manual" if mode == "skip" else "skip"
        second = reopened.managed_tasks.send_instruction(
            caller, command_id="followup", session_id=task.session_id,
            expected_run_id=task.run_id, purpose="Continue bounded work",
        )
        assert reopened.agent_run_manager.get_run(second["run_id"]).metadata["session_policy"]["safety_review_mode"] == mode
        reopened.agent_run_manager.mark_running(second["run_id"])
        reopened.agent_run_manager.request_pause(second["run_id"])
        reopened.agent_run_manager.acknowledge_pause(second["run_id"])
        replacement = reopened.managed_tasks.send_instruction(
            caller, command_id="replace", session_id=task.session_id,
            expected_run_id=second["run_id"], purpose="Revised bounded work",
        )
        assert reopened.agent_run_manager.get_run(replacement["run_id"]).metadata["session_policy"]["safety_review_mode"] == mode
    finally:
        reopened.stop()


def test_queued_work_freezes_approval_at_acceptance(runtime):
    parent = context_fixture.__wrapped__(runtime)
    parent.safety_review_mode = "manual"
    receipt = runtime.management_queue.queue_work(parent, command_id="queued", purpose="Read bounded state")
    parent.safety_review_mode = "skip"
    run = runtime.agent_run_manager.get_run(receipt["run_id"])
    assert run.metadata["session_policy"]["safety_review_mode"] == "manual"
    runtime.agent_run_manager.complete_run(parent.run_id)
    assert runtime.management_queue.tick() == 1
    assert runtime.agent_run_manager.get_run(run.run_id).metadata["session_policy"]["safety_review_mode"] == "manual"


def test_management_dispatch_default_overrides_request_for_new_local_work(runtime):
    parent = context_fixture.__wrapped__(runtime)
    parent.safety_review_mode = "manual"
    runtime.managed_tasks.review_mode = lambda: "llm"

    task = runtime.managed_tasks.start_task(parent, command_id="default-task", purpose="Inspect bounded state")
    queued = runtime.management_queue.queue_work(
        parent, command_id="default-queue", purpose="Queue bounded read", read_only_work=True,
    )
    matter = runtime.management_work.organize_work(
        parent, "default-matter", [{"op": "create", "ref": "work", "fields": {
            "title": "Bounded work", "visibility": "user",
        }}],
    )["refs"]["work"]
    assigned = runtime.management_assignments.start_work(
        parent, command_id="default-assignment", matter_id=matter, expected_revision=1,
        purpose="Start assigned bounded work",
    )

    for receipt in (task, queued, assigned):
        run = runtime.agent_run_manager.get_run(receipt["run_id"])
        assert run.metadata["session_policy"]["safety_review_mode"] == "llm"


def test_management_dispatch_default_overrides_request_for_delegate(runtime):
    parent = context_fixture.__wrapped__(runtime)
    parent.safety_review_mode = "manual"
    runtime.managed_tasks.review_mode = lambda: "llm"

    delegate = runtime.management_delegates.delegate_work(
        parent, command_id="default-delegate", objective="A bounded delegated line",
    )
    with closing(runtime._conn()) as conn:
        stored = runtime.management_delegates.get_for_session(conn, delegate["session_id"])
    assert stored["safety_review_mode"] == "llm"
    assert runtime._session_run_policy(delegate["session_id"]).safety_review_mode == "llm"

    # Remote continuation resolves configured policy ahead of the caller and
    # predecessor snapshots, while an unset policy keeps legacy precedence.
    assert runtime.managed_tasks.dispatch_review_mode("manual", "skip") == "llm"
    runtime.managed_tasks.review_mode = lambda: None
    assert runtime.managed_tasks.dispatch_review_mode("manual", "skip") == "manual"
    assert runtime.managed_tasks.dispatch_review_mode(None, "skip") == "skip"


def test_remote_start_and_continuation_use_management_dispatch_default(runtime, tmp_path, monkeypatch):
    from tests.test_remote_execution import setup_remote as remote_setup

    generator = remote_setup.__wrapped__(runtime, tmp_path, monkeypatch)
    env = next(generator)
    try:
        env["context"].safety_review_mode = "manual"
        runtime.managed_tasks.review_mode = lambda: "llm"
        env["worker"].config.review_policy = "inherit"
        receipt = env["hub"].managed_tasks.start_task(
            env["context"], command_id="review-default-remote", purpose="Read the project",
            execution_target={"device_id": "pc", "environment_id": "linux", "workspace_id": "project"},
        )
        original = runtime.agent_run_manager.get_run(receipt["run_id"])
        assert original.metadata["session_policy"]["safety_review_mode"] == "llm"
        assert original.metadata["remote_approval_requested"] == "llm"

        env["node"].agent_turn_loop.llm_client = ScriptedClient([
            operation("final_answer", reason="Read complete"), "Project read.",
        ])
        env["worker"].run_once()
        assert env["node"].agent_dispatch.run_one("review-default-first")
        env["worker"].run_once()

        continuation = runtime.managed_tasks.send_instruction(
            env["context"], command_id="review-default-remote-next", session_id=receipt["session_id"],
            expected_run_id=receipt["run_id"], purpose="Continue reading the project",
        )
        continued = runtime.agent_run_manager.get_run(continuation["run_id"])
        assert continued.metadata["session_policy"]["safety_review_mode"] == "llm"
        assert continued.metadata["remote_approval_requested"] == "llm"
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


def test_dispatch_mode_default_none_preserves_explicit_manual(runtime):
    parent = context_fixture.__wrapped__(runtime)
    parent.safety_review_mode = "manual"
    runtime.managed_tasks.review_mode = lambda: None
    receipt = runtime.managed_tasks.start_task(parent, command_id="legacy-mode", purpose="Keep old policy")
    run = runtime.agent_run_manager.get_run(receipt["run_id"])
    assert run.metadata["session_policy"]["safety_review_mode"] == "manual"
    assert runtime.agent_turn_loop.safety_review_mode == SafetyReviewMode.SKIP


@pytest.mark.parametrize("mode", ["skip", "llm"])
def test_ordinary_bash_write_uses_inherited_mode_without_manual_prompt(runtime, tmp_path, monkeypatch, mode):
    parent = context_fixture.__wrapped__(runtime)
    parent.safety_review_mode = mode
    task = runtime.managed_tasks.start_task(parent, command_id="ordinary-bash", purpose="Write one test artifact",
                                           workspace_path=str(tmp_path))
    monkeypatch.setattr(runtime.agent_turn_loop, "_decide_safety_review_with_llm",
                        lambda review, **kwargs: approve(runtime, review))
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="bash"),
        operation("tool_call", tool_name="bash.run", tool_input={"command": "printf tested > artifact.txt"}),
        operation("final_answer", reason="Artifact created"), "Created.",
    ])
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(task["run_id"]).status == AgentRunStatus.COMPLETED
    assert (tmp_path / "artifact.txt").read_text() == "tested"
    [review] = runtime.agent_run_manager.list_safety_reviews(task["run_id"])
    assert review.mode.value == mode


@pytest.mark.parametrize("mode", ["skip", "llm"])
def test_real_bash_blacklist_waits_without_executing(runtime, tmp_path, monkeypatch, mode):
    parent = context_fixture.__wrapped__(runtime)
    parent.safety_review_mode = mode
    task = runtime.managed_tasks.start_task(parent, command_id="destructive", purpose="Synthetic review probe",
                                           workspace_path=str(tmp_path))
    target = tmp_path / "keep"
    target.mkdir()
    (target / "data.txt").write_text("must survive")
    monkeypatch.setattr(runtime.tool_registry.get_tool("bash.run"), "_run_sync",
                        lambda **kwargs: pytest.fail("Blacklisted shell effect executed"))
    monkeypatch.setattr(runtime.agent_turn_loop, "_decide_safety_review_with_llm",
                        lambda *args, **kwargs: pytest.fail("Blacklist was sent for model approval"))
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="bash"),
        operation("tool_call", tool_name="bash.run", tool_input={"command": "rm -rf keep"}),
    ])
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(task["run_id"]).status == AgentRunStatus.WAITING_CONFIRMATION
    [review] = runtime.agent_run_manager.list_safety_reviews(task["run_id"])
    assert review.mode == SafetyReviewMode.MANUAL and "blacklist" in review.reason
    assert (target / "data.txt").read_text() == "must survive"


def test_legacy_delegate_schema_adds_nullable_preference_without_changing_records(tmp_path):
    path = tmp_path / "old.sqlite3"
    with closing(connect(path)) as conn, conn:
        conn.execute("CREATE TABLE manager_delegates (delegate_id TEXT PRIMARY KEY,session_id TEXT UNIQUE,"
                     "parent_session_id TEXT,objective TEXT,allowed_tools TEXT,workspace TEXT,kickoff_envelope TEXT,"
                     "state TEXT,revision INTEGER,created_at TEXT,updated_at TEXT)")
        row = ("d", "s", "p", "Keep objective", "[]", None, "{}", "active", 3, "created", "updated")
        conn.execute("INSERT INTO manager_delegates VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
    service = ManagementDelegateService(lambda: connect(path), tasks=None, management=None, goals=None, registry=None)
    service.ensure_schema()
    service.ensure_schema()
    with closing(connect(path)) as conn:
        stored = conn.execute("SELECT * FROM manager_delegates").fetchone()
        assert tuple(stored) == (*row, None)
