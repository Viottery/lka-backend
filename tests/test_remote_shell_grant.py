"""Native execution grants preserve scope denials and per-call human review."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.context_driver import ToolView
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolSpec,
    tool_scope_discovery_denial,
)
from app.domains.remote_shell import approve_host_execution_authority
from app.tool_packages.bash import (
    BashAccessPolicy,
    BashRunTool,
    BashSessionManager,
    _child_run_id,
)


def view(**changes):
    values = {"snapshot_id": "remote-task", "child_run_id": "run", "side_effect_level": "external",
                  "allowed_tools": ("bash.run",), "allowed_paths": ()}
    values.update(changes)
    return ToolView(**values)


def grant(**changes):
    values = {"shell_trust": "host_user", "allowed_tools": ("bash.run",), "source_view": None}
    values.update(changes)
    return approve_host_execution_authority(**values)


def test_host_execution_requires_explicit_operator_and_tool_grants():
    assert not view().host_execution_authority
    assert not grant(shell_trust="disabled")
    assert not grant(shell_trust="true")
    assert not grant(allowed_tools=("bash",))
    assert not grant(allowed_tools=("bash.write_session",))
    assert not grant(mounted_workspace=True)
    assert not grant(effect_constraints={"message-source": {}})
    assert grant()


@pytest.mark.parametrize("changes", [
    {"full_workspace_authority": False}, {"full_data_authority": False},
    {"allowed_paths": ("/bounded",)}, {"allowed_source_ids": ("source",)},
    {"allowed_account_ids": ("account",)}, {"read_only_tools": ("bash.run",)},
    {"max_tool_calls": 2}, {"side_effect_level": "read"},
    {"allowed_tools": ("filesystem.read_file",)},
    {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
])
def test_inherited_restrictions_cannot_be_bypassed(changes):
    values = {"full_workspace_authority": True, "full_data_authority": True}
    values.update(changes)
    assert not grant(source_view=view(**values))


def test_unrestricted_source_can_delegate_but_host_grant_does_not_grant_data():
    assert grant(source_view=view(full_workspace_authority=True, full_data_authority=True))
    target = view(host_execution_authority=True)
    assert not target.full_workspace_authority
    assert not target.full_data_authority
    assert tool_scope_discovery_denial(BashRunTool.spec, target) is None
    ordinary = ToolSpec(name="filesystem.read_file", type="local_tool", description="Read a file", read_only=True,
                        scope_uses_workspace=True, scope_path_fields=("path",))
    assert tool_scope_discovery_denial(ordinary, target) is not None
    assert tool_scope_discovery_denial(BashRunTool.spec, view()) is not None


def test_typed_grant_cannot_be_forged_by_alias_or_string():
    with pytest.raises(ValidationError):
        view(host_execution_authority="true")
    with pytest.raises(ValidationError):
        view(shell_trust="host_user")


def executor(tmp_path, monkeypatch, *, review_mode="manual"):
    root = tmp_path / "workspace"
    root.mkdir()
    tool = BashRunTool(policy=BashAccessPolicy.from_workspace_roots([root]),
                       session_manager=BashSessionManager())
    calls = []
    monkeypatch.setattr(tool, "_run_sync", lambda **kwargs: calls.append(kwargs) or {"exit_code": 0})
    registry = ToolRegistry()
    registry.register_tool(tool)
    result = ToolExecutor(registry)
    review = SimpleNamespace(status=SimpleNamespace(value="approved"), mode=review_mode,
                             invocation_id="call", tool_name="bash.run", tool_input={"command": "pwd"},
                             session_id="session", run_id="run", workspace_root=str(root), workspace_access=[])
    run = SimpleNamespace(status=SimpleNamespace(value="running"), parent_run_id=None,
                          session_id="session")
    result.run_manager = SimpleNamespace(get_safety_review=lambda _: review, get_run=lambda _: run,
                                        is_cancel_requested=lambda _: False)
    result.host_execution_guard = lambda _: None
    context = ToolContext(session_id="session", run_id="run", workspace_root=str(root),
                          safety_review_approved=True, safety_review_id="review",
                          safety_review_mode="manual",
                          tool_view=view(host_execution_authority=True, allowed_paths=(str(root),)))
    return result, context, calls, review


def execute(executor, context):
    return executor.execute(invocation_id="call", tool_name="bash.run",
                            tool_input={"command": "pwd"}, context=context)


def test_explicit_grant_runs_through_matching_manual_review(tmp_path, monkeypatch):
    runner, context, calls, _ = executor(tmp_path, monkeypatch)
    result = execute(runner, context)
    assert result.status == "completed"
    assert len(calls) == 1
    assert _child_run_id(context) == "run"


@pytest.mark.parametrize("mode", ["skip", "llm"])
def test_read_only_shell_still_requires_manual_approval(tmp_path, monkeypatch, mode):
    runner, context, calls, _ = executor(tmp_path, monkeypatch, review_mode=mode)
    assert execute(runner, context).status == "rejected"
    assert not calls


def test_grant_cannot_bypass_missing_review_or_revoked_trust(tmp_path, monkeypatch):
    runner, context, calls, review = executor(tmp_path, monkeypatch)
    assert execute(runner, context.model_copy(update={"safety_review_mode": "skip"})).status == "rejected"
    review.invocation_id = "another-call"
    assert execute(runner, context).status == "rejected"
    review.invocation_id = "call"
    runner.host_execution_guard = None
    assert execute(runner, context).status == "rejected"
    runner.host_execution_guard = lambda _: "Operator trust revoked"
    assert execute(runner, context).error == "Operator trust revoked"
    assert not calls


def test_grant_rechecked_after_waiting_for_admission(tmp_path, monkeypatch):
    runner, context, calls, _ = executor(tmp_path, monkeypatch)
    checks = iter([None, "Operator trust revoked"])
    runner.host_execution_guard = lambda _: next(checks)
    assert execute(runner, context).error == "Operator trust revoked"
    assert not calls


def test_shell_session_lookup_never_returns_another_runs_session():
    manager = BashSessionManager()
    manager._sessions["other"] = SimpleNamespace(owner_run_id="another-run")
    with pytest.raises(KeyError):
        manager.get("other", owner_run_id="run")
    manager._sessions.clear()
    assert _child_run_id(view_context := ToolContext(session_id="session", run_id="root",
                         tool_view=view(child_run_id=None, host_execution_authority=True))) == "root"
    assert view_context.tool_view.full_workspace_authority is False
