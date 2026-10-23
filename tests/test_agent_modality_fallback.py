"""Deterministic default routing and actual multimodal Agent dispatch, without network."""
import json

import pytest

from app.core.llm.errors import LLMClientError
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.llm.registry import LLMClientRegistry
from app.core.llm.service import LLMService
from app.core.local_config import LLMClientConfig, LLMModelConfigOverride, LLMProviderConfig
from tests.test_agent_image_input import _assert_images, _png, _session, _turn, _upload
from tests.test_agent_image_input import image_runtime as _image_runtime

image_runtime = _image_runtime


def routing_service(*, image_default=True, audio_default=False, fallback=False, audio_capacity=True):
    registry = LLMClientRegistry()
    configs = []
    for name, vision, audio, capacity in (
        ("default", image_default, audio_default, 200000),
        ("selected", False, False, 100000),
        ("audio", True, True, 150000 if audio_capacity else None),
        ("fallback", True, True, 180000),
    ):
        registry.register_client(OpenAICompatibleLLMClient(
            name=name, provider_name="offline", base_url="https://invalid.example/v1", api_key="offline",
            default_model=name + "-model", supports_vision=vision, supports_audio=audio,
        ))
        configs.append(LLMClientConfig(name=name, default_model=name + "-model", context_window_tokens=capacity))
    return LLMService(config=LLMProviderConfig(
        default_client="default", clients=configs, audio_client="audio", audio_model="audio-model",
        fallback_client="fallback" if fallback else None,
    ), registry=registry)


def test_supported_selection_and_text_remain_on_selected_model():
    service = routing_service()
    assert service.resolve_input_identity(client_name="audio", has_images=True, has_audio=True) == ("audio", "audio-model")
    assert service.resolve_input_identity(client_name="selected") == ("selected", "selected-model")


@pytest.mark.parametrize("images,audio,expected", [
    (True, False, ("default", "default-model")),
    (False, True, ("audio", "audio-model")),
    (True, True, ("audio", "audio-model")),
])
def test_default_fallback_checks_all_modalities(images, audio, expected):
    service = routing_service()
    assert service.resolve_input_identity(client_name="selected", has_images=images, has_audio=audio) == expected


def test_missing_capacity_or_partial_support_cannot_be_used_as_fallback():
    service = routing_service(audio_capacity=False)
    with pytest.raises(LLMClientError, match="No configured default model supports image/audio"):
        service.resolve_input_identity(client_name="selected", has_images=True, has_audio=True)
    service.config.fallback_client = "fallback"
    assert service.resolve_input_identity(client_name="selected", has_images=True, has_audio=True) == ("fallback", "fallback-model")
    service.config.fallback_client = None
    service.config.clients[0].context_window_tokens = None
    with pytest.raises(LLMClientError, match="context_window_tokens"):
        service.resolve_input_identity(client_name="selected", has_images=True)


def test_unknown_selection_does_not_silently_become_a_default():
    with pytest.raises(LLMClientError, match="not registered"):
        routing_service().resolve_input_identity(client_name="typo", has_images=True)


@pytest.mark.parametrize("stream", [False, True])
def test_image_answer_fallback_does_not_require_unused_model_capacity(image_runtime, stream):
    env = image_runtime
    sid = _session(env)
    image = _png(160)
    attachment = _upload(env, sid, image)
    assert env.runtime.agent_llm_client.config.resolve_context_capacity(env.provider.name, "text_only") is None
    response = env.client.post("/agent/turn/stream" if stream else "/agent/turn", json={
        "session_id": sid, "user_input": "Describe the image.", "attachment_ids": [attachment],
        "llm": {"answer_model": "text_only"},
    })
    assert response.status_code == 200, response.text
    if stream:
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        completed = [frame for frame in frames if frame["type"] == "run_completed"]
        assert completed, response.text
        run_id = completed[-1]["run_id"]
    else:
        run_id = response.json()["run_id"]
    run = env.runtime.agent_run_manager.get_run(run_id)
    assert run.metadata["llm_answer_model"] == "visual"
    assert run.metadata["session_policy"]["llm_answer_model"] == "visual"
    assert env.provider.requests
    assert all(request.model == "visual" for request in env.provider.requests)
    _assert_images(env.provider.requests, [image])


