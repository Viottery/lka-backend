from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.core import agent_turn as turn
from app.core.agent_runs import AgentRunCancelled, InMemoryAgentRunManager
from app.core.llm import LLMResponse, LLMResponseMode, LLMService
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig


def response(content, *, status="completed", partial=False, finish_reason="stop"):
    return LLMResponse(provider="synthetic", status=status, content=content,
                       prompt_summary="synthetic", finish_reason=finish_reason, partial=partial,
                       usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


class ScriptedProvider:
    default_model = "synthetic-model"
    thinking_control = "deepseek"

    def __init__(self, name, responses):
        self.name = name
        self.responses = list(responses)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value.model_copy(update={"client_name": self.name, "model": request.model})

    async def stream(self, request):
        raise AssertionError("private answer review must not stream draft deltas")


def make_loop(tmp_path, writer_responses, reviewer_responses, *, max_reworks=2,
              output_reserve_tokens=2048):
    writer = ScriptedProvider("writer", writer_responses)
    reviewer = ScriptedProvider("reviewer", reviewer_responses)
    registry = LLMClientRegistry()
    registry.register_client(writer)
    registry.register_client(reviewer)
    config = LLMProviderConfig(default_client="reviewer", clients=[
        LLMClientConfig(name="writer", default_model="synthetic-model", context_window_tokens=100_000,
                        output_reserve_tokens=output_reserve_tokens),
        LLMClientConfig(name="reviewer", default_model="synthetic-model", context_window_tokens=100_000,
                        output_reserve_tokens=output_reserve_tokens, thinking_control="deepseek"),
    ])
    manager = InMemoryAgentRunManager()
    loop = turn.AgentTurnLoop(session_service=None, tool_executor=None,
                              llm_client=LLMService(config=config, registry=registry),
                              log_dir=tmp_path, run_manager=manager,
                              answer_review_enabled=True, answer_review_max_reworks=max_reworks)
    return loop, writer, reviewer, manager


@contextmanager
def scope(manager, *, stream=False, child_budget=None):
    parent = manager.create_run(session_id="review-test", user_input="Summarize the source accurately.")
    run = parent
    if child_budget is not None:
        run = manager.create_child_run(parent_run_id=parent.run_id, plan_id="review-test",
                                       step_id="answer", attempt=1, user_input=parent.user_input)
    manager._update_run(run.run_id, status=run.status, metadata_patch={"llm_answer_client_name": "writer"})
    if child_budget is not None:
        manager._update_run(run.run_id, status=run.status,
                            metadata_patch={"context_snapshot": {"budget": child_budget}})
    tokens = [(var, var.set(value)) for var, value in [
        (turn._turn_run_manager, manager), (turn._turn_run_id, run.run_id),
        (turn._turn_llm_client_name, "reviewer"), (turn._turn_llm_answer_client_name, "writer"),
        (turn._turn_llm_response_mode, LLMResponseMode.STREAM if stream else LLMResponseMode.TEXT),
    ]]
    try:
        yield run
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def answer(loop, events, observations=None, user_input="Summarize the source accurately."):
    return loop._answer_with_llm(user_input=user_input, route={}, context_window={},
                                 observations=observations or [], final_decision=None, llm_events=events)


def verdict(**changes):
    value = {"faithful": True, "hallucination_free": True, "request_aligned": True,
             "decision": "pass", "issues": [], "revision_instructions": "", "evidence_gap": ""}
    value.update(changes)
    return response(json.dumps(value))


def test_hallucinated_draft_is_private_then_corrected_and_approved(tmp_path):
    loop, writer, reviewer, manager = make_loop(
        tmp_path, [response("The report says 17% growth."), response("The report states 7% growth.")],
        [verdict(faithful=False, hallucination_free=False, decision="rewrite",
                 issues=["The source says 7%, not 17%."], revision_instructions="Correct the percentage."), verdict()])
    events = []
    with scope(manager, stream=True) as run:
        assert answer(loop, events) == "The report states 7% growth."
        run_events = manager.list_events(run.run_id)
    assert [r.messages[1].content for r in writer.requests] != []
    assert len(writer.requests) == 2 and len(reviewer.requests) == 2
    assert "17%" in reviewer.requests[0].messages[1].content
    assert "17%" in json.loads(writer.requests[1].messages[1].content)["answer_review_feedback"]["issues"][0]
    assert not any(e.type == "llm_delta" and "17%" in e.payload.get("delta", "") for e in run_events)
    assert all(e.type != "answer_review_started" or e.stage == "answer_review" for e in run_events)


def test_partial_on_topic_answer_can_pass(tmp_path):
    loop, _, reviewer, manager = make_loop(tmp_path, [response("The available excerpt says revenue rose 7%; it does not include the cause.")], [verdict()])
    with scope(manager):
        assert answer(loop, []).startswith("The available excerpt")
    assert len(reviewer.requests) == 1


def test_request_drift_is_rewritten_with_original_tool_observation(tmp_path):
    loop, writer, reviewer, manager = make_loop(
        tmp_path, [response("Here is a poem about the ocean."), response("The source says revenue rose 7%.")],
        [verdict(request_aligned=False, decision="rewrite", issues=["This does not summarize the source."],
                 revision_instructions="Summarize the observed revenue result."), verdict()])
    observations = [{"tool_name": "synthetic.read", "result": {"status": "completed", "output": {"text": "Revenue rose 7%."}}}]
    with scope(manager):
        assert answer(loop, [], observations=observations) == "The source says revenue rose 7%."
    assert "Revenue rose 7%." in reviewer.requests[0].messages[1].content
    assert len(writer.requests) == 2


@pytest.mark.parametrize("bad", ["not json", json.dumps({"faithful": True, "hallucination_free": True,
    "request_aligned": True, "decision": "pass", "issues": ["contradiction"],
    "revision_instructions": "", "evidence_gap": ""})])
def test_invalid_or_contradictory_review_fails_closed(tmp_path, bad):
    loop, _, _, manager = make_loop(
        tmp_path, [response("PRIVATE_DRAFT_SENTINEL"), response("Partial supported result; other requested details remain incomplete.")],
        [response(bad), verdict()])
    with scope(manager) as run:
        delivered = answer(loop, [])
    assert "PRIVATE_DRAFT_SENTINEL" not in delivered
    assert "incomplete" in delivered.lower()
    state = manager.get_run(run.run_id)
    assert state.metadata["answer_review_degraded"] is True
    assert state.metadata["answer_review_approval_source"] == "llm"


def test_review_uses_reasoning_client_without_thinking_writer_uses_answer_client(tmp_path):
    loop, writer, reviewer, manager = make_loop(tmp_path, [response("draft")], [verdict()])
    with scope(manager):
        answer(loop, [])
    assert writer.requests[0].client_name == "writer"
    assert reviewer.requests[0].client_name == "reviewer"
    assert reviewer.requests[0].thinking_enabled is False
    assert reviewer.requests[0].reasoning_effort is None


def test_review_falls_back_to_low_when_provider_cannot_disable_reasoning(tmp_path):
    loop, _, reviewer, manager = make_loop(tmp_path, [response("draft")], [verdict()])
    config = loop.llm_client.config.clients[1]
    config.thinking_control = None
    config.supports_reasoning_effort = True
    with scope(manager):
        answer(loop, [])
    assert reviewer.requests[0].reasoning_effort == "low"
    assert reviewer.requests[0].thinking_enabled is None


def test_pass_with_disclosed_gap_delivers_without_regeneration(tmp_path):
    loop, writer, reviewer, manager = make_loop(tmp_path,
        [response("The supplied excerpt confirms this part; the rest remains unverified.")],
        [verdict(evidence_gap="Other source sections are unavailable and this is disclosed.")])
    with scope(manager) as run:
        delivered = answer(loop, [])
    assert delivered.startswith("The supplied excerpt confirms")
    assert len(writer.requests) == len(reviewer.requests) == 1
    assert manager.get_run(run.run_id).metadata["answer_review_approval_source"] == "llm"
    assert not any(e.type == "answer_degradation_started" for e in manager.list_events(run.run_id))


def test_answer_review_and_degraded_generation_do_not_cap_reasoning_at_output_reserve(tmp_path):
    loop, writer, reviewer, manager = make_loop(
        tmp_path,
        [response("Initial draft."), response("Supported partial result; remaining details are unavailable.")],
        [response("truncated review", finish_reason="length"), verdict()],
        output_reserve_tokens=8192,
    )
    with scope(manager) as run:
        delivered = answer(loop, [])

    assert delivered.startswith("Supported partial result")
    assert manager.get_run(run.run_id).metadata["answer_review_degraded"] is True
    assert [request.max_output_tokens for request in writer.requests] == [None, None]
    assert [request.max_output_tokens for request in reviewer.requests] == [None, None]
    events = manager.list_events(run.run_id)
    assert any(event.type == "answer_degradation_started"
               and event.payload.get("reason") == "answer_review_unavailable" for event in events)


def test_truncated_review_and_degraded_review_never_publish_draft(tmp_path):
    loop, writer, reviewer, manager = make_loop(
        tmp_path,
        [response("PRIVATE_DRAFT_SENTINEL"), response("PRIVATE_DEGRADED_SENTINEL")],
        [response("truncated review", finish_reason="length"),
         response("truncated degraded review", finish_reason="length")],
    )
    with scope(manager) as run:
        delivered = answer(loop, [])

    assert delivered.startswith("本地状态说明")
    assert "PRIVATE_DRAFT_SENTINEL" not in delivered
    assert "PRIVATE_DEGRADED_SENTINEL" not in delivered
    events = manager.list_events(run.run_id)
    assert not any(event.type == "answer_review_completed" for event in events)
    assert not any(event.type == "final_answer" for event in events)
    state = manager.get_run(run.run_id)
    assert state.metadata["answer_review_degraded"] is True
    assert state.metadata["answer_review_approval_source"] == "system_status_notice"
    assert [request.max_output_tokens for request in writer.requests] == [None, None]
    assert [request.max_output_tokens for request in reviewer.requests] == [None, None]


def test_rework_limit_is_persistent_and_does_not_publish_rejected_draft(tmp_path):
    rejected = verdict(faithful=False, decision="rewrite", issues=["Fix it."], revision_instructions="Revise.")
    loop, _, _, manager = make_loop(tmp_path,
        [response("PRIVATE_DRAFT_ONE"), response("PRIVATE_DRAFT_TWO"), response("Still unsupported candidate.")],
        [rejected, rejected, rejected], max_reworks=1)
    with scope(manager) as run:
        delivered = answer(loop, [])
    assert "PRIVATE_DRAFT" not in delivered
    latest = manager.get_run(run.run_id)
    assert latest.metadata["answer_review_reworks"] == 1
    assert latest.metadata["answer_review_approval_source"] == "system_status_notice"
    assert delivered.startswith("本地状态说明")
    assert not any(e.type == "final_answer" for e in manager.list_events(run.run_id))


def test_reviewer_is_charged_to_existing_child_call_budget(tmp_path):
    loop, _, _, manager = make_loop(tmp_path, [response("draft")], [verdict()])
    with scope(manager, child_budget={"max_llm_calls": 1}) as run:
        delivered = answer(loop, [])
    assert "draft" not in delivered
    assert manager.get_run(run.run_id).metadata["answer_review_approval_source"] == "system_status_notice"
    assert delivered.startswith("本地状态说明")


def test_two_call_child_budget_accounts_for_draft_and_review_usage(tmp_path):
    # Children keep their frozen inference model for both generation and review.
    loop, writer, reviewer, manager = make_loop(tmp_path, [], [response("Supported result."), verdict()])
    with scope(manager, child_budget={"max_llm_calls": 2}) as run:
        assert answer(loop, []) == "Supported result."
        budget = loop._child_budget_for_prompt()
        assert budget["remaining_llm_calls"] == 0
        assert budget["consumed_tokens"] == 30
        assert budget["usage_breakdown"]["unknown_usage_calls"] == 0
        assert writer.requests == [] and len(reviewer.requests) == 2
        assert [e.stage for e in manager.list_events(run.run_id) if e.type == "llm_started"] == ["answer", "answer_review"]


def test_cancellation_after_review_prevents_approval_and_publication(tmp_path):
    loop, _, reviewer, manager = make_loop(tmp_path, [response("draft")], [verdict()])
    original = reviewer.complete
    with scope(manager) as run:
        async def cancel_after_review(request):
            result = await original(request)
            manager.cancel_run(run.run_id)
            return result
        reviewer.complete = cancel_after_review
        with pytest.raises(AgentRunCancelled):
            answer(loop, [])
        assert manager.get_run(run.run_id).metadata.get("answer_review_approved_hash") is None
        assert not any(e.type == "final_answer" for e in manager.list_events(run.run_id))


def test_approval_cannot_be_reused_for_modified_answer(tmp_path):
    loop, _, _, manager = make_loop(tmp_path, [response("Supported answer.")], [verdict()])
    with scope(manager):
        approved = answer(loop, [])
        assert loop._verify_final_answer(answer=approved, tool_events=[]) == []
        with pytest.raises(RuntimeError, match="matching review approval"):
            loop._verify_final_answer(answer=approved + " Fabricated addition.", tool_events=[])


def test_current_tool_evidence_survives_large_history_in_writer_and_reviewer(tmp_path):
    loop, writer, reviewer, manager = make_loop(tmp_path, [response("Supported result.")], [verdict()])
    loop.prompt_input_target_tokens = 18000
    observations = [{"tool_name": "synthetic.read", "result": {"status": "completed",
        "output": {"text": "SUPPORTED_SOURCE_7: revenue rose 7%."}}}]
    context = {"summary": "", "summary_metadata": {"method": "conversation_intent"},
        "recent_messages": [{"role": "user", "trace_id": "old", "content": "OLD_HISTORY " * 20000}]}
    with scope(manager):
        loop._answer_with_llm(user_input="Summarize the source.", route={}, context_window=context,
                             observations=observations, final_decision=None, llm_events=[])
    for request in (writer.requests[0], reviewer.requests[0]):
        payload = json.loads(request.messages[1].content)
        assert "SUPPORTED_SOURCE_7" in json.dumps(payload["observations"])
        assert "OLD_HISTORY" not in json.dumps(payload["session_context_window"]["recent_messages"])
