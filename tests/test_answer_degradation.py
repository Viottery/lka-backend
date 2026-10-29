from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.core import agent_turn as turn
from app.core.agent_runs import AgentRunCancelled, InMemoryAgentRunManager
from app.core.llm import LLMResponse, LLMResponseMode, LLMService
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig


def response(content):
    return LLMResponse(provider="synthetic", status="completed", content=content,
                       prompt_summary="synthetic", finish_reason="stop",
                       usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def verdict(decision="pass", *, issues=None):
    accepted = decision == "pass"
    return response(json.dumps({
        "faithful": accepted, "hallucination_free": accepted, "request_aligned": accepted,
        "decision": decision, "issues": issues or ([] if accepted else ["Draft needs revision."]),
        "revision_instructions": "" if accepted else "Limit the answer to supported results.",
        "evidence_gap": "" if accepted else "Some requested material is unavailable.",
    }))


class Provider:
    default_model = "synthetic-model"
    thinking_control = "deepseek"

    def __init__(self, name, replies, *, after_call=None):
        self.name = name
        self.replies = list(replies)
        self.requests = []
        self.after_call = after_call

    async def complete(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if self.after_call:
            self.after_call(request)
        return reply.model_copy(update={"client_name": self.name, "model": request.model})

    async def stream(self, request):
        raise AssertionError("answer degradation stays private")


def make_loop(tmp_path, writer_replies, reviewer_replies, *, max_reworks=0, reviewer_after_call=None):
    writer = Provider("writer", writer_replies)
    reviewer = Provider("reviewer", reviewer_replies, after_call=reviewer_after_call)
    registry = LLMClientRegistry()
    registry.register_client(writer)
    registry.register_client(reviewer)
    service = LLMService(config=LLMProviderConfig(default_client="reviewer", clients=[
        LLMClientConfig(name="writer", default_model="synthetic-model", context_window_tokens=100_000,
                        output_reserve_tokens=2048),
        LLMClientConfig(name="reviewer", default_model="synthetic-model", context_window_tokens=100_000,
                        output_reserve_tokens=2048, thinking_control="deepseek"),
    ]), registry=registry)
    manager = InMemoryAgentRunManager()
    loop = turn.AgentTurnLoop(session_service=None, tool_executor=None, llm_client=service,
                              log_dir=tmp_path, run_manager=manager, answer_review_enabled=True,
                              answer_review_max_reworks=max_reworks)
    return loop, writer, reviewer, manager


@contextmanager
def scope(manager, *, child_budget=None):
    parent = manager.create_run(session_id="degradation-test", user_input="Summarize the source accurately.")
    run = parent
    if child_budget is not None:
        run = manager.create_child_run(parent_run_id=parent.run_id, plan_id="degradation-test",
                                       step_id="answer", attempt=1, user_input=parent.user_input)
    manager._update_run(run.run_id, status=run.status, metadata_patch={"llm_answer_client_name": "writer"})
    if child_budget is not None:
        manager._update_run(run.run_id, status=run.status,
                            metadata_patch={"context_snapshot": {"budget": child_budget}})
    tokens = [(var, var.set(value)) for var, value in [
        (turn._turn_run_manager, manager), (turn._turn_run_id, run.run_id),
        (turn._turn_llm_client_name, "reviewer"), (turn._turn_llm_answer_client_name, "writer"),
        (turn._turn_llm_response_mode, LLMResponseMode.STREAM),
    ]]
    try:
        yield run
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def run_answer(loop, *, initial=None, events=None):
    return loop._complete_answer_with_review(
        stage="answer", system_prompt="Use the supplied evidence to answer.",
        user_prompt=json.dumps({"user_input": "Summarize the source accurately.",
                                "observations": [{"tool_name": "source.read", "result": {
                                    "status": "completed", "output": {"text": "SUPPORTED_FACT_7"}}}]}),
        prompt_summary="degradation-test", llm_events=events or [], initial_response=initial,
    )


def test_rework_exhaustion_runs_separate_bounded_partial_delivery_and_pass(tmp_path):
    loop, writer, reviewer, manager = make_loop(
        tmp_path, [response("Unsupported claim about SECRET_99."),
                   response("Partial result: the supplied excerpt supports SUPPORTED_FACT_7; other requested details are unavailable.")],
        [verdict("rewrite", issues=["SECRET_99 is unsupported."]), verdict("pass")], max_reworks=0)
    with scope(manager) as run:
        delivered = run_answer(loop)
    assert "SUPPORTED_FACT_7" in delivered.content
    assert "SECRET_99" not in delivered.content
    assert "partial" in writer.requests[1].messages[0].content.lower()
    assert "incomplete" in writer.requests[1].messages[0].content.lower()
    assert "SUPPORTED_FACT_7" in writer.requests[1].messages[1].content
    assert len(writer.requests) == len(reviewer.requests) == 2
    state = manager.get_run(run.run_id)
    assert state.metadata["answer_review_reworks"] == 0
    assert state.metadata["answer_review_degraded"] is True
    assert state.metadata["answer_review_approval_source"] == "llm"
    assert state.metadata["answer_review_approved_hash"]


def test_rejected_degraded_candidate_returns_fact_free_system_notice(tmp_path):
    loop, writer, reviewer, manager = make_loop(
        tmp_path, [response("SECRET_99"), response("Still claims SECRET_99 despite the evidence gap.")],
        [verdict("rewrite"), verdict("rewrite")], max_reworks=0)
    with scope(manager) as run:
        delivered = run_answer(loop)
    assert "SECRET_99" not in delivered.content
    assert "SUPPORTED_FACT_7" not in delivered.content
    state = manager.get_run(run.run_id)
    assert state.metadata["answer_review_reworks"] == 0
    assert state.metadata["answer_review_approval_source"] == "system_status_notice"
    assert state.metadata["answer_review_degraded"] is True
    assert len(writer.requests) == 2 and len(reviewer.requests) == 2


def test_hard_child_budget_uses_notice_without_degradation_dispatch(tmp_path):
    loop, writer, reviewer, manager = make_loop(
        tmp_path, [], [verdict("rewrite")], max_reworks=0)
    with scope(manager, child_budget={"max_llm_calls": 1}) as run:
        delivered = run_answer(loop, initial=response("SECRET_99"))
    assert "SECRET_99" not in delivered.content
    assert writer.requests == [] and len(reviewer.requests) == 1
    state = manager.get_run(run.run_id)
    assert state.metadata["answer_review_approval_source"] == "system_status_notice"
    assert state.metadata["answer_review_reworks"] == 0


def test_cancellation_during_degradation_does_not_return_notice_or_answer(tmp_path):
    manager_ref = {}

    def cancel_after_writer(_request):
        if len(writer.requests) < 2:
            return
        manager, run_id = manager_ref["manager"], manager_ref["run_id"]
        manager.cancel_run(run_id)

    loop, writer, reviewer, manager = make_loop(
        tmp_path, [response("First bad draft."), response("Degraded draft.")],
        [verdict("rewrite")], max_reworks=0)
    manager_ref["manager"] = manager
    with scope(manager) as run:
        manager_ref["run_id"] = run.run_id
        writer.after_call = cancel_after_writer
        with pytest.raises(AgentRunCancelled):
            run_answer(loop)
    assert len(writer.requests) == 2
    assert len(reviewer.requests) == 1
    state = manager.get_run(run.run_id)
    assert not state.metadata.get("answer_review_approved_hash")


def test_degraded_structured_output_keeps_requested_json_shape(tmp_path, monkeypatch):
    contract = 'Return JSON.\n```json-schema\n{"type":"object","required":["summary","incomplete"],"properties":{"summary":{"type":"string"},"incomplete":{"type":"boolean"}}}\n```'
    monkeypatch.setattr(turn, "_current_output_contract", lambda: contract)
    loop, writer, _reviewer, manager = make_loop(
        tmp_path, [response('{"summary":"SECRET_99","incomplete":false}'),
                   response('{"summary":"Only SUPPORTED_FACT_7 is supported.","incomplete":true}')],
        [verdict("rewrite"), verdict("pass")], max_reworks=0)
    with scope(manager):
        delivered = run_answer(loop)
    parsed = json.loads(delivered.content)
    assert parsed == {"summary": "Only SUPPORTED_FACT_7 is supported.", "incomplete": True}
    assert "incomplete" in writer.requests[1].messages[0].content.lower()


def test_invalid_degraded_json_cannot_be_approved_under_output_contract(tmp_path, monkeypatch):
    contract = 'Return JSON.\n```json-schema\n{"type":"object","required":["summary"],"properties":{"summary":{"type":"string"}}}\n```'
    monkeypatch.setattr(turn, "_current_output_contract", lambda: contract)
    loop, writer, _reviewer, manager = make_loop(
        tmp_path, [response('{"summary":"SECRET_99"}'), response("not JSON")],
        [verdict("rewrite"), verdict("pass")], max_reworks=0)
    with scope(manager) as run, pytest.raises(turn.LLMResourceError, match="output schema"):
        run_answer(loop)
    state = manager.get_run(run.run_id)
    assert state.metadata["answer_review_degraded"] is True
    assert state.metadata.get("answer_review_approved_hash") is None
    assert len(writer.requests) == len(_reviewer.requests) == 2