@pytest.mark.parametrize("stream", [False, True])
def test_image_fallback_routes_primary_and_answer_and_preserves_preferences(image_runtime, stream):
    env = image_runtime
    sid = _session(env)
    image = _png(210)
    attachment = _upload(env, sid, image)
    env.runtime.agent_llm_client.config.clients[0].model_overrides["text_only"] = LLMModelConfigOverride(
        context_window_tokens=200000,
    )
    payload = {"session_id": sid, "user_input": "Describe the image.", "attachment_ids": [attachment],
               "llm": {"model": "text_only", "answer_model": "text_only"}}
    if stream:
        response = env.client.post("/agent/turn/stream", json=payload)
        assert response.status_code == 200, response.text
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        completed = [frame for frame in frames if frame["type"] == "run_completed"]
        assert completed, response.text
        run_id = completed[-1]["run_id"]
    else:
        response = env.client.post("/agent/turn", json=payload)
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
    assert env.provider.requests
    assert all(request.model == "visual" for request in env.provider.requests)
    _assert_images(env.provider.requests, [image])
    run = env.runtime.agent_run_manager.get_run(run_id)
    assert run.metadata["llm_answer_model"] == "visual"
    assert run.metadata["session_policy"]["llm_answer_model"] == "visual"
    if env.orchestrator == "langgraph":
        frozen = env.runtime.agent_turn_runner.get_state(run_id).values["request"]
        assert frozen["llm_model"] == frozen["llm_answer_model"] == "visual"
    assert env.provider.default_model == "visual"
    assert env.runtime.session_service.run_policy(sid).llm_answer_model is None
    env.provider.requests.clear()
    _turn(env, sid, user_input="Reply in text.", attachment_ids=[], llm={"model": "text_only", "answer_model": "text_only"})
    assert all(request.model == "text_only" for request in env.provider.requests)


def test_manager_batch_uses_final_modalities_and_keeps_original_receipts(image_runtime):
    env = image_runtime
    runtime = env.runtime
    runtime.local_app_config.management.enabled = True
    runtime.agent_llm_client.config.clients[0].model_overrides["text_only"] = LLMModelConfigOverride(context_window_tokens=200000)
    sid = runtime.management_service.create(
        make_default=True, llm_client_name=env.provider.name, llm_model="text_only",
        llm_answer_client_name=env.provider.name, llm_answer_model="text_only",
    )["session"]["session_id"]
    attachment = _upload(env, sid, _png(180))
    first = runtime.session_inputs.submit(sid, client_message_id="text-first", user_input="Consider this.", attachment_ids=[])
    runtime.session_inputs.tick()
    second = runtime.session_inputs.submit(sid, client_message_id="image-second", user_input="And this image.", attachment_ids=[attachment])
    assert runtime.agent_dispatch.run_one()
    run = runtime.agent_run_manager.get_run(first["input"]["run_id"] or runtime.session_inputs.snapshot(sid)["inputs"][0]["run_id"])
    assert run.status.value == "completed", run.error
    assert len(run.metadata["session_input_batch"]) == 2
    assert run.metadata["llm_answer_model"] == run.metadata["session_policy"]["llm_model"] == "visual"
    assert all(request.model == "visual" for request in env.provider.requests)
    assert runtime.management_service.get(sid)["controller"]["llm_answer_model"] == "text_only"
    replay = runtime.session_inputs.submit(sid, client_message_id="image-second", user_input="And this image.", attachment_ids=[attachment])
    assert replay["input"]["input_id"] == second["input"]["input_id"]


