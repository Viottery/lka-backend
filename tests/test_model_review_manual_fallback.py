"""Model review denials and failures must wait for a human decision."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.api.routes.agent import decide_agent_safety_review
from app.api.schemas import SafetyReviewDecisionRequest
from app.core.agent_runs import AgentRunCancelled
from app.core.agent_turn import _turn_run_id, _turn_run_manager
from app.core.llm import LLMResponse
from app.core.llm_workloads import WorkBudgetExceeded
from app.core.safety import (
    InMemorySafetyReviewStore,
    SafetyReviewDecision,
    SafetyReviewMode,
    SafetyReviewRequest,
)
from tests.test_management_coordinator import bind, timer
from tests.test_management_coordinator import runtime as management_runtime_fixture
from tests.test_source_constraint_recovery import runtime_for
from tests.test_unified_entry_quality import ScriptedClient, operation


def _configure_read_turn(runtime, monkeypatch):
    loop = runtime.agent_turn_loop
    monkeypatch.setattr(loop, "_route", lambda **kwargs: {"selected_package": "bash"})
    decisions = iter([
        {"action": "call_tool", "tool_name": "bash.run", "tool_input": {"command": "pwd"}},
        {"action": "final_answer"},
    ])
    monkeypatch.setattr(loop, "_decide_next_action", lambda **kwargs: next(decisions))
    monkeypatch.setattr(loop, "_answer_with_llm", lambda **kwargs: "Done.")


def _wait_for_manual_review(manager):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = manager.list_pending_safety_reviews()
        if pending:
            return pending[0]
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for manual fallback review.")


@pytest.mark.parametrize("model_result", ["refusal", "exception"])
@pytest.mark.parametrize("human_decision", [SafetyReviewDecision.APPROVE, SafetyReviewDecision.REJECT])
def test_model_review_failure_waits_for_manual_approval_and_executes_once(
    tmp_path, monkeypatch, model_result, human_decision
):
    runtime = runtime_for(tmp_path, monkeypatch, "legacy")
    app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))
    loop = runtime.agent_turn_loop
    loop.safety_review_mode = SafetyReviewMode.LLM
    _configure_read_turn(runtime, monkeypatch)
    if model_result == "refusal":
        monkeypatch.setattr(loop, "_complete_text_with_retry", lambda **kwargs: LLMResponse(
            provider="synthetic", status="ok", prompt_summary="review",
            content=json.dumps({"approve": False, "reason": "Need a human decision."}),
        ))
    else:
        def fail_review(**kwargs):
            if kwargs.get("stage") == "safety_review":
                raise RuntimeError("synthetic reviewer failure")
            raise AssertionError("Unexpected model call")
        monkeypatch.setattr(loop, "_complete_text_with_retry", fail_review)

    result_holder = {}
    worker = threading.Thread(target=lambda: result_holder.setdefault(
        "result", runtime.run_agent_turn(session_id="fallback", user_input="Check this workspace")
    ), daemon=True)
    worker.start()
    try:
        fallback = _wait_for_manual_review(runtime.agent_run_manager)
        assert fallback.mode == SafetyReviewMode.MANUAL
        assert fallback.status.value == "pending"
        assert runtime.agent_run_manager.get_run(fallback.run_id).status.value == "waiting_confirmation"
        assert worker.is_alive()  # execution remains paused pending human input

        decided = asyncio.run(decide_agent_safety_review(
            fallback.review_id,
            SafetyReviewDecisionRequest(
                    decision=human_decision,
                reason="Approved by the user.",
            ),
            SimpleNamespace(app=app),
        ))
        expected_status = "approved" if human_decision == SafetyReviewDecision.APPROVE else "rejected"
        assert decided.status == expected_status
        worker.join(timeout=5)
        assert not worker.is_alive()
        result = result_holder["result"]
        reviews = runtime.agent_run_manager.list_safety_reviews(result.run_id)
        assert len(reviews) == 2
        model_review = next(review for review in reviews if review.mode == SafetyReviewMode.LLM)
        assert model_review.status.value == "rejected"
        assert model_review.decided_by in {"llm", "system.llm_review_error"}
        assert model_review.review_id in fallback.reason
        if human_decision == SafetyReviewDecision.APPROVE:
            assert result.tool_events[0].result["status"] == "completed"
            assert result.tool_events[0].result.get("execution_started") is True
        else:
            assert result.tool_events[0].result["status"] == "rejected"
            assert result.tool_events[0].result.get("execution_started") is False
        assert len(result.tool_events) == 1
    finally:
        worker.join(timeout=1)
        runtime.stop()


def test_model_approval_executes_without_manual_fallback(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch, "legacy")
    loop = runtime.agent_turn_loop
    loop.safety_review_mode = SafetyReviewMode.LLM
    _configure_read_turn(runtime, monkeypatch)
    monkeypatch.setattr(loop, "_complete_text_with_retry", lambda **kwargs: LLMResponse(
        provider="synthetic", status="ok", prompt_summary="review",
        content=json.dumps({"approve": True, "reason": "The requested read is scoped."}),
    ))
    try:
        result = runtime.run_agent_turn(session_id="model-approved", user_input="Check this workspace")
        assert result.tool_events[0].result["status"] == "completed"
        assert len(runtime.agent_run_manager.list_safety_reviews(result.run_id)) == 1
    finally:
        runtime.stop()


def test_mandatory_manual_rule_cannot_be_approved_by_model_mode(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch, "legacy")
    loop = runtime.agent_turn_loop
    loop.safety_review_mode = SafetyReviewMode.LLM
    try:
        assert loop._effective_safety_review_mode(
            "bash.run", {"command": "rm -rf /tmp/lka-review-test"}
        ) == SafetyReviewMode.MANUAL
    finally:
        runtime.stop()


@pytest.mark.parametrize("error", [AgentRunCancelled("cancelled"), WorkBudgetExceeded("budget exhausted")])
def test_control_exception_never_creates_manual_fallback(tmp_path, monkeypatch, error):
    runtime = runtime_for(tmp_path, monkeypatch, "legacy")
    loop = runtime.agent_turn_loop
    run = runtime.create_agent_run(session_id="control-review", user_input="Inspect current directory")
    review = runtime.agent_run_manager.create_safety_review(SafetyReviewRequest(
        review_id=f"control-{type(error).__name__}", run_id=run.run_id,
        session_id=run.session_id, trace_id=run.trace_id, invocation_id="control-invocation",
        tool_name="bash.run", tool_input={"command": "pwd"}, user_request=run.user_input,
        read_only=True, mode=SafetyReviewMode.LLM, reason="Configured per-call review",
        created_at=run.created_at,
    ))
    monkeypatch.setattr(loop, "_complete_text_with_retry", lambda **kwargs: (_ for _ in ()).throw(error))
    manager_token = _turn_run_manager.set(runtime.agent_run_manager)
    run_token = _turn_run_id.set(run.run_id)
    try:
        with pytest.raises(type(error)):
            loop._decide_safety_review_with_llm(review, llm_events=[])
        reviews = runtime.agent_run_manager.list_safety_reviews(run.run_id)
        assert len(reviews) == 1
        assert reviews[0].review_id == review.review_id
        assert reviews[0].mode == SafetyReviewMode.LLM
        if isinstance(error, AgentRunCancelled):
            assert reviews[0].status.value == "pending"
        else:
            assert reviews[0].status.value == "rejected"
    finally:
        _turn_run_id.reset(run_token)
        _turn_run_manager.reset(manager_token)
        runtime.stop()


def test_cancel_check_does_not_hold_review_lock_against_approval():
    store = InMemorySafetyReviewStore()
    store.create(SafetyReviewRequest(
        review_id="lock-order-review", run_id="run", session_id="session",
        trace_id="trace", invocation_id="invocation", tool_name="bash.run",
        tool_input={"command": "pwd"}, mode=SafetyReviewMode.MANUAL,
        reason="Manual review", created_at="2026-10-25T00:00:00+00:00",
    ))
    cancel_check_entered = threading.Event()
    approval_completed = threading.Event()
    failures = []

    def cancellation_probe():
        cancel_check_entered.set()
        if not approval_completed.wait(timeout=0.5):
            raise AssertionError("Approval could not acquire the review store lock.")
        return False

    def wait_for_approval():
        try:
            store.wait_for_decision(
                "lock-order-review", timeout_seconds=0.01, cancel_check=cancellation_probe,
            )
        except Exception as exc:  # noqa: BLE001 - thread failures must reach the main test.
            failures.append(exc)

    worker = threading.Thread(target=wait_for_approval, daemon=True)
    worker.start()
    try:
        assert cancel_check_entered.wait(timeout=1)
        decided = store.decide(
            review_id="lock-order-review", decision=SafetyReviewDecision.APPROVE,
            decided_by="user", reason="Approved", decided_at="2026-10-25T00:00:01+00:00",
        )
        approval_completed.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert failures == []
        assert decided.status.value == "approved"
        assert store.get("lock-order-review").status.value == "approved"
    finally:
        approval_completed.set()
        worker.join(timeout=1)


@pytest.mark.parametrize("human_decision", [SafetyReviewDecision.APPROVE, SafetyReviewDecision.REJECT])
def test_langgraph_model_refusal_waits_for_human_and_resumes_once(
    tmp_path, monkeypatch, human_decision,
):
    runtime = next(management_runtime_fixture.__wrapped__(tmp_path, monkeypatch))
    session_id, _ = timer(runtime)
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.LLM
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.read_events", tool_input={}),
        operation("final_answer", reason="The event inspection is complete"),
        "Events checked after model fallback approval.",
    ])
    original_complete = runtime.agent_turn_loop._complete_text_with_retry

    def refuse(**kwargs):
        if kwargs.get("stage") == "safety_review":
            return LLMResponse(
                provider="synthetic", status="ok", prompt_summary="review",
                content=json.dumps({"approve": False, "reason": "Ask a human to review this read."}),
            )
        return original_complete(**kwargs)

    monkeypatch.setattr(runtime.agent_turn_loop, "_complete_text_with_retry", refuse)
    try:
        assert runtime.management_coordinator.tick()["scheduled"] == 1
        run = bind(runtime)
        assert runtime.agent_dispatch.run_one()
        manager = runtime.agent_run_manager
        assert manager.get_run(run.run_id).status.value == "waiting_confirmation"
        reviews = manager.list_safety_reviews(run.run_id)
        assert len(reviews) == 2
        model_review = next(review for review in reviews if review.mode == SafetyReviewMode.LLM)
        fallback = next(review for review in reviews if review.mode == SafetyReviewMode.MANUAL)
        assert model_review.status.value == "rejected"
        assert model_review.review_id in fallback.reason
        assert fallback.status.value == "pending"
        runtime.management_coordinator.reconcile()
        manager.decide_safety_review_with_transition(
            review_id=fallback.review_id, decision=human_decision,
            decided_by="user", reason="Human decided this exact event read.",
        )
        result = runtime.resume_agent_run(run.run_id)
        assert manager.get_run(run.run_id).status.value == "completed"
        completed = [event for event in manager.list_events(run.run_id)
                     if event.type == "tool_completed"
                     and event.payload.get("tool_name") == "management.read_events"]
        if human_decision == SafetyReviewDecision.APPROVE:
            assert result.answer == "Events checked after model fallback approval."
            assert len(completed) == 1
            assert runtime.management_goals.list_events(session_id)["events"][0][
                "consumed_execution_id"
            ] is not None
        else:
            assert completed == []
    finally:
        runtime.stop()
