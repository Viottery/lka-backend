from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from app.api.main import create_app
from app.api.routes.agent import _preflight_reasoning
from app.api.schemas import AgentTurnRequest
from app.core.config import get_settings
from app.core.llm import (
    LLMClientRegistry,
    LLMRequest,
    LLMResponse,
    LLMResponseMode,
    LLMService,
    LLMStreamEvent,
)
from app.core.local_config import LLMClientConfig, LLMModelConfigOverride, LLMProviderConfig


class RecordingLLM:
    def __init__(self, *, incomplete_answer_once: bool = False) -> None:
        self.calls: list[dict] = []
        self.incomplete_answer_once = incomplete_answer_once

    def complete_text(self, **kwargs):
        metadata = kwargs.get("metadata") or {}
        stage = metadata.get("stage")
        self.calls.append({"stage": stage, "client_name": kwargs.get("client_name"),
                           "model": kwargs.get("model"), "metadata": metadata,
                           "max_output_tokens": kwargs.get("max_output_tokens")})
        if stage == "route":
            content = '{"selected_package":null,"reason":"context is enough"}'
        else:
            content = "answer"
        finish_reason, partial = None, False
        if stage in {"answer", "context_answer"} and self.incomplete_answer_once:
            self.incomplete_answer_once = False
            content, finish_reason, partial = "cut", "length", True
        return LLMResponse(provider="recording", status="completed", content=content,
                           prompt_summary=kwargs.get("prompt_summary", ""),
                           finish_reason=finish_reason, partial=partial)

    async def stream(self, request):
        stage = request.metadata.get("stage")
        self.calls.append({"stage": stage, "client_name": request.client_name,
                           "model": request.model, "metadata": request.metadata})
        text = '{"selected_package":null,"reason":"context is enough"}' if stage == "route" else "stream answer"
        yield LLMStreamEvent(event_type="llm_started", stage=stage, client_name=request.client_name or "default",
                             provider="recording", model=request.model or "default")
        yield LLMStreamEvent(event_type="llm_delta", stage=stage, client_name=request.client_name or "default",
                             provider="recording", model=request.model or "default", delta=text)
        yield LLMStreamEvent(event_type="llm_completed", stage=stage, client_name=request.client_name or "default",
                             provider="recording", model=request.model or "default", content_snapshot=text)


class ServiceRecordingClient:
    supports_stream = True
    supports_json_mode = True
    supports_vision = False
    supports_audio = False
    supports_function_calling = False
    supports_required_tool_choice = False
    supports_reasoning_effort = False

    def __init__(self, name: str, default_model: str):
        self.name = name
        self.provider_name = "test"
        self.default_model = default_model
        self.available_models = [default_model, "writer-v2"]
        self.calls: list[LLMRequest] = []
        self.incomplete_answer_once = False

    async def complete(self, request):
        self.calls.append(request)
        stage = request.metadata.get("stage")
        content = ('{"selected_package":null,"reason":"context is enough"}' if stage == "route"
                   else "service answer")
        if stage in {"answer", "context_answer"} and self.incomplete_answer_once:
            self.incomplete_answer_once = False
            return LLMResponse(provider="test", status="incomplete", partial=True, finish_reason="length",
                               content="partial", prompt_summary=request.prompt_summary,
                               client_name=request.client_name, model=request.model)
        return LLMResponse(provider="test", status="completed", content=content,
                           prompt_summary=request.prompt_summary, client_name=request.client_name,
                           model=request.model)

    async def stream(self, request):
        response = await self.complete(request)
        yield LLMStreamEvent(
            event_type="llm_delta", stage=request.metadata["stage"], client_name=self.name,
            provider="test", model=request.model, delta=response.content,
        )
        yield LLMStreamEvent(
            event_type="llm_completed", stage=request.metadata["stage"], client_name=self.name,
            provider="test", model=request.model, content_snapshot=response.content,
            metadata={"status": "completed", "partial": False, "finish_reason": "stop"},
        )