def test_audio_keeps_compatible_selection_and_skips_incomplete_default(image_runtime):
    from tests.test_agent_audio_input import upload

    env = image_runtime
    sid = _session(env)
    attachment, _ = upload(env, sid)
    env.provider.audio_model_overrides = {"audio-selected": True, "audio-default": True, "visual": True}
    service = env.runtime.agent_llm_client
    service.config.audio_model = "audio-default"
    service.config.clients[0].model_overrides["audio-selected"] = LLMModelConfigOverride(context_window_tokens=200000)
    assert env.runtime.resolve_agent_input_model(
        session_id=sid, attachment_ids=[attachment], llm_model="audio-selected",
    ) == (env.provider.name, "audio-selected")
    # The dedicated audio default has no capacity; the complete global default
    # must still be tried rather than failing at the incomplete candidate.
    assert env.runtime.resolve_agent_input_model(
        session_id=sid, attachment_ids=[attachment], llm_model="text_only",
    ) == (env.provider.name, "visual")


def test_queued_unspecified_primary_does_not_read_changed_session_preference(image_runtime):
    env = image_runtime
    runtime = env.runtime
    runtime.local_app_config.management.enabled = True
    sid = runtime.management_service.create(make_default=True)["session"]["session_id"]
    attachment = _upload(env, sid, _png(120))
    queued = runtime.create_agent_run(session_id=sid, user_input="Describe.", resumable=True)
    assert queued.metadata["session_policy"]["llm_model"] is None
    runtime.management_service.update(sid, expected_revision=1, llm_model="changed-visual")
    resolved = runtime._prepare_agent_turn_input(
        session_id=sid, attachment_ids=[attachment], llm_client_name=None, llm_model=None,
        llm_answer_client_name=None, llm_answer_model=None, existing_run_id=queued.run_id,
    )
    assert resolved[1:3] == (env.provider.name, "visual")
    assert runtime.agent_run_manager.get_run(queued.run_id).metadata["session_policy"]["llm_model"] == "visual"
    assert runtime.management_service.get(sid)["controller"]["llm_model"] == "changed-visual"
    assert not env.provider.requests


def test_checkpoint_resume_retains_fallback_after_defaults_change(image_runtime):
    import asyncio

    env = image_runtime
    if env.orchestrator != "langgraph":
        pytest.skip("Checkpoint resume belongs to the graph orchestrator.")
    runtime = env.runtime
    sid = _session(env)
    attachment = _upload(env, sid, _png(95))
    runtime.agent_llm_client.config.clients[0].model_overrides["text_only"] = LLMModelConfigOverride(context_window_tokens=200000)
    queued = runtime.create_agent_run(
        session_id=sid, user_input="Describe after resuming.", llm_answer_model="text_only",
    )
    ids, client, model, answer_client, answer_model = runtime._prepare_agent_turn_input(
        session_id=sid, attachment_ids=[attachment], llm_client_name=None, llm_model="text_only",
        llm_answer_client_name=None, llm_answer_model=None, existing_run_id=queued.run_id,
    )
    runner = runtime.agent_turn_runner
    runner.interrupt_after = ["initialize_run"]
    asyncio.run(runner._invoke(
        run_id=queued.run_id, session_id=sid, user_input=queued.user_input, attachment_ids=ids,
        llm_client_name=client, llm_model=model, llm_answer_client_name=answer_client,
        llm_answer_model=answer_model,
    ))
    service = runtime.agent_llm_client
    service.config.clients[0].model_overrides["visual"] = LLMModelConfigOverride(context_window_tokens=200000)
    service.config.clients[0].default_model = env.provider.default_model = "text_only"
    # Resuming uses checkpoint request + run snapshot, with no new default resolution.
    runner.interrupt_after = []
    result = runtime.resume_agent_run(queued.run_id)
    assert result.answer == "The image contains red pixels."
    assert all(request.model == "visual" for request in env.provider.requests)
    frozen = runner.get_state(queued.run_id).values["request"]
    assert frozen["llm_model"] == frozen["llm_answer_model"] == "visual"
