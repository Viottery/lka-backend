import pytest

from app.core.context_driver import ToolView
from app.domains.remote_execution import RemoteExecutionConfig
from tests.test_management_api import runtime as runtime_fixture
from tests.test_remote_execution import setup_remote as setup_remote_fixture
from tests.test_remote_execution import start
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def setup_remote(tmp_path, monkeypatch):
    hub_generator = runtime_fixture.__wrapped__(tmp_path, monkeypatch)
    hub = next(hub_generator)
    try:
        yield from setup_remote_fixture.__wrapped__(hub, tmp_path, monkeypatch)
    finally:
        hub_generator.close()


def _admit(e):
    receipt = start(e, device_id="pc", workspace_id="project")
    e["worker"].run_once()
    return e["node"].agent_run_manager.get_run(receipt["run_id"])


def test_local_policy_grants_registered_non_management_tool_with_source_intersection(setup_remote):
    e = setup_remote
    e["worker"].config.tool_policy = "local"
    e["worker"].config.allowed_tools = []
    e["context"] = e["context"].model_copy(update={
        "tool_view": ToolView(snapshot_id="test-source", side_effect_level="external",
                              allowed_tools=("matter.search", "management.start_task"))
    })

    run = _admit(e)
    allowed = run.metadata["session_policy"]["allowed_tools"]
    assert "matter.search" in allowed
    assert "management.start_task" not in allowed
    registered = {tool.name for tool in e["node"].tool_registry.list_tools()}
    assert set(allowed) <= {name for name in registered if not name.startswith("management.")}
    assert run.metadata["context_views"]["tool"]["host_execution_authority"] is False


def test_default_policy_remains_allowlist_only_filesystem(setup_remote):
    e = setup_remote
    assert RemoteExecutionConfig().tool_policy == "allowlist"
    run = _admit(e)
    assert run.metadata["session_policy"]["allowed_tools"] == ["filesystem.read_file"]
    assert "matter.search" not in run.metadata["session_policy"]["allowed_tools"]


def test_local_tool_policy_does_not_grant_host_shell(setup_remote):
    e = setup_remote
    e["worker"].config.tool_policy = "local"
    e["worker"].config.shell_trust = "disabled"
    e["context"] = e["context"].model_copy(update={
        "tool_view": ToolView(snapshot_id="test-source", side_effect_level="external",
                              allowed_tools=("bash.run",))
    })
    run = _admit(e)
    assert "bash.run" in run.metadata["session_policy"]["allowed_tools"]
    assert run.metadata["context_views"]["tool"]["host_execution_authority"] is False


def test_local_policy_cannot_expand_frozen_permissions_on_same_session_followup(setup_remote):
    e = setup_remote
    first = start(e, device_id="pc", workspace_id="project")
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="First task finished"), "First result."
    ])
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("first-local-policy")
    e["worker"].run_once()

    original = e["node"].agent_run_manager.get_run(first["run_id"])
    assert original.metadata["session_policy"]["allowed_tools"] == ["filesystem.read_file"]
    e["worker"].config.tool_policy = "local"
    e["worker"].config.allowed_tools = []

    followup = e["hub"].managed_tasks.send_instruction(
        e["context"], command_id="local-policy-followup", session_id=first["session_id"],
        expected_run_id=first["run_id"], purpose="Explain the existing result",
    )
    e["worker"].run_once()
    continued = e["node"].agent_run_manager.get_run(followup["run_id"])
    assert continued is not None
    assert continued.metadata["session_policy"]["allowed_tools"] == ["filesystem.read_file"]
    assert "matter.search" not in continued.metadata["session_policy"]["allowed_tools"]
    assert "mail.list" not in continued.metadata["session_policy"]["allowed_tools"]