def _app(tmp_path, monkeypatch, *, graph: bool = False):
    config = tmp_path / "local.toml"
    config.write_text('[agent]\norchestrator = "langgraph"\n' if graph else "", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    return create_app()


@pytest.mark.parametrize("graph", [False, True])
def test_answer_pair_routes_only_final_generation_and_freezes_for_graph(tmp_path, monkeypatch, graph):
    app = _app(tmp_path, monkeypatch, graph=graph)
    fake = RecordingLLM()
    runtime = app.state.runtime
    runtime.agent_turn_loop.llm_client = fake
    if graph:
        queued = runtime.create_agent_run(session_id="dual", user_input="answer",
                                          llm_answer_client_name="writer", llm_answer_model="writer-v2",
                                          resumable=True)
        result = runtime.run_agent_turn(session_id=queued.session_id, user_input=queued.user_input,
                                        existing_run_id=queued.run_id)
    else:
        result = runtime.run_agent_turn(session_id="dual", user_input="answer",
                                        llm_client_name="primary", llm_model="reasoner-v1",
                                        llm_answer_client_name="writer", llm_answer_model="writer-v2")
    stages = {call["stage"]: call for call in fake.calls}
    assert stages["route"]["client_name"] == (None if graph else "primary")
    assert stages["route"]["model"] == (None if graph else "reasoner-v1")
    assert stages["context_answer"]["client_name"] == "writer"
    assert stages["context_answer"]["model"] == "writer-v2"
    run = runtime.agent_run_manager.get_run(result.run_id)
    assert run.metadata["llm_answer_client_name"] == "writer"
    assert run.metadata["session_policy"]["llm_answer_model"] == "writer-v2"


def test_answer_selection_falls_back_to_primary_and_streams_with_answer_pair(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    fake = RecordingLLM()
    runtime.agent_turn_loop.llm_client = fake
    runtime.run_agent_turn(
        session_id="fallback", user_input="answer", llm_client_name="main", llm_model="main-v1",
    )
    assert {(call["client_name"], call["model"]) for call in fake.calls} == {("main", "main-v1")}

    fake.calls.clear()
    runtime.run_agent_turn(session_id="stream", user_input="answer",
                           llm_answer_client_name="writer", llm_answer_model="writer-v1",
                           llm_response_mode=LLMResponseMode.STREAM)
    answer = next(call for call in fake.calls if call["stage"] == "context_answer")
    assert (answer["client_name"], answer["model"]) == ("writer", "writer-v1")


def test_answer_recovery_stays_on_frozen_answer_model(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    fake = RecordingLLM(incomplete_answer_once=True)
    runtime.agent_turn_loop.llm_client = fake
    runtime.agent_turn_loop.llm_max_attempts = 1
    result = runtime.run_agent_turn(session_id="recovery", user_input="answer",
                                    llm_client_name="main", llm_model="main-v1",
                                    llm_answer_client_name="writer", llm_answer_model="writer-v1")
    answer_calls = [call for call in fake.calls if call["stage"] == "context_answer"]
    assert len(answer_calls) == 2
    assert {(call["client_name"], call["model"]) for call in answer_calls} == {
        ("writer", "writer-v1")
    }
    assert result.answer == "answer"


def test_llm_service_dispatch_uses_answer_model_and_model_only_primary_client(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    configs = [
        LLMClientConfig(name="primary", provider="mock", default_model="reasoner-v1",
                        context_window_tokens=65536, output_reserve_tokens=1024,
                        model_overrides={"writer-v2": LLMModelConfigOverride(
                            context_window_tokens=49152, output_reserve_tokens=2048)}),
        LLMClientConfig(name="writer", provider="mock", default_model="writer-v1",
                        context_window_tokens=65536, output_reserve_tokens=1024),
    ]
    provider_config = LLMProviderConfig(default_client=None, clients=configs)
    registry = LLMClientRegistry()
    primary = ServiceRecordingClient("primary", "reasoner-v1")
    writer = ServiceRecordingClient("writer", "writer-v1")
    registry.register_client(primary)
    registry.register_client(writer)
    service = LLMService(config=provider_config, registry=registry)
    runtime.agent_llm_client = service
    runtime.agent_turn_loop.llm_client = service
    with pytest.raises(HTTPException) as rejected:
        _preflight_reasoning(AgentTurnRequest(
            user_input="No admission", llm={"answer_client_name": "unknown"},
        ), runtime)
    assert rejected.value.status_code == 422
    assert not primary.calls and not writer.calls

    result = runtime.run_agent_turn(session_id="service-route", user_input="answer",
                                    llm_client_name="primary", llm_model="reasoner-v1",
                                    llm_answer_client_name="writer", llm_answer_model="writer-v1")
    assert result.answer == "service answer"
    assert [(call.client_name, call.model) for call in primary.calls] == [
        ("primary", "reasoner-v1"),
    ]
    assert [(call.client_name, call.model) for call in writer.calls] == [
        ("writer", "writer-v1"),
    ]

    primary.calls.clear()
    writer.calls.clear()
    runtime.run_agent_turn(session_id="model-only", user_input="answer",
                           llm_client_name="primary", llm_model="reasoner-v1",
                           llm_answer_model="writer-v2")
    assert [(call.client_name, call.model) for call in primary.calls] == [
        ("primary", "reasoner-v1"), ("primary", "writer-v2"),
    ]
    assert [(call.client_name, call.model) for call in writer.calls] == []
    assert [(call.client_name, call.model) for call in primary.calls
            if call.metadata.get("stage") == "context_answer"] == [
        ("primary", "writer-v2"),
    ]
    # A client may host DeepSeek plus a renderer that does not accept its vendor control.
    service.config.clients[0].thinking_control = "deepseek"
    primary.thinking_control = "deepseek"
    primary.incomplete_answer_once = True
    primary.calls.clear()
    runtime.run_agent_turn(session_id="mixed-client-recovery", user_input="answer",
                           llm_client_name="primary", llm_model="reasoner-v1", llm_answer_model="writer-v2")
    answer_calls = [call for call in primary.calls if call.metadata["stage"] == "context_answer"]
    assert len(answer_calls) == 2
    assert all(call.model == "writer-v2" and call.thinking_enabled is None for call in answer_calls)


def test_existing_run_answer_pair_comes_from_frozen_policy_and_ignores_override(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, graph=True)
    runtime = app.state.runtime
    runtime.session_service.ensure_session(session_id="frozen-policy")
    # Queued/managed roots persist the selection in the policy without duplicate top-level keys.
    run = runtime.agent_run_manager.create_run(session_id="frozen-policy", user_input="answer", metadata={
        "session_policy": {
                           "llm_answer_client_name": "accepted-client",
                           "llm_answer_model": "accepted-model"},
    })
    monkeypatch.setattr(runtime.session_service, "run_policy",
                        lambda _session_id: pytest.fail("resume reread live session policy"))
    assert runtime.resolve_agent_answer_model(
        session_id=run.session_id, existing_run_id=run.run_id,
        requested_client="changed-client", requested_model="changed-model",
    ) == ("accepted-client", "accepted-model")


def test_unsessioned_stream_freezes_shared_answer_defaults(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    with runtime._conn() as conn:
        conn.execute(
            "INSERT INTO agent_ui_preferences(id, preferences, updated_at) VALUES(1, ?, 'now')",
            ('{"llm_answer_client":"writer","llm_answer_model":"writer-v1"}',),
        )
    run = runtime.create_agent_run(session_id=None, user_input="answer")
    assert run.metadata["llm_answer_client_name"] == "writer"
    assert run.metadata["llm_answer_model"] == "writer-v1"
    assert run.metadata["session_policy"]["llm_answer_model"] == "writer-v1"


def test_parallel_turns_keep_answer_models_isolated(tmp_path, monkeypatch):
    runtime = _app(tmp_path, monkeypatch).state.runtime
    fake = RecordingLLM()
    runtime.agent_turn_loop.llm_client = fake

    def run(label):
        return runtime.run_agent_turn(
            session_id=label, user_input="Reply", llm_client_name="primary-" + label,
            llm_model="reasoner-" + label, llm_answer_client_name="writer-" + label,
            llm_answer_model="answer-" + label,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ["first", "second"]))
        for result, label in zip(results, ["first", "second"], strict=True):
            policy = runtime.agent_run_manager.get_run(result.run_id).metadata["session_policy"]
            assert policy["llm_answer_model"] == "answer-" + label
        assert {(call["client_name"], call["model"]) for call in fake.calls} == {
            ("primary-first", "reasoner-first"), ("primary-second", "reasoner-second"),
            ("writer-first", "answer-first"), ("writer-second", "answer-second"),
        }
        fake.calls.clear()
        runtime.run_agent_turn(session_id="following", user_input="Reply")
        assert {(call["client_name"], call["model"]) for call in fake.calls} == {(None, None)}
    finally:
        runtime.stop()


def test_internal_silent_completion_uses_inference_model(tmp_path, monkeypatch):
    runtime = _app(tmp_path, monkeypatch).state.runtime
    fake = RecordingLLM()
    runtime.agent_turn_loop.llm_client = fake
    runtime.agent_turn_loop.answer_visibility_provider = lambda _run_id: {
        "user_visible": False, "notification": {"decision_source": "explicit"},
    }
    try:
        runtime.run_agent_turn(
            session_id="silent", user_input="Internal update", llm_client_name="primary", llm_model="reasoner",
            llm_answer_client_name="writer", llm_answer_model="answer-model",
        )
        assert {(call["client_name"], call["model"]) for call in fake.calls} == {("primary", "reasoner")}
    finally:
        runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
@pytest.mark.parametrize("stream", [False, True])
def test_unified_decision_and_final_answer_use_separate_services(tmp_path, monkeypatch, orchestrator, stream):
    from tests.test_reasoning_turn_integration import install_service
    from tests.test_unified_entry_quality import operation, runtime_for

    runtime, _ = runtime_for(tmp_path, monkeypatch, orchestrator, [])
    try:
        primary_calls = install_service(runtime, monkeypatch, [operation("final_answer", reason="Ready.")])
        service = runtime.agent_llm_client
        service.config.clients.append(LLMClientConfig(
            name="writer", provider="mock", default_model="writer-v1", context_window_tokens=49152,
            output_reserve_tokens=2048,
        ))
        writer = ServiceRecordingClient("writer", "writer-v1")
        service.registry.register_client(writer)
        result = runtime.run_agent_turn(
            session_id="unified-dual", user_input="Say hello.", llm_reasoning_level="high",
            llm_answer_client_name="writer", llm_answer_model="writer-v1",
            llm_response_mode=LLMResponseMode.STREAM if stream else LLMResponseMode.TEXT,
        )
        assert result.answer == "service answer"
        assert [request.metadata["stage"] for request, _wire in primary_calls] == ["decision"]
        expected_stage = "context_answer" if orchestrator == "langgraph" else "answer"
        assert [request.metadata["stage"] for request in writer.calls] == [expected_stage]
        assert writer.calls[0].thinking_enabled is None
        assert writer.calls[0].reasoning_effort is None
        assert writer.calls[0].metadata["reasoning_level_requested"] == "high"
        assert "reasoning_capability_snapshot" not in writer.calls[0].metadata
        run = runtime.agent_run_manager.get_run(result.run_id)
        assert run.metadata["session_policy"]["llm_answer_client_name"] == "writer"
    finally:
        runtime.stop()
