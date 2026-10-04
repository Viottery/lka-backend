"""Offline regressions for incomplete streams and unknown provider usage."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager

import pytest

from app.core import agent_turn as turn
from app.core.agent_runs import AgentRunCancelled, AgentRunStatus, InMemoryAgentRunManager
from app.core.background_llm import IncompleteGenerationError, require_complete_response
from app.core.llm import LLMClientError, LLMResponse, LLMResponseMode, LLMService
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig
from app.core.prompt_tokens import TokenCount


def _response(content, *, finish_reason="stop", usage=None):
    return LLMResponse(
        provider="offline", client_name="selected", model="offline-model",
        status="completed", content=content, prompt_summary="boundary regression",
        finish_reason=finish_reason, usage=usage or {},
    )


class _FakeHTTP(io.BytesIO):
    headers = None


class _OfflineClient(OpenAICompatibleLLMClient):
    """Use the real SSE parser; replace only its HTTP transport."""

    def __init__(self, responses, *, sse=b""):
        super().__init__(
            name="selected", provider_name="offline", base_url="https://unused.invalid",
            api_key="offline-fixture", default_model="offline-model",
        )
        self.responses = list(responses)
        self.sse = sse
        self.stream_requests = []
        self.completed_requests = []
        self.after_complete = None

    def _open_stream(self, request):
        self.stream_requests.append(request)
        return _FakeHTTP(self.sse)

    async def complete(self, request):
        self.completed_requests.append(request)
        response = self.responses.pop(0)
        if self.after_complete is not None:
            self.after_complete(request)
        return response


def _make_loop(tmp_path, responses, *, sse=b""):
    client = _OfflineClient(responses, sse=sse)
    registry = LLMClientRegistry()
    registry.register_client(client)
    service = LLMService(
        config=LLMProviderConfig(default_client="selected", clients=[
            LLMClientConfig(
                name="selected", default_model="offline-model",
                context_window_tokens=100_000, output_reserve_tokens=1024,
            ),
        ]),
        registry=registry,
    )
    manager = InMemoryAgentRunManager()
    loop = turn.AgentTurnLoop(
        session_service=None, tool_executor=None, llm_client=service,
        log_dir=tmp_path, run_manager=manager,
    )
    return loop, client, manager


@contextmanager
def _scope(manager, *, stream=False, child=False):
    run = manager.create_run(session_id="boundary", user_input="Return supported facts.")
    manager.mark_running(run.run_id)
    if child:
        run = manager.create_child_run(
            parent_run_id=run.run_id, plan_id="boundary-plan", step_id="answer",
            attempt=1, user_input="Return supported facts.",
        )
        manager.mark_child_running(run.run_id)
        manager._update_run(
            run.run_id, status=manager.get_run(run.run_id).status,
            metadata_patch={"context_snapshot": {
                "budget": {"max_tokens": 50_000, "max_llm_calls": 3},
            }},
        )
    tokens = [(variable, variable.set(value)) for variable, value in (
        (turn._turn_run_manager, manager), (turn._turn_run_id, run.run_id),
        (turn._turn_llm_client_name, "selected"),
        (turn._turn_llm_response_mode,
         LLMResponseMode.STREAM if stream else LLMResponseMode.TEXT),
    )]
    try:
        yield run
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def _answer(loop, events):
    return loop._answer_with_llm(
        user_input="Return the supported facts: 已知事实。", route={}, context_window={},
        observations=[], final_decision=None, llm_events=events,
    )


def _sse_chunk(content, *, finish_reason=None):
    payload = {"choices": [{"delta": {"content": content}, "finish_reason": finish_reason}]}
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def test_nonempty_sse_eof_without_completion_marker_recovers_once(tmp_path):
    loop, client, manager = _make_loop(
        tmp_path, [_response("Complete replacement.")], sse=_sse_chunk("Unfinished sentence"),
    )
    events = []
    with _scope(manager, stream=True) as run:
        assert _answer(loop, events) == "Complete replacement."
        persisted = manager.list_events(run.run_id)
    assert len(client.stream_requests) == len(client.completed_requests) == 1
    assert client.completed_requests[0].response_mode == LLMResponseMode.TEXT
    assert len(events) == 2
    assert events[0].status != "completed" or events[0].partial
    assert any(event.type == "answer_generation_recovery_started" for event in persisted)
    # Only the initial stream is provisional; recovery must not splice its text.
    assert [event.payload["content_snapshot"] for event in persisted
            if event.type == "llm_delta"] == ["Unfinished sentence"]


@pytest.mark.parametrize("ending", ["finish_reason", "done"])
def test_explicit_sse_completion_needs_no_recovery(tmp_path, ending):
    sse = _sse_chunk("Complete streamed answer.",
                     finish_reason="stop" if ending == "finish_reason" else None)
    if ending == "done":
        sse += b"data: [DONE]\n\n"
    loop, client, manager = _make_loop(tmp_path, [], sse=sse)
    with _scope(manager, stream=True):
        assert _answer(loop, []) == "Complete streamed answer."
    assert len(client.stream_requests) == 1
    assert not client.completed_requests


@pytest.mark.parametrize("partial,status", [(True, "completed"), (False, "incomplete")])
def test_nonstream_provider_fallback_preserves_incomplete_status(tmp_path, partial, status):
    first = _response("Incomplete fallback.").model_copy(
        update={"partial": partial, "status": status},
    )
    loop, client, manager = _make_loop(tmp_path, [first, _response("Replacement.")])
    client.supports_stream = False
    events = []
    with _scope(manager, stream=True):
        assert _answer(loop, events) == "Replacement."
    assert len(client.completed_requests) == len(events) == 2
    assert not client.stream_requests
    assert events[0].partial == partial
    assert events[0].status == status


def test_background_publication_rejects_incomplete_status_even_with_valid_text():
    incomplete = _response('{"candidates": []}').model_copy(update={"status": "incomplete"})
    with pytest.raises(IncompleteGenerationError):
        require_complete_response(incomplete)


def test_sse_eof_recovery_cannot_dispatch_a_third_attempt(tmp_path):
    loop, client, manager = _make_loop(
        tmp_path, [_response("Still truncated.", finish_reason="length")],
        sse=_sse_chunk("Unfinished sentence"),
    )
    with _scope(manager, stream=True), pytest.raises(LLMClientError, match="single recovery"):
        _answer(loop, [])
    assert len(client.stream_requests) == len(client.completed_requests) == 1


def test_cancellation_at_sse_eof_prevents_recovery(tmp_path, monkeypatch):
    loop, client, manager = _make_loop(
        tmp_path, [_response("Must not be dispatched.")], sse=_sse_chunk("Unfinished sentence"),
    )
    with _scope(manager, stream=True) as run:
        original_open = client._open_stream

        def open_and_cancel_on_close(request):
            http = original_open(request)
            original_close = http.close

            def close():
                original_close()
                manager.cancel_run(run.run_id, "Cancelled at EOF.")

            monkeypatch.setattr(http, "close", close)
            return http

        monkeypatch.setattr(client, "_open_stream", open_and_cancel_on_close)
        with pytest.raises(AgentRunCancelled):
            _answer(loop, [])
        assert manager.get_run(run.run_id).status == AgentRunStatus.CANCELLED
    assert len(client.stream_requests) == 1
    assert not client.completed_requests


def test_missing_usage_preserves_dispatch_reservation_and_blocks_recovery(tmp_path):
    loop, client, manager = _make_loop(tmp_path, [
        _response("暂存片段。" * 100, finish_reason="length"),
        _response("This recovery must not be dispatched."),
    ])
    reservation = {}
    events = []
    with _scope(manager, child=True) as run:
        def tighten_after_dispatch(request):
            if len(client.completed_requests) != 1:
                return
            counted = loop._budget_llm_prompt(
                system_prompt=request.messages[0].content,
                user_prompt=request.messages[1].content,
                max_output_tokens=request.max_output_tokens, tools=None,
            )
            assert counted.conservative
            reservation["input"] = counted.input_tokens
            reservation["output"] = request.max_output_tokens
            # Enough for the original dispatch, but one token short of the
            # second input once the unknown original output stays reserved.
            budget = 2 * counted.input_tokens + request.max_output_tokens - 1
            current = manager.get_run(run.run_id)
            manager._update_run(
                run.run_id, status=current.status,
                metadata_patch={"context_snapshot": {
                    "budget": {"max_tokens": budget, "max_llm_calls": 3},
                }},
            )

        client.after_complete = tighten_after_dispatch
        with pytest.raises(RuntimeError, match="token budget"):
            _answer(loop, events)
        audit = [event for event in manager.list_events(run.run_id)
                 if event.type == "llm_completed"]
    assert len(client.completed_requests) == len(audit) == 1
    assert audit[0].payload["budget_token_count"] >= sum(reservation.values())
    # A conservative budget reservation must not masquerade as actual usage.
    assert events[0].total_token_count is None
    assert audit[0].payload["audit_record"]["total_token_count"] is None


def test_missing_usage_recovery_is_clamped_to_remaining_reservation(tmp_path):
    loop, client, manager = _make_loop(tmp_path, [
        _response("Truncated.", finish_reason="length"), _response("Done."),
    ])
    with _scope(manager, child=True) as run:
        def tighten_after_dispatch(request):
            if len(client.completed_requests) != 1:
                return
            counted = loop._budget_llm_prompt(
                system_prompt=request.messages[0].content,
                user_prompt=request.messages[1].content,
                max_output_tokens=request.max_output_tokens, tools=None,
            )
            budget = 2 * counted.input_tokens + request.max_output_tokens + 37
            current = manager.get_run(run.run_id)
            manager._update_run(
                run.run_id, status=current.status,
                metadata_patch={"context_snapshot": {
                    "budget": {"max_tokens": budget, "max_llm_calls": 3},
                }},
            )

        client.after_complete = tighten_after_dispatch
        assert _answer(loop, []) == "Done."
        info = loop._child_budget_for_prompt()
        assert info["remaining_tokens"] == 0
    assert [request.max_output_tokens for request in client.completed_requests] == [1024, 37]


def test_missing_usage_uses_selected_counter_not_character_guess(tmp_path, monkeypatch):
    class SelectedCounter:
        def count_request(self, system_prompt, user_prompt, tools=None):
            return TokenCount(count=317, method="selected_fixture_counter", conservative=False)

        def count_text(self, text):
            return TokenCount(count=17, method="selected_fixture_counter", conservative=False)

    loop, _, manager = _make_loop(tmp_path, [_response("Done.")])
    monkeypatch.setattr(loop, "_counter_for_tokenizer", lambda _: SelectedCounter())
    with _scope(manager, child=True) as run:
        assert _answer(loop, []) == "Done."
        audit = next(event for event in manager.list_events(run.run_id)
                     if event.type == "llm_completed")
    assert audit.payload["token_count_method"] == "selected_fixture_counter"
    assert audit.payload["budget_token_count"] == 317 + turn.CHILD_PROMPT_OVERHEAD_TOKENS + 1024
    assert audit.payload["budget_token_count_method"] == "dispatch_reservation"
    assert audit.payload["audit_record"]["total_token_count"] is None


def test_reported_actual_usage_still_supersedes_reservation(tmp_path):
    loop, client, manager = _make_loop(tmp_path, [_response(
        "Complete answer.", usage={"prompt_tokens": 500, "completion_tokens": 900,
                                   "total_tokens": 1400},
    )])
    events = []
    with _scope(manager, child=True) as run:
        assert _answer(loop, events) == "Complete answer."
        audit = [event for event in manager.list_events(run.run_id)
                 if event.type == "llm_completed"]
    assert len(client.completed_requests) == len(audit) == 1
    assert audit[0].payload["budget_token_count"] == events[0].total_token_count == 1400
