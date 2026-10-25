"""Synthetic policy/lifecycle contracts; these do not measure model judgment quality."""

import json

import pytest

from app.core.agent_runs import AgentRunCancelled
from app.core.llm.errors import LLMResourceError
from app.core.llm_workloads import BackgroundBudgetDeferred, WorkBudgetExceeded
from app.core.safety import (
    SafetyReviewMode,
    SafetyReviewRequest,
    stable_safety_review_id,
)
from app.core.tools import ToolContext
from tests.test_answer_generation_recovery_quality import make_loop, response, scope
from tests.test_source_constraint_recovery import runtime_for


def request(run, **updates):
    values = {
        "review_id": stable_safety_review_id(run.run_id, "call", "bash.run"),
        "run_id": run.run_id, "session_id": run.session_id, "trace_id": run.trace_id,
        "invocation_id": "call", "tool_name": "bash.run", "tool_input": {"command": "pwd"},
        "user_request": "Organize the workspace", "read_only": True,
        "mode": SafetyReviewMode.LLM, "reason": "Per-call review", "created_at": run.created_at,
        "safety_review_policy": "autonomous",
    }
    return SafetyReviewRequest(**(values | updates))


def test_prompt_permits_supporting_steps_and_preserves_authority(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response('{"approve":true,"reason":"Scoped"}')])
    with scope(manager) as run:
        review = manager.create_safety_review(request(run, read_only=False, tool_risk="high"))
        decided = loop._decide_safety_review_with_llm(review, llm_events=[])
    assert decided.status == "approved"
    prompt = provider.requests[0].messages[0].content
    for text in ["bounded reversible edits", "each exact operation", "metadata alone is insufficient",
                 "irreversible data loss", "publication or financial", "secret", "authentication changes",
                 "untrusted", "cannot approve themselves", "not a sandbox"]:
        assert text in prompt
    assert json.loads(provider.requests[0].messages[1].content)["review"]["safety_review_policy"] == "autonomous"


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
@pytest.mark.parametrize("failure", ["denial", "exception", "invalid", "truncated", "no_client", "manual"])
def test_rejection_replans_to_safe_alternative_without_manual_wait(tmp_path, monkeypatch, orchestrator, failure):
    runtime = runtime_for(tmp_path, monkeypatch, orchestrator)
    loop = runtime.agent_turn_loop
    policy = loop._session_run_policy().model_copy(update={"safety_review_policy": "autonomous"})
    monkeypatch.setattr(loop, "_session_run_policy", lambda *args: policy)
    loop.safety_review_mode = SafetyReviewMode.LLM
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "denied.txt"
    tool = runtime.tool_registry.get_tool("bash.run")
    if failure == "manual":
        monkeypatch.setattr(tool, "manual_review_reason", lambda args: "Requires human authority", raising=False)
    reviewed = []
    original_decider = loop._decide_safety_review_with_llm

    def reviewer(**kwargs):
        reviewed.append(kwargs)
        if failure == "exception":
            raise RuntimeError("Review unavailable")
        if failure == "invalid":
            return response("invalid JSON")
        if failure == "truncated":
            return response('{"approve":true,"reason":"Incomplete"}', reason="length")
        return response('{"approve":false,"reason":"Operation exceeds authorized scope"}')

    monkeypatch.setattr(loop, "_complete_text_with_retry", reviewer)
    if failure == "no_client":
        # Keep decision-loop model availability while simulating unavailable reviewer.
        original = loop._decide_safety_review_with_llm
        def unavailable(review, **kwargs):
            client = loop.llm_client
            loop.llm_client = None
            try:
                return original(review, **kwargs)
            finally:
                loop.llm_client = client
        monkeypatch.setattr(loop, "_decide_safety_review_with_llm", unavailable)
    monkeypatch.setattr(loop, "_route", lambda **kwargs: {"selected_package": "bash"})
    calls = []
    def decide(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"action": "call_tool", "tool_name": "bash.run",
                    "tool_input": {"command": f"touch {target}", "cwd": str(workspace)}}
        if len(calls) == 2:
            # An alternative independent local read is permitted under the same policy.
            monkeypatch.setattr(loop, "_decide_safety_review_with_llm", original_decider)
            monkeypatch.setattr(loop, "_complete_text_with_retry", lambda **kwargs:
                response('{"approve":true,"reason":"Scoped alternative inspection"}'))
            monkeypatch.setattr(tool, "manual_review_reason", lambda args: None, raising=False)
            return {"action": "call_tool", "tool_name": "bash.run",
                    "tool_input": {"command": "pwd", "cwd": str(workspace)}}
        return {"action": "final_answer"}
    monkeypatch.setattr(loop, "_decide_next_action", decide)
    monkeypatch.setattr(loop, "_answer_with_llm", lambda **kwargs: "Inspected safely.")
    try:
        result = runtime.run_agent_turn(session_id="autonomous", user_input="Inspect workspace safely")
        assert result.answer == "Inspected safely."
        assert not target.exists()
        first, second = result.tool_events
        assert first.result["status"] == "rejected"
        assert first.result["execution_started"] is False
        assert "safe alternative" in first.feedback["remaining_work"]
        assert "Do not bypass" in first.feedback["remaining_work"]
        assert second.result["status"] == "completed"
        reviews = runtime.agent_run_manager.list_safety_reviews(result.run_id)
        assert len(reviews) == 2
        assert all(r.safety_review_policy == "autonomous" for r in reviews)
        assert not runtime.agent_run_manager.list_pending_safety_reviews()
        assert runtime.agent_run_manager.get_run(result.run_id).status.value == "completed"
        assert bool(reviewed) == (failure not in {"manual", "no_client"})
    finally:
        runtime.stop()


