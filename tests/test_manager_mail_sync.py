"""Manager refresh permission through real discovery, review and tool execution."""
from __future__ import annotations

import pytest

from app.core.agent_graph import AgentTurnWaitingForConfirmation
from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewDecision, SafetyReviewMode
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture(params=["legacy", "langgraph"])
def runtime(request, tmp_path, monkeypatch):
    config = LocalAppConfig()
    config.management.enabled = True
    config.agent.orchestrator = request.param
    config.agent.unified_entry_enabled = True
    config.memory.enabled = False
    config.message_history.enabled = False
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    instance = LocalKnowledgeAgentRuntime(Settings(
        LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path),
    ))
    yield instance
    instance.stop()


def manager(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    return sid


def test_only_active_manager_gets_sync_and_other_write_restrictions_remain(runtime):
    sid = manager(runtime)
    policy = runtime._session_run_policy(sid)
    assert "mail.sync" in policy.allowed_tools
    assert "mail.sync" not in policy.read_only_tools
    for name in ("filesystem.create_file", "filesystem.edit_file", "matter.create", "matter.update"):
        assert name not in policy.allowed_tools
    assert "bash.run" in policy.read_only_tools
    spec = runtime.tool_registry.get_tool("mail.sync").spec
    assert spec.read_only is False
    runtime.management_service.update(sid, expected_revision=2, status="paused")
    assert "mail.sync" not in runtime._session_run_policy(sid).allowed_tools
    assert "mail.sync" in runtime._session_run_policy("ordinary").allowed_tools


@pytest.mark.parametrize("mode", ["skip", "llm", "manual"])
def test_manager_can_sync_after_normal_review_without_remote_calls(runtime, monkeypatch, mode):
    sid = manager(runtime)
    sync_calls = []
    tool = runtime.tool_registry.get_tool("mail.sync")

    def sync(**kwargs):
        [review] = runtime.agent_run_manager.list_safety_reviews(run_id)
        assert review.status.value == "approved"
        sync_calls.append(kwargs)
        return {"account_id": "isolated", "folder": kwargs["folder"], "imported_messages": 0,
                "imported_attachments": 0, "status": "completed", "sync_mode": "delta",
                "next_link": None, "delta_link": None}

    monkeypatch.setattr(tool, "_sync_mail", sync)
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="mail"),
        operation("tool_call", tool_name="mail.sync", tool_input={"folder": "Inbox"}),
        operation("final_answer", reason="The sync receipt is available."), "Refreshed; no new messages.",
    ])

    def approve(review, **kwargs):
        return runtime.agent_run_manager.decide_safety_review(
            review_id=review.review_id, decision=SafetyReviewDecision.APPROVE,
            decided_by="isolated-test", reason="Allow bounded test refresh.",
        )

    monkeypatch.setattr(runtime.agent_turn_loop, "_decide_safety_review_with_llm", approve)
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode(mode)
    run = runtime.create_agent_run(
        session_id=sid, user_input="Refresh the mailbox now.", resumable=mode == "manual",
    )
    run_id = run.run_id
    try:
        result = runtime.run_agent_turn(session_id=sid, user_input=run.user_input, existing_run_id=run_id,
                                        safety_review_mode=SafetyReviewMode(mode))
    except AgentTurnWaitingForConfirmation as waiting:
        assert mode == "manual"
        assert sync_calls == []
        approve(runtime.agent_run_manager.get_safety_review(waiting.review_id))
        result = runtime.resume_agent_run(run_id)
    [review] = runtime.agent_run_manager.list_safety_reviews(run_id)
    assert review.mode.value == mode
    assert review.tool_name == "mail.sync"
    assert review.read_only is False
    assert len(sync_calls) == 1
    assert sync_calls[0] == {"folder": "Inbox", "limit": 25, "max_pages": 1, "trigger": "tool"}
    assert result.answer == "Refreshed; no new messages."
    assert runtime.agent_run_manager.get_run(run_id).status.value == "completed"
    assert [event.tool_name for event in result.tool_events] == ["mail.sync"]


def test_existing_run_keeps_frozen_permission_after_controller_activation(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    old = runtime.create_agent_run(session_id=sid, user_input="Read existing mail.", resumable=True)
    runtime.management_service.update(sid, expected_revision=1, status="active")
    assert "mail.sync" in runtime._session_run_policy(sid).allowed_tools
    saved = runtime.agent_run_manager.get_run(old.run_id)
    assert "mail.sync" not in saved.metadata["session_policy"]["allowed_tools"]
