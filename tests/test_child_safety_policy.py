"""Offline review-policy inheritance and external approval denial contracts."""
import asyncio

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.codex_app_server import ApprovalRequest, ServerNotification
from app.core.codex_expert import CodexAppServerExpertExecutor
from app.core.multi_agent import TaskResultStatus
from tests.test_codex_expert import FakeCodexClient, _setup


def child(manager, parent, step):
    return manager.create_child_run(parent_run_id=parent.run_id, plan_id="policy-plan",
                                    step_id=step, attempt=1, user_input="Inspect safely")


def test_autonomous_children_freeze_only_review_policy_across_restart_and_nesting(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "runs.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    parent = manager.create_run(session_id="parent", user_input="Inspect", metadata={"session_policy": {
        "safety_review_policy": "autonomous", "safety_review_mode": "llm",
        "allowed_tools": ["filesystem.read_file"], "allowed_paths": ["/private"],
        "llm_model": "parent-model",
    }})
    first = child(manager, parent, "first")
    expected = {"safety_review_policy": "autonomous", "safety_review_mode": "llm"}
    assert first.metadata["session_policy"] == expected
    manager = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    restored = manager.get_run(first.run_id)
    assert restored.metadata["session_policy"] == expected
    nested = child(manager, restored, "nested")
    assert nested.metadata["session_policy"] == expected
    parent.metadata["session_policy"]["safety_review_policy"] = "standard"
    assert restored.metadata["session_policy"] == expected


@pytest.mark.parametrize("policy", [{}, {"safety_review_policy": "standard", "safety_review_mode": "manual"}])
def test_standard_child_creation_retains_historical_defaults(policy):
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Inspect", metadata={"session_policy": policy})
    assert "session_policy" not in child(manager, parent, "first").metadata


@pytest.mark.parametrize("route_human", [False, True])
@pytest.mark.parametrize("method", ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"])
def test_autonomous_codex_declines_manual_authority_and_continues(tmp_path, route_human, method):
    manager, run, snapshot, views = _setup(str(tmp_path))
    run = manager._update_run(run.run_id, status=run.status, metadata_patch={"session_policy": {
        "safety_review_policy": "autonomous", "safety_review_mode": "llm",
    }})
    client = FakeCodexClient()
    target = tmp_path / "never-created"
    client.events.put_nowait(ApprovalRequest(request_id=81, method=method, params={
        "itemId": "denied", "threadId": "thread-test", "turnId": "turn-test",
        "command": ["touch", str(target)], "cwd": str(tmp_path),
    }))
    client.events.put_nowait(ServerNotification(method="turn/completed", params={
        "turn": {"id": "turn-test", "status": "completed"},
    }))
    executor = CodexAppServerExpertExecutor(manager, client_factory=lambda _: client,
                                           route_human_approvals=route_human)
    result = asyncio.run(asyncio.wait_for(executor.execute(child_run_id=run.run_id,
                                                           snapshot=snapshot, views=views), timeout=2))
    assert result.status == TaskResultStatus.COMPLETED
    assert client.decisions == [(81, "decline")]
    assert client.interrupts == []
    assert not target.exists()
    assert not manager.list_pending_safety_reviews()
    reviews = manager.list_safety_reviews(run.run_id)
    assert len(reviews) == 1
    assert reviews[0].safety_review_policy == "autonomous"
    assert reviews[0].mode == "manual" and reviews[0].status == "rejected"


def test_codex_recovery_preserves_existing_standard_manual_review(tmp_path):
    from app.core.codex_expert import _ActiveTurn
    from app.core.safety import SafetyReviewDecision, SafetyReviewRequest, stable_safety_review_id

    manager, run, _, _ = _setup(str(tmp_path))
    review_id = stable_safety_review_id(run.run_id, "thread-test", "turn-test", "denied", "81")
    manager.create_safety_review(SafetyReviewRequest(
        review_id=review_id, run_id=run.run_id, session_id=run.session_id, trace_id=run.trace_id,
        invocation_id="old-call", tool_name="codex.command", tool_input={}, user_request="Inspect",
        mode="manual", reason="Previously requested authority", created_at=run.created_at,
    ))
    run = manager._update_run(run.run_id, status=manager.get_run(run.run_id).status,
                              metadata_patch={"session_policy": {"safety_review_policy": "autonomous"}})
    client = FakeCodexClient()
    executor = CodexAppServerExpertExecutor(manager, client_factory=lambda _: client)
    event = ApprovalRequest(request_id=81, method="item/commandExecution/requestApproval",
                            params={"itemId": "denied"})

    async def exercise():
        waiting = asyncio.create_task(executor._route_approval(
            run, _ActiveTurn(client, thread_id="thread-test", turn_id="turn-test"), event))
        await asyncio.sleep(0)
        assert not waiting.done()
        assert manager.get_safety_review(review_id).safety_review_policy == "standard"
        manager.decide_safety_review(review_id=review_id, decision=SafetyReviewDecision.APPROVE,
                                     decided_by="user", reason="Explicit approval")
        return await asyncio.wait_for(waiting, timeout=1)

    assert asyncio.run(exercise()) == "accept"
    assert not client.interrupts