@pytest.mark.parametrize("error", [LLMResourceError("No resources", error_category="resource_unavailable"), BackgroundBudgetDeferred("Deferred"),
                                   WorkBudgetExceeded("Quota"), AgentRunCancelled("Fenced")])
def test_resources_quota_and_cancellation_remain_hard_stops(tmp_path, monkeypatch, error):
    loop, _, manager = make_loop(tmp_path, [])
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(loop, "_complete_text_with_retry", fail)
    with scope(manager) as run:
        record = manager.create_safety_review(request(run))
        with pytest.raises(type(error)):
            loop._decide_safety_review_with_llm(record, llm_events=[])
        assert manager.list_pending_safety_reviews() == []
        assert len(manager.list_safety_reviews(run.run_id)) == 1


@pytest.mark.parametrize("graph", [False, True])
@pytest.mark.parametrize("frozen_policy", ["standard", "autonomous"])
@pytest.mark.parametrize("frozen_mode", [SafetyReviewMode.LLM, SafetyReviewMode.MANUAL])
def test_recovery_uses_frozen_review_policy_not_current_policy(tmp_path, monkeypatch, graph, frozen_policy, frozen_mode):
    runtime = runtime_for(tmp_path, monkeypatch, "legacy")
    loop = runtime.agent_turn_loop
    current = "standard" if frozen_policy == "autonomous" else "autonomous"
    policy = loop._session_run_policy().model_copy(update={"safety_review_policy": current})
    monkeypatch.setattr(loop, "_session_run_policy", lambda *args: policy)
    loop.safety_review_mode = SafetyReviewMode.SKIP
    manager = runtime.agent_run_manager
    try:
        with scope(manager) as run:
            old = request(run, safety_review_policy=frozen_policy, mode=frozen_mode)
            # Old persisted records lack a policy and must retain standard authority.
            if frozen_policy == "standard":
                old = SafetyReviewRequest.model_validate(old.model_dump(exclude={"safety_review_policy"}))
            manager.create_safety_review(old)
            monkeypatch.setattr(loop, "_complete_text_with_retry", lambda **kwargs:
                response('{"approve":false,"reason":"Denied"}'))
            if not graph and frozen_policy == "standard":
                # Avoid blocking while verifying standard recovery still reaches manual fallback.
                def pending(*args, **kwargs):
                    raise RuntimeError("historical manual wait")
                monkeypatch.setattr(manager.safety_reviews, "wait_for_decision", pending)
            method = loop.prepare_graph_safety_review if graph else loop._review_tool_call_before_execute
            kwargs = {"invocation_id": "call", "tool_name": "bash.run", "tool_input": {"command": "pwd"},
                          "context": ToolContext(session_id=run.session_id), "progress_events": [], "llm_events": []}
            if not graph and frozen_policy == "standard":
                with pytest.raises(RuntimeError, match="historical manual wait"):
                    method(**kwargs)
            else:
                rejected, pending_review = method(**kwargs)
                if frozen_policy == "autonomous":
                    assert rejected.status == "rejected" and pending_review is None
                else:
                    assert rejected is None and pending_review.mode == SafetyReviewMode.MANUAL
            reviews = manager.list_safety_reviews(run.run_id)
            assert all(review.safety_review_policy == frozen_policy for review in reviews)
            assert len(reviews) == (2 if frozen_policy == "standard" and frozen_mode == SafetyReviewMode.LLM else 1)
    finally:
        runtime.stop()
