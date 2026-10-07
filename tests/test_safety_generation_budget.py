"""Safety review must not inherit a small generation cap from token accounting."""

import json

import pytest

from app.core.safety import SafetyReviewMode, SafetyReviewRequest
from tests.test_answer_generation_recovery_quality import make_loop, response, scope


def review(loop, manager, run, events):
    record = manager.create_safety_review(SafetyReviewRequest(
        review_id="review-budget", run_id=run.run_id, session_id=run.session_id,
        trace_id=run.trace_id, invocation_id="invocation-budget", tool_name="bash.run",
        tool_input={"command": "pwd"}, user_request="Inspect the current directory",
        read_only=True, mode=SafetyReviewMode.LLM, reason="Configured per-call review",
        created_at=run.created_at))
    return loop._decide_safety_review_with_llm(record, llm_events=events)


@pytest.mark.parametrize("stream", [False, True])
def test_safety_output_unset_at_provider_but_normal_stage_reserve_still_enforced(tmp_path, stream):
    approval = json.dumps({"approve": True, "reason": "The requested directory inspection is read-only."})
    loop, provider, manager = make_loop(tmp_path, [response(approval), response("normal stage")])
    events = []
    with scope(manager, stream=stream) as run:
        result = review(loop, manager, run, events)
        assert result.status == "approved"
        assert result.decided_by == "llm"
        assert provider.requests[0].max_output_tokens is None
        loop._complete_text_with_retry(stage="route", system_prompt="Route the task", user_prompt="inspect",
            prompt_summary="normal control", max_output_tokens=None, llm_events=events)
    assert provider.requests[1].max_output_tokens == 1024
    assert events[0].output_token_count == 900  # More than the former 512 cap.


@pytest.mark.parametrize("content", ["", '{"approve":true,"reason":"The request',
    '{"approve":true,"reason":"Read only"}'])
def test_provider_truncation_never_approves_and_is_recorded_as_incomplete(tmp_path, content):
    loop, provider, manager = make_loop(tmp_path, [response(content, reason="length")])
    with scope(manager) as run:
        result = review(loop, manager, run, [])
    assert result.status == "rejected"
    assert result.decided_by == "system.llm_incomplete"
    assert "not an explicit model rejection" in result.decision_reason
    assert result.llm_output == content
    assert provider.requests[0].max_output_tokens is None


def test_explicit_model_refusal_remains_a_refusal(tmp_path):
    content = json.dumps({"approve": False, "reason": "The target path is not authorized."})
    loop, _, manager = make_loop(tmp_path, [response(content)])
    with scope(manager) as run:
        result = review(loop, manager, run, [])
    assert result.status == "rejected" and result.decided_by == "llm"
    assert result.decision_reason == "The target path is not authorized."


def test_child_safety_review_still_obeys_explicit_task_budget(tmp_path):
    content = json.dumps({"approve": True, "reason": "Read-only scoped inspection."})
    loop, provider, manager = make_loop(tmp_path, [response(content)])
    with scope(manager, child_budget={"max_tokens": 10000}) as run:
        result = review(loop, manager, run, [])
    assert result.status == "approved"
    assert 0 < provider.requests[0].max_output_tokens <= 10000
