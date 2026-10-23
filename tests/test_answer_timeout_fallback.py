"""Fault-injected answer timeout tests; no remote models or user data."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.core import agent_turn as turn
from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.llm import LLMClientError, LLMMessage, LLMRequest, LLMTimeoutError
from app.core.llm.errors import LLMResourceError
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.safety import SafetyReviewMode
from app.core.tools import ToolResult, ToolSpec
from tests.test_agent_image_input import image_runtime as _image_runtime
from tests.test_agent_modality_fallback import routing_service
from tests.test_answer_generation_recovery_quality import (
    Provider,
    answer,
    make_loop,
    response,
    scope,
)

image_runtime = _image_runtime


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider_timeout,expected", [(180, 60), (20, 20)])
def test_answer_timeout_metadata_reaches_real_transport(monkeypatch, stream, provider_timeout, expected):
    provider = OpenAICompatibleLLMClient(
        name="offline", provider_name="offline", base_url="https://invalid.example/v1",
        api_key="offline", default_model="test", timeout_seconds=provider_timeout,
    )
    timeouts = []

    def timeout_transport(request, *, timeout):
        timeouts.append(timeout)
        raise TimeoutError("injected read timeout")

    monkeypatch.setattr("urllib.request.urlopen", timeout_transport)
    request = LLMRequest(messages=[LLMMessage(role="user", content="Reply.")],
                         prompt_summary="offline timeout", metadata={"network_timeout_seconds": 60})

    async def dispatch():
        if stream:
            return [event async for event in provider.stream(request)]
        return await provider.complete(request)

    with pytest.raises(LLMTimeoutError):
        asyncio.run(dispatch())
    assert timeouts == [expected]


def default_provider(loop, responses):
    provider = loop.llm_client.registry.get("default")
    provider.responses = list(responses)
    return provider


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("context_only", [False, True])
def test_timeout_switches_default_once_with_audit_and_same_evidence(tmp_path, stream, context_only):
    loop, selected, manager = make_loop(tmp_path, [LLMTimeoutError("selected timeout")])
    fallback = default_provider(loop, [response("Recovered answer.")])
    events = []
    with scope(manager, stream=stream) as run:
        result = (loop._answer_from_context_with_llm(
            user_input="Return the supported results.", route={}, context_window={}, llm_events=events,
        ) if context_only else answer(loop, events))
        assert result == "Recovered answer."
        saved = manager.get_run(run.run_id)
        assert saved.metadata["answer_timeout_fallback"]["client_name"] == "default"
        assert saved.metadata["answer_timeout_fallback"]["from_client_name"] == "selected"
        assert len([e for e in manager.list_events(run.run_id)
                    if e.type == "answer_model_fallback_started"]) == 1
        assert loop._stage_inference_selection("route")[0] == "selected"
    assert len(selected.requests) == len(fallback.requests) == 1
    assert selected.requests[0].messages == fallback.requests[0].messages
    assert [e.client_name for e in events] == ["selected", "default"]
    assert events[0].error_category == "timeout"
    assert all(p.requests[0].metadata["network_timeout_seconds"] == 60 for p in (selected, fallback))


def test_timeout_prefers_primary_over_global_default_and_keeps_frozen_policy(tmp_path):
    loop, selected, manager = make_loop(tmp_path, [response("Primary recovered.")])
    writer = Provider([LLMTimeoutError("writer timeout")], name="writer")
    loop.llm_client.registry.register_client(writer)
    config = loop.llm_client.config.clients[1].model_copy(update={"name": "writer"})
    loop.llm_client.config.clients.append(config)
    fallback = default_provider(loop, [])
    with scope(manager) as run:
        manager._update_run(run.run_id, status=run.status, metadata_patch={
            "llm_answer_client_name": "writer", "llm_answer_model": "test-model",
        })
        assert answer(loop, []) == "Primary recovered."
        saved = manager.get_run(run.run_id)
        assert saved.metadata["llm_answer_client_name"] == "writer"
        assert saved.metadata["answer_timeout_fallback"]["client_name"] == "selected"
    assert len(writer.requests) == len(selected.requests) == 1
    assert not fallback.requests


@pytest.mark.parametrize("stream", [False, True])
def test_fallback_timeout_is_not_retried_even_with_large_attempt_limit(tmp_path, stream):
    loop, selected, manager = make_loop(tmp_path, [LLMTimeoutError("selected timeout")])
    loop.llm_max_attempts = 5
    fallback = default_provider(loop, [LLMTimeoutError("fallback timeout"), response("must not run")])
    events = []
    with scope(manager, stream=stream) as run, pytest.raises(LLMTimeoutError, match="fallback timeout"):
        answer(loop, events)
    assert len(selected.requests) == len(fallback.requests) == 1
    assert [e.error_category for e in events] == ["timeout", "timeout"]
    assert [e.client_name for e in events] == ["selected", "default"]
    assert not any(e.type == "final_answer" for e in manager.list_events(run.run_id))


def test_fallback_uses_own_output_reserve_and_native_reasoning(tmp_path):
    loop, selected, manager = make_loop(tmp_path, [LLMTimeoutError("timeout")], selected_thinking="deepseek")
    loop.llm_client.config.clients[0].output_reserve_tokens = 256
    fallback = default_provider(loop, [response()])
    with scope(manager):
        assert answer(loop, []) == "complete answer"
    assert selected.requests[0].max_output_tokens == 1024
    assert fallback.requests[0].max_output_tokens == 256
    assert fallback.requests[0].thinking_enabled is None
    assert fallback.requests[0].metadata["inference_selection_source"] == "timeout_fallback"


def test_no_other_configured_identity_keeps_bounded_same_model_retry(tmp_path):
    loop, selected, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), response()])
    loop.llm_client.config.default_client = "selected"
    with scope(manager) as run:
        assert answer(loop, []) == "complete answer"
        assert "answer_timeout_fallback" not in manager.get_run(run.run_id).metadata
    assert len(selected.requests) == 2


@pytest.mark.parametrize("kind", ["silent", "child"])
def test_internal_and_child_answers_do_not_change_identity(tmp_path, monkeypatch, kind):
    loop, selected, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), response()])
    fallback = default_provider(loop, [])
    if kind == "silent":
        monkeypatch.setattr(loop, "_answer_publication", lambda: {"user_visible": False})
    with scope(manager, child_budget={"max_llm_calls": 3} if kind == "child" else None) as run:
        assert answer(loop, []) == "complete answer"
        assert "answer_timeout_fallback" not in manager.get_run(run.run_id).metadata
    assert len(selected.requests) == 2
    assert not fallback.requests


def test_deadline_exhaustion_does_not_activate_fallback(tmp_path):
    loop, selected, manager = make_loop(tmp_path, [])
    fallback = default_provider(loop, [])
    with scope(manager) as run:
        manager._update_run(run.run_id, status=run.status, metadata_patch={
            "execution_deadline_unix_seconds": time.time() + 30,
        })

        async def expire(request):
            selected.requests.append(request)
            manager._update_run(run.run_id, status=run.status, metadata_patch={
                "execution_deadline_unix_seconds": time.time() - 1,
            })
            raise LLMTimeoutError("deadline reached")

        selected.complete = expire
        with pytest.raises(LLMResourceError) as raised:
            answer(loop, [])
        assert raised.value.error_category == "execution_deadline"
        assert "answer_timeout_fallback" not in manager.get_run(run.run_id).metadata
    assert 0 < selected.requests[0].metadata["network_timeout_seconds"] <= 30
    assert not fallback.requests


def test_timeout_identity_selection_checks_all_modalities_and_capacity():
    service = routing_service(image_default=False, audio_capacity=False)
    with pytest.raises(LLMClientError, match="No configured default model supports image/audio"):
        service.resolve_input_identity(client_name="audio", excluded_identities={("audio", "audio-model")},
                                       preferred_identity=("selected", None), has_images=True, has_audio=True)
    service.config.fallback_client = "fallback"
    assert service.resolve_input_identity(
        client_name="audio", excluded_identities={("audio", "audio-model")},
        preferred_identity=("selected", None), has_images=True, has_audio=True,
    ) == ("fallback", "fallback-model")


def test_cold_restore_keeps_fallback_newer_than_checkpoint(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "isolated.sqlite3")
    first = InMemoryAgentRunManager(durable_store=store)
    loop, selected, _ = make_loop(tmp_path, [LLMTimeoutError("timeout")])
    default_provider(loop, [response()])
    loop.run_manager = first
    with scope(first) as run:
        stale_checkpoint = run.model_copy(deep=True)
        assert answer(loop, []) == "complete answer"
    restored = InMemoryAgentRunManager(durable_store=store)
    restored.restore_run(stale_checkpoint)
    default_provider(loop, [response("After restart.")])
    tokens = [(var, var.set(value)) for var, value in [
        (turn._turn_run_manager, restored), (turn._turn_run_id, run.run_id),
        (turn._turn_llm_client_name, "selected"),
    ]]
    try:
        assert answer(loop, []) == "After restart."
        assert restored.get_run(run.run_id).metadata["answer_timeout_fallback"]["client_name"] == "default"
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
    assert len(selected.requests) == 1


def test_answer_fallback_refits_smaller_window_from_original_evidence(tmp_path):
    loop, _, manager = make_loop(tmp_path, [LLMTimeoutError("timeout")])
    fallback = default_provider(loop, [response()])
    loop.llm_client.config.clients[0].context_window_tokens = 16384
    loop.llm_client.config.clients[0].output_reserve_tokens = 256
    # Older observations can be omitted, but the most recent small receipt must survive.
    observations = [{"tool_name": "test.read", "result": {"output": "old evidence " * 6000}},
                    {"tool_name": "test.write", "result": {"output": "saved exactly once"}}]
    with scope(manager) as run:
        result = loop._complete_text_with_retry(
            stage="context_answer", system_prompt="Return the supported findings.",
            user_prompt=json.dumps({"user_input": "Report the saved result.", "observations": observations}),
            prompt_summary="saved receipt", max_output_tokens=None, llm_events=[],
        )
        assert result.content == "complete answer"
        budget_events = [e for e in manager.list_events(run.run_id) if e.type == "prompt_budget_applied"]
        assert budget_events
        assert budget_events[-1].payload["input_tokens"] <= budget_events[-1].payload["input_limit"] < 16384
    prompt = fallback.requests[0].messages[1].content
    assert "saved exactly once" in prompt
    assert len(prompt) < 24000


def test_real_turn_timeout_fallback_does_not_repeat_write_tool(image_runtime, tmp_path):
    env = image_runtime
    writes = tmp_path / "isolated-write-receipts.txt"

    class WriteOnce:
        spec = ToolSpec(name="filesystem.timeout_test_write", type="function", package="filesystem",
                        description="Record the requested test receipt.", read_only=False,
                        input_schema={"type": "object", "properties": {}, "additionalProperties": False})

        def invoke(self, *, invocation, context):
            with writes.open("a", encoding="utf-8") as handle:
                handle.write("saved exactly once\n")
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="ok", output={"receipt": "saved exactly once"})

    env.runtime.agent_turn_loop.tool_executor.registry.register_tool(WriteOnce())

    def primary_answer(request):
        stage = request.metadata.get("stage")
        if stage == "route":
            return json.dumps({"selected_package": "filesystem", "reason": "Save receipt."})
        if stage in {"decision", "decision_repair"}:
            operation = ({"type": "final_answer", "reason": "Receipt is saved."} if writes.exists() else
                         {"type": "tool_call", "tool_name": "filesystem.timeout_test_write",
                          "tool_input": {}, "reason": "Save the requested receipt."})
            return json.dumps({"operation": operation})
        if stage == "tool_result_check":
            return '{"status":"accepted","message":"saved","remaining_work":""}'
        return "The receipt was saved exactly once."

    env.provider._answer = primary_answer
    writer = Provider([LLMTimeoutError("writer timeout")], name="writer")
    env.runtime.agent_llm_client.registry.register_client(writer)
    config = env.runtime.agent_llm_client.config.clients[0].model_copy(update={
        "name": "writer", "default_model": "test-model",
    })
    env.runtime.agent_llm_client.config.clients.append(config)
    result = env.runtime.run_agent_turn(
        session_id="isolated-timeout-write", user_input="Save one receipt, then report the result.",
        llm_answer_client_name="writer", llm_answer_model="test-model", safety_review_mode=SafetyReviewMode.SKIP,
    )
    run = env.runtime.agent_run_manager.get_run(result.run_id)
    assert run.status.value == "completed"
    assert result.answer == "The receipt was saved exactly once."
    assert writes.read_text(encoding="utf-8") == "saved exactly once\n"
    assert len(writer.requests) == 1
    assert run.metadata["answer_timeout_fallback"]["client_name"] == env.provider.name
    assert len([e for e in env.runtime.agent_run_manager.list_events(run.run_id)
                if e.type == "answer_model_fallback_started"]) == 1
