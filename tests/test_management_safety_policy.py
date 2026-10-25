"""Server-owned autonomous review policy survives independent task boundaries."""
from contextlib import closing

import pytest
from pydantic import ValidationError

from app.core.local_config import ManagementConfig
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.sessions import SessionRunPolicy
from tests.test_management_delegates import child_context
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_management_tasks import runtime as runtime_fixture
from tests.test_remote_execution import setup_remote as remote_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def policy(runtime, receipt):
    return runtime.agent_run_manager.get_run(receipt["run_id"]).metadata["session_policy"]


def test_policy_defaults_and_scope(runtime):
    assert ManagementConfig().safety_review_policy == "standard"
    assert SessionRunPolicy().safety_review_policy == "standard"
    with pytest.raises(ValidationError):
        ManagementConfig(safety_review_policy="allow_everything")
    parent = context_fixture.__wrapped__(runtime)
    runtime.local_app_config.management.safety_review_policy = "autonomous"
    assert runtime._session_run_policy(parent.session_id).safety_review_policy == "autonomous"
    ordinary = runtime.session_service.create_session(title="Independent ordinary session").session
    assert runtime._session_run_policy(ordinary.session_id).safety_review_policy == "standard"


def test_local_queue_assignment_freeze_policy_at_admission(runtime):
    parent = context_fixture.__wrapped__(runtime)
    runtime.local_app_config.management.safety_review_mode = "llm"
    runtime.local_app_config.management.safety_review_policy = "autonomous"
    task = runtime.managed_tasks.start_task(parent, command_id="policy-task", purpose="Inspect bounded state")
    queued = runtime.management_queue.queue_work(parent, command_id="policy-queue", purpose="Inspect later")
    matter = runtime.management_work.organize_work(
        parent, "policy-matter", [{"op": "create", "ref": "work", "fields": {
            "title": "Bounded work", "visibility": "user",
        }}],
    )["refs"]["work"]
    assigned = runtime.management_assignments.start_work(
        parent, command_id="policy-assignment", matter_id=matter, expected_revision=1,
        purpose="Inspect assigned work",
    )
    runtime.local_app_config.management.safety_review_policy = "standard"
    for receipt in (task, queued, assigned):
        assert policy(runtime, receipt)["safety_review_policy"] == "autonomous"
        assert policy(runtime, receipt)["safety_review_mode"] == "llm"
    runtime.agent_run_manager.complete_run(parent.run_id)
    assert runtime.management_queue.tick() == 1
    assert policy(runtime, queued)["safety_review_policy"] == "autonomous"


def test_delegate_policy_persists_without_schema_migration(runtime):
    parent = context_fixture.__wrapped__(runtime)
    runtime.local_app_config.management.safety_review_policy = "autonomous"
    delegate = runtime.management_delegates.delegate_work(parent, command_id="policy-line", objective="Bounded line")
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        reopened.local_app_config.management.safety_review_policy = "standard"
        assert reopened._session_run_policy(delegate["session_id"]).safety_review_policy == "autonomous"
        with closing(reopened._conn()) as conn:
            stored = reopened.management_delegates.get_for_session(conn, delegate["session_id"])
        assert stored["kickoff_envelope"]["debug_info"]["safety_review_policy"] == "autonomous"
        # New child tasks use the current server policy, while accepted delegate
        # runs retain the policy frozen when the line was commissioned.
        reopened.local_app_config.management.safety_review_policy = "autonomous"
        child = child_context(reopened, delegate["session_id"])
        task = reopened.managed_tasks.start_task(child, command_id="policy-child", purpose="Inspect state")
        assert policy(reopened, task)["safety_review_policy"] == "autonomous"
        reopened.agent_run_manager.mark_running(task["run_id"])
        reopened.agent_run_manager.complete_run(task["run_id"])
        continued = reopened.managed_tasks.send_instruction(
            child, command_id="policy-child-next", session_id=task["session_id"],
            expected_run_id=task["run_id"], purpose="Inspect remaining state",
        )
        assert policy(reopened, continued)["safety_review_policy"] == "autonomous"
    finally:
        reopened.stop()


@pytest.mark.parametrize("node_review", ["inherit", "manual"])
def test_remote_policy_respects_node_authority_and_freezes_continuation(runtime, tmp_path, monkeypatch, node_review):
    generator = remote_fixture.__wrapped__(runtime, tmp_path, monkeypatch)
    env = next(generator)
    try:
        runtime.local_app_config.management.safety_review_mode = "llm"
        runtime.local_app_config.management.safety_review_policy = "autonomous"
        env["worker"].config.review_policy = node_review
        task = runtime.managed_tasks.start_task(
            env["context"], command_id="policy-remote", purpose="Read project",
            execution_target={"device_id": "pc", "environment_id": "linux", "workspace_id": "project"},
        )
        assert policy(runtime, task)["safety_review_policy"] == "autonomous"
        env["node"].agent_turn_loop.llm_client = ScriptedClient([
            operation("final_answer", reason="Read complete"), "Read complete.",
        ])
        env["worker"].run_once()
        effective = policy(env["node"], task)
        assert effective["safety_review_policy"] == ("autonomous" if node_review == "inherit" else "standard")
        assert effective["safety_review_mode"] == ("llm" if node_review == "inherit" else "manual")
        assert env["node"].agent_dispatch.run_one("policy-first")
        env["worker"].run_once()
        continued = runtime.managed_tasks.send_instruction(
            env["context"], command_id="policy-remote-next", session_id=task["session_id"],
            expected_run_id=task["run_id"], purpose="Read more",
        )
        assert policy(runtime, continued)["safety_review_policy"] == "autonomous"
        runtime.local_app_config.management.safety_review_policy = "standard"
        assert policy(runtime, continued)["safety_review_policy"] == "autonomous"
    finally:
        try:
            next(generator)
        except StopIteration:
            pass
