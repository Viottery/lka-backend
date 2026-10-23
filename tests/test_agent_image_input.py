"""Offline image-input contracts against an isolated temporary runtime."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.config import get_settings
from app.core.llm.models import LLMImageBlock, LLMResponse, LLMStreamEvent, LLMTextBlock
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.llm.registry import LLMClientRegistry
from app.core.llm.service import LLMService
from app.core.local_config import LLMClientConfig, LLMProviderConfig


def _png(red: int) -> bytes:
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    pixels = b"\x00" + bytes([red, 0, 0]) * 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels * 2))
        + chunk(b"IEND", b"")
    )


class _CapturingVisionClient(OpenAICompatibleLLMClient):
    def __init__(self):
        super().__init__(
            name="vision_test",
            provider_name="vision_test",
            base_url="https://invalid.example/v1",
            api_key="offline",
            default_model="visual",
            supports_vision=True,
            vision_model_overrides={"text_only": False},
            supports_stream=True,
        )
        self.requests = []
        self.route_package = "filesystem"

    def _answer(self, request):
        stage = request.metadata.get("stage")
        if stage == "route":
            return json.dumps(
                {
                    "selected_package": self.route_package,
                    "reason": "Inspect image.",
                    "search_query": None,
                }
            )
        if stage in {"decision", "decision_repair"}:
            return json.dumps(
                {
                    "assistant_message": "Image examined.",
                    "operation": {
                        "type": "final_answer",
                        "reason": "Image evidence is sufficient.",
                        "final_answer": None,
                        "package_name": None,
                        "tool_name": None,
                        "tool_input": {},
                    },
                }
            )
        if stage == "tool_result_check":
            return '{"status":"accepted","message":"okay","remaining_work":""}'
        return "The image contains red pixels."

    async def complete(self, request):
        self.requests.append(request.model_copy(deep=True))
        return LLMResponse(
            provider=self.provider_name,
            status="completed",
            content=self._answer(request),
            prompt_summary=request.prompt_summary,
            client_name=self.name,
            model=request.model,
        )

    async def stream(self, request):
        self.requests.append(request.model_copy(deep=True))
        content = self._answer(request)
        fields = {
            "stage": str(request.metadata.get("stage")),
            "client_name": self.name,
            "provider": self.provider_name,
            "model": request.model or self.default_model,
        }
        yield LLMStreamEvent(event_type="llm_started", **fields)
        yield LLMStreamEvent(
            event_type="llm_delta", delta=content, content_snapshot=content, **fields
        )
        yield LLMStreamEvent(event_type="llm_completed", content_snapshot=content, **fields)


@pytest.fixture(params=["legacy", "langgraph"])
def image_runtime(request, tmp_path, monkeypatch):
    config = tmp_path / "local.toml"
    config.write_text(f'[agent]\norchestrator = "{request.param}"\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    app = create_app()
    provider = _CapturingVisionClient()
    registry = LLMClientRegistry()
    registry.register_client(provider)
    service = LLMService(
        config=LLMProviderConfig(
            default_client=provider.name,
            clients=[
                LLMClientConfig(
                    name=provider.name,
                    default_model="visual",
                    context_window_tokens=200000,
                    supports_vision=True,
                )
            ],
        ),
        registry=registry,
    )
    runtime = app.state.runtime
    runtime.agent_llm_client = service
    runtime.agent_turn_loop.llm_client = service
    client = TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False)
    yield SimpleNamespace(
        app=app,
        runtime=runtime,
        client=client,
        provider=provider,
        data_dir=tmp_path / "data",
        orchestrator=request.param,
    )
    runtime.stop()
    client.close()
    get_settings.cache_clear()


def _session(env):
    response = env.client.post("/sessions", json={"title": "Isolated image test"})
    assert response.status_code == 200, response.text
    return response.json()["session"]["session_id"]


def _upload(env, sid, image, filename="红色.png"):
    response = env.client.post(
        f"/sessions/{sid}/attachments",
        content=image,
        headers={"Content-Type": "image/png", "X-Filename": quote(filename)},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["filename"] == filename
    assert result["media_type"] == "image/png"
    assert result["size_bytes"] == len(image)
    assert (result["width"], result["height"]) == (2, 2)
    assert result["sha256"] == hashlib.sha256(image).hexdigest()
    return result["attachment_id"]


def _turn(env, sid, **payload):
    response = env.client.post("/agent/turn", json={"session_id": sid, **payload})
    assert response.status_code == 200, response.text
    return response.json()


def _images(request):
    return [
        base64.b64decode(block.image_url.url.split(",", 1)[1])
        for message in request.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, LLMImageBlock)
    ]


def _assert_images(requests, expected):
    main = [
        request
        for request in requests
        if request.metadata.get("stage") in {"route", "decision", "answer", "context_answer"}
    ]
    assert main
    for request in main:
        assert _images(request) == expected
        user = next(message for message in request.messages if message.role == "user")
        assert any(isinstance(block, LLMTextBlock) and block.text for block in user.content)
    for request in requests:
        if request.metadata.get("stage") not in {"route", "decision", "answer", "context_answer"}:
            assert _images(request) == []


def _assert_no_encoded_images(env, images, public_result):
    needles = [base64.b64encode(image) for image in images] + [b"data:image/"]
    assert all(needle not in json.dumps(public_result).encode() for needle in needles)
    # Attachments keep original bytes; every other persisted artifact must contain references only.
    for path in env.data_dir.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert all(needle not in data for needle in needles), str(path)


def test_agent_image_input_dispatches_bytes_and_persists_references(image_runtime):
    env = image_runtime
    sid = _session(env)
    image = _png(255)
    attachment = _upload(env, sid, image)
    result = _turn(env, sid, user_input="Describe this image.", attachment_ids=[attachment])
    assert result["answer"] == "The image contains red pixels."
    stages = {request.metadata.get("stage") for request in env.provider.requests}
    assert {"route", "decision", "answer"} <= stages
    _assert_images(env.provider.requests, [image])
    detail = env.client.get(f"/sessions/{sid}").json()
    user = next(message for message in detail["messages"] if message["role"] == "user")
    assert user["payload"]["attachment_ids"] == [attachment]
    if env.orchestrator == "langgraph":
        snapshot = env.runtime.agent_turn_runner.get_state(result["run_id"])
        assert snapshot.values["request"]["attachment_ids"] == [attachment]
        # Recovery uses the persisted result while preserving the same image selection.
        env.runtime.agent_turn_runner._results.clear()
        recovered = env.runtime.agent_turn_runner.resume(result["run_id"])
        assert recovered.answer == result["answer"]
    _assert_no_encoded_images(env, [image], result)


def test_image_only_context_answer_accepts_empty_text(image_runtime):
    env = image_runtime
    env.provider.route_package = None
    sid = _session(env)
    image = _png(200)
    attachment = _upload(env, sid, image)
    result = _turn(env, sid, attachment_ids=[attachment])
    assert result["answer"] == "The image contains red pixels."
    assert "context_answer" in {request.metadata.get("stage") for request in env.provider.requests}
    _assert_images(env.provider.requests, [image])
    user = next(
        message
        for message in env.client.get(f"/sessions/{sid}").json()["messages"]
        if message["role"] == "user"
    )
    assert user["content"] == "[Image input]"
    assert user["payload"]["attachment_ids"] == [attachment]


def test_large_image_upload_stays_bounded_when_sent_to_model(image_runtime, monkeypatch):
    env = image_runtime
    sid = _session(env)
    monkeypatch.setattr(
        env.runtime.session_attachment_service, "get",
        lambda **_kwargs: {"kind": "image", "size_bytes": 12 * 1024 * 1024 + 1},
    )
    from app.domains.session_attachments import SessionAttachmentError
    with pytest.raises(SessionAttachmentError, match="12 MiB"):
        env.runtime.prepare_agent_attachment_input(session_id=sid, attachment_ids=["att_large"])


def test_multiple_images_followup_reuses_null_and_clear_is_persistent(image_runtime):
    env = image_runtime
    sid = _session(env)
    images = [_png(100), _png(150)]
    ids = [_upload(env, sid, image, f"{index}.png") for index, image in enumerate(images)]
    _turn(env, sid, user_input="Compare.", attachment_ids=ids)
    _assert_images(env.provider.requests, images)
    for selection in [{}, {"attachment_ids": None}]:
        env.provider.requests.clear()
        _turn(env, sid, user_input="Explain again.", **selection)
        _assert_images(env.provider.requests, images)
    for selection in [{"attachment_ids": []}, {}]:
        env.provider.requests.clear()
        _turn(env, sid, user_input="Continue using text.", **selection)
        assert env.provider.requests
        assert all(_images(request) == [] for request in env.provider.requests)


def test_invalid_image_selection_rejects_before_creating_runs(image_runtime):
    env = image_runtime
    sid, other_sid = _session(env), _session(env)
    attachment = _upload(env, sid, _png(50))
    cases = [
        ({"session_id": other_sid, "attachment_ids": [attachment]}, 404),
        ({"session_id": "missing", "attachment_ids": [attachment]}, 404),
        ({"attachment_ids": [attachment]}, 422),
        ({"session_id": sid, "attachment_ids": ["att_" + "0" * 32]}, 404),
        ({"session_id": sid, "attachment_ids": ["missing"]}, 422),
        ({"session_id": sid, "attachment_ids": [attachment] * 5}, 422),
        ({"session_id": sid, "attachment_ids": [""]}, 422),
    ]
    before = len(env.runtime.agent_run_manager._runs)
    for payload, status in cases:
        response = env.client.post("/agent/turn", json={"user_input": "Describe.", **payload})
        assert response.status_code == status, response.text
        assert len(env.runtime.agent_run_manager._runs) == before
    assert env.provider.requests == []
    assert env.client.delete(f"/sessions/{sid}").status_code == 204
    response = env.client.post(
        "/agent/turn",
        json={"session_id": sid, "user_input": "Describe.", "attachment_ids": [attachment]},
    )
    assert response.status_code == 404, response.text


def test_agent_image_stream_dispatches_image_and_emits_answer_delta(image_runtime):
    env = image_runtime
    sid = _session(env)
    image = _png(75)
    attachment = _upload(env, sid, image)
    response = env.client.post(
        "/agent/turn/stream",
        json={"session_id": sid, "user_input": "Describe.", "attachment_ids": [attachment]},
    )
    assert response.status_code == 200, response.text
    frames = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert any(
        frame["type"] == "llm_delta"
        and frame["payload"]["content_role"] == "final_answer"
        and frame["payload"]["delta"] == "The image contains red pixels."
        for frame in frames
    )
    assert any(frame["type"] == "run_completed" for frame in frames)
    _assert_images(env.provider.requests, [image])
    _assert_no_encoded_images(env, [image], frames)


def test_old_text_request_needs_no_existing_session_or_images(image_runtime):
    env = image_runtime
    result = _turn(env, "new_text_session", user_input="Answer directly.")
    assert result["answer"]
    assert env.provider.requests
    assert all(_images(request) == [] for request in env.provider.requests)


def test_image_refs_survive_durable_graph_restart(image_runtime):
    import asyncio

    env = image_runtime
    if env.orchestrator != "langgraph":
        pytest.skip("Durable resume belongs to the graph orchestrator.")
    sid = _session(env)
    image = _png(125)
    attachment = _upload(env, sid, image)
    runner = env.runtime.agent_turn_runner
    runner.interrupt_after = ["initialize_run"]
    run = env.runtime.create_agent_run(session_id=sid, user_input="Describe after restart.")
    asyncio.run(
        runner._invoke(
            run_id=run.run_id,
            session_id=sid,
            user_input=run.user_input,
            attachment_ids=[attachment],
        )
    )
    paused = runner.get_state(run.run_id)
    assert paused.values["request"]["attachment_ids"] == [attachment]
    assert paused.next == ("prepare_context",)
    assert not env.provider.requests
    env.runtime.stop()
    get_settings.cache_clear()
    restored_app = create_app()
    restored = restored_app.state.runtime
    restored.agent_llm_client = env.runtime.agent_llm_client
    restored.agent_turn_loop.llm_client = env.runtime.agent_llm_client
    try:
        result = restored.resume_agent_run(run.run_id)
        assert result.answer == "The image contains red pixels."
        _assert_images(env.provider.requests, [image])
        snapshot = restored.agent_turn_runner.get_state(run.run_id)
        assert snapshot.values["request"]["attachment_ids"] == [attachment]
        _assert_no_encoded_images(env, [image], result.model_dump(mode="json"))
    finally:
        restored.stop()


@pytest.mark.parametrize("stream", [False, True, "failure_event"])
def test_provider_image_error_cannot_leak_bytes_to_agent_artifacts(image_runtime, stream):
    from app.core.llm.errors import LLMProviderHTTPError

    env = image_runtime
    sid = _session(env)
    image = _png(175)
    attachment = _upload(env, sid, image)
    url = "data:image/png;base64," + base64.b64encode(image).decode()

    def failure():
        return LLMProviderHTTPError(
            status_code=400,
            message="Rejected " + url,
            error_body={"error": {"message": url, "content": [url]}},
            provider_error_type="invalid_request_error",
            provider_error_code="invalid_image",
        )

    async def complete(request):
        env.provider.requests.append(request.model_copy(deep=True))
        raise failure()

    async def events(request):
        env.provider.requests.append(request.model_copy(deep=True))
        if stream == "failure_event":
            yield LLMStreamEvent(
                event_type="llm_failed",
                stage=str(request.metadata.get("stage")),
                client_name=env.provider.name,
                provider=env.provider.provider_name,
                model=request.model or env.provider.default_model,
                error="Rejected " + url,
                metadata={"error_body": {"message": url}, "http_status": 400},
            )
            return
        raise failure()

    env.provider.complete = complete
    env.provider.stream = events
    endpoint = "/agent/turn/stream" if stream else "/agent/turn"
    response = env.client.post(
        endpoint,
        json={"session_id": sid, "user_input": "Describe.", "attachment_ids": [attachment]},
    )
    assert response.status_code in {200, 500, 502, 503}, response.text
    assert env.provider.requests
    _assert_images(env.provider.requests, [image])
    _assert_no_encoded_images(env, [image], response.text)


def test_successful_provider_metadata_cannot_persist_submitted_image(image_runtime):
    env = image_runtime
    sid = _session(env)
    image = _png(225)
    attachment = _upload(env, sid, image)
    original_complete = env.provider.complete

    async def complete(request):
        response = await original_complete(request)
        image_block = next(
            (
                block
                for message in request.messages
                if isinstance(message.content, list)
                for block in message.content
                if isinstance(block, LLMImageBlock)
            ),
            None,
        )
        if image_block is None:
            return response
        url = image_block.image_url.url
        return response.model_copy(
            update={
                "metadata": {
                    "provider_diagnostics": {
                        "submitted_image": url,
                        "submitted_bytes": url.split(",", 1)[1],
                    },
                    "benign_marker": "metadata survives redaction",
                }
            }
        )

    env.provider.complete = complete
    result = _turn(env, sid, user_input="Describe.", attachment_ids=[attachment])
    assert result["answer"] == "The image contains red pixels."
    _assert_images(env.provider.requests, [image])
    _assert_no_encoded_images(env, [image], result)


def test_image_reservation_trims_removable_history_before_dispatch(image_runtime):
    from app.core.agent_turn import _turn_input_content, _turn_run_id, _turn_run_manager

    env = image_runtime
    sid = _session(env)
    service = env.runtime.agent_llm_client
    service.config.clients[0].context_window_tokens = 56000
    image = _png(235)
    attachment = _upload(env, sid, image)
    current_input = "ESSENTIAL_CURRENT_IMAGE_INPUT: Describe this image."
    history_text = ("REMOVABLE_IMAGE_HISTORY Earlier unrelated conversation details. ") * 40
    history = [
        {"role": "user" if index % 2 == 0 else "agent", "content": history_text}
        for index in range(10)
    ]
    history.extend(
        [
            {"role": "agent", "content": "Recent short context."},
            {"role": "user", "content": current_input},
        ]
    )
    user_prompt = json.dumps(
        {"user_input": current_input, "session_context_window": {"recent_messages": history}}
    )
    loop = env.runtime.agent_turn_loop
    system_prompt = "Describe the user's attached image using the current input."
    # The old text-only budget fits this prompt; reserving the image requires trimming.
    baseline = loop._budget_llm_prompt(
        system_prompt=system_prompt, user_prompt=user_prompt, max_output_tokens=None, tools=None
    )
    assert not baseline.omitted
    run = env.runtime.create_agent_run(session_id=sid, user_input=current_input)
    content_token = _turn_input_content.set((sid, (attachment,)))
    run_token = _turn_run_id.set(run.run_id)
    manager_token = _turn_run_manager.set(env.runtime.agent_run_manager)
    audit_events = []
    try:
        response = loop._complete_text_with_retry(
            stage="context_answer",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_summary="image history reservation regression",
            max_output_tokens=None,
            llm_events=audit_events,
        )
    finally:
        _turn_run_manager.reset(manager_token)
        _turn_run_id.reset(run_token)
        _turn_input_content.reset(content_token)
    assert response is not None and response.content == "The image contains red pixels."
    _assert_images(env.provider.requests, [image])
    assert len(env.provider.requests) == 1
    dispatched = env.provider.requests[0]
    text = next(
        block.text
        for message in dispatched.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, LLMTextBlock)
    )
    payload = json.loads(text)
    assert payload["user_input"] == current_input
    assert payload["_prompt_budget"]["older_context_messages"] > 0
    assert len(payload["session_context_window"]["recent_messages"]) < len(history)
    assert service._count_request(dispatched, None) + (dispatched.max_output_tokens or 0) <= 56000
    _assert_no_encoded_images(
        env, [image], [event.model_dump(mode="json") for event in audit_events]
    )
