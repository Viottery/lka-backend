"""API preflight shares a fresh policy without caching admission authority."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import agent as routes
from app.api.schemas import AgentTurnLLMOptions, AgentTurnRequest
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.sessions import SessionRunPolicy
from app.domains.session_attachments import SessionAttachmentError
from tests.test_management_api import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def _payload(**kwargs):
    return AgentTurnRequest(session_id="session", user_input="synthetic input", attachment_ids=[], **kwargs)


def _fake(policy=None):
    seen = []
    policy = policy or SessionRunPolicy(llm_reasoning_level="high", allowed_tools=["example.read"])

    def capture(stage):
        def call(**kwargs):
            seen.append((stage, kwargs))
            if stage == "image":
                return kwargs["attachment_ids"]
            if stage == "resolve":
                return kwargs["llm_client_name"], kwargs["llm_model"]
        return call

    instance = SimpleNamespace(
        session_service=SimpleNamespace(run_policy=lambda _sid: policy),
        prepare_agent_image_input=capture("image"), resolve_agent_input_model=capture("resolve"),
        validate_agent_answer_selection=capture("answer"),
        validate_agent_reasoning_selection=capture("reasoning"),
    )
    return instance, seen, policy


def test_real_runtime_reads_one_policy_per_api_preflight_and_observes_next_change(runtime, monkeypatch):
    session_id = runtime.session_service.create_session(title="API preflight").session.session_id
    current = SessionRunPolicy(llm_reasoning_level="low")
    reads, levels = [], []
    original = runtime.validate_agent_reasoning_selection

    def policy(sid):
        reads.append(sid)
        return current

    def reasoning(**kwargs):
        levels.append(kwargs["frozen_policy"].llm_reasoning_level)
        return original(**kwargs)

    monkeypatch.setattr(runtime.session_service, "run_policy", policy)
    monkeypatch.setattr(runtime, "validate_agent_reasoning_selection", reasoning)
    payload = AgentTurnRequest(session_id=session_id, user_input="synthetic", attachment_ids=[])
    routes._preflight_agent_payload(payload, runtime)
    current = current.model_copy(update={"llm_reasoning_level": "high"})
    routes._preflight_agent_payload(payload, runtime)
    assert reads == [session_id, session_id]
    assert levels == ["low", "high"]


def test_all_selection_stages_share_one_deep_copy_of_policy():
    runtime, seen, policy = _fake()
    routes._preflight_agent_payload(_payload(), runtime)
    assert [stage for stage, _ in seen] == ["image", "resolve", "answer", "reasoning"]
    snapshots = [kwargs["frozen_policy"] for _, kwargs in seen]
    assert all(snapshot is snapshots[0] for snapshot in snapshots)
    assert snapshots[0] is not policy
    policy.allowed_tools.append("example.write")
    assert snapshots[0].allowed_tools == ["example.read"]


def test_request_model_answer_and_reasoning_overrides_keep_precedence():
    runtime, seen, _ = _fake(SessionRunPolicy(
        llm_answer_client_name="preferred-answer", llm_answer_model="preferred-model",
        llm_reasoning_level="high",
    ))
    payload = _payload(llm=AgentTurnLLMOptions(
        client_name="explicit-primary", model="explicit-model", answer_model="explicit-answer-model",
        reasoning_level="low",
    ))
    routes._preflight_agent_payload(payload, runtime)
    calls = dict(seen)
    assert calls["image"]["llm_client_name"] == "explicit-primary"
    assert calls["resolve"]["llm_model"] == "explicit-model"
    assert calls["answer"]["llm_answer_client_name"] == "explicit-primary"
    assert calls["answer"]["llm_answer_model"] == "explicit-answer-model"
    resolver = LocalKnowledgeAgentRuntime.resolve_agent_answer_model
    assert resolver(runtime, session_id="session", requested_client=calls["answer"]["llm_answer_client_name"],
                    requested_model=calls["answer"]["llm_answer_model"],
                    frozen_policy=calls["answer"]["frozen_policy"]) == (
                        "explicit-primary", "explicit-answer-model",
                    )
    assert LocalKnowledgeAgentRuntime.resolve_agent_reasoning_level(
        runtime, session_id="session", requested=calls["reasoning"]["requested"],
        frozen_policy=calls["reasoning"]["frozen_policy"],
    ) == ("low", "request")


def test_fake_without_policy_provider_keeps_original_keyword_arguments():
    def image(*, session_id, attachment_ids, llm_client_name, llm_model):
        return attachment_ids

    def resolve(*, session_id, attachment_ids, llm_client_name, llm_model):
        return llm_client_name, llm_model

    def answer(*, session_id, attachment_ids, resolve_preference, primary_client_name):
        assert resolve_preference is True

    def reasoning(*, session_id, requested, attachment_ids, llm_client_name, llm_model):
        pass

    runtime = SimpleNamespace(
        prepare_agent_image_input=image, resolve_agent_input_model=resolve,
        validate_agent_answer_selection=answer, validate_agent_reasoning_selection=reasoning,
    )
    assert routes._preflight_agent_payload(_payload(), runtime).attachment_ids == []


@pytest.mark.parametrize(("stage", "error", "status"), [
    ("image", KeyError("synthetic"), 404),
    ("image", SessionAttachmentError("synthetic attachment", 413), 413),
    ("image", OSError("synthetic"), 422),
    ("answer", ValueError("synthetic answer"), 422),
    ("reasoning", ValueError("synthetic reasoning"), 422),
    ("policy", KeyError("synthetic"), 404),
    ("policy", ValueError("synthetic policy"), 422),
])
def test_preflight_preserves_http_errors(stage, error, status):
    runtime, _, _ = _fake()

    def fail(*_args, **_kwargs):
        raise error

    if stage == "policy":
        runtime.session_service.run_policy = fail
    else:
        attr = {"image": "prepare_agent_image_input", "answer": "validate_agent_answer_selection",
                "reasoning": "validate_agent_reasoning_selection"}[stage]
        setattr(runtime, attr, fail)
    with pytest.raises(HTTPException) as caught:
        routes._preflight_agent_payload(_payload(), runtime)
    assert caught.value.status_code == status


@pytest.mark.parametrize("stream", [False, True])
def test_api_preflight_runs_in_one_thread_call_and_does_not_block_loop(monkeypatch, stream):
    runtime, seen, policy = _fake()
    entered, release = threading.Event(), threading.Event()
    policy_threads, calls = [], []
    main_thread = threading.get_ident()

    def get_policy(_sid):
        policy_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(timeout=5)
        return policy

    runtime.session_service.run_policy = get_policy
    original_to_thread = asyncio.to_thread

    async def tracked_to_thread(function, *args, **kwargs):
        calls.append(function)
        return await original_to_thread(function, *args, **kwargs)

    async def execute(**kwargs):
        assert "frozen_policy" not in kwargs
        return "synthetic response"

    async def admit(**kwargs):
        assert "frozen_policy" not in kwargs
        return SimpleNamespace(run_id="synthetic-run")

    runtime.run_agent_turn_async = execute
    monkeypatch.setattr(asyncio, "to_thread", tracked_to_thread)
    monkeypatch.setattr(routes, "_admit_agent_turn", admit)
    monkeypatch.setattr(routes.AgentTurnResponse, "from_result", lambda result: result)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))

    async def scenario():
        endpoint = routes.stream_agent_turn if stream else routes.run_agent_turn_endpoint
        task = asyncio.create_task(endpoint(_payload(), request))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
            assert not task.done()
            assert policy_threads == [policy_threads[0]]
            assert policy_threads[0] != main_thread
        finally:
            release.set()
        result = await task
        if stream:
            assert result.media_type == "text/event-stream"
        else:
            assert result == "synthetic response"

    asyncio.run(scenario())
    assert calls == [routes._preflight_agent_payload]
    assert [stage for stage, _ in seen] == ["image", "resolve", "answer", "reasoning"]
