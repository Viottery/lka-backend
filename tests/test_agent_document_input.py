"""Offline document-input integration contracts; no provider/network calls."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from test_agent_image_input import _CapturingVisionClient, _images, _png
from test_file_processing import _pdf, _zip

from app.api.main import create_app
from app.core.config import get_settings
from app.core.context_driver import SideEffectLevel, ToolView
from app.core.llm.models import IMAGE_INPUT_TOKEN_RESERVATION, LLMTextBlock
from app.core.llm.registry import LLMClientRegistry
from app.core.llm.service import LLMService
from app.core.local_config import LLMClientConfig, LLMProviderConfig
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewRequest
from app.core.tools import ToolContext

MAIN_STAGES = {"route", "decision", "answer", "context_answer"}


@pytest.fixture(params=["legacy", "langgraph"])
def document_runtime(request, tmp_path, monkeypatch):
    config = tmp_path / "local.toml"
    config.write_text(
        f'[agent]\norchestrator = "{request.param}"\n'
        '[memory]\nenabled = false\nbackground_enabled = false\n'
        '[message_history]\nenabled = false\nbackground_enabled = false\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    app = create_app()
    provider = _CapturingVisionClient()
    registry = LLMClientRegistry()
    registry.register_client(provider)
    service = LLMService(config=LLMProviderConfig(default_client=provider.name, clients=[
        LLMClientConfig(name=provider.name, default_model="visual",
                        context_window_tokens=200000, supports_vision=True,
                        model_overrides={"text_only": {"context_window_tokens": 200000}}),
    ]), registry=registry)
    runtime = app.state.runtime
    runtime.agent_llm_client = service
    runtime.agent_turn_loop.llm_client = service
    client = TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False)
    yield SimpleNamespace(app=app, runtime=runtime, client=client, provider=provider,
                          service=service, data_dir=tmp_path / "data", orchestrator=request.param)
    client.close()
    runtime.stop()
    get_settings.cache_clear()


def _session(env):
    response = env.client.post("/sessions", json={"title": "Offline document test"})
    assert response.status_code == 200, response.text
    return response.json()["session"]["session_id"]


def _upload(env, sid, text, filename="证据.txt", media_type="text/plain"):
    raw = text.encode() if isinstance(text, str) else text
    response = env.client.post(f"/sessions/{sid}/attachments", content=raw,
                               headers={"Content-Type": media_type, "X-Filename": quote(filename)})
    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["filename"] == filename
    assert metadata["sha256"] == hashlib.sha256(raw).hexdigest()
    assert metadata["size_bytes"] == len(raw)
    assert "text" not in metadata
    return metadata["attachment_id"]


def _turn(env, sid, **payload):
    response = env.client.post("/agent/turn", json={"session_id": sid, **payload})
    assert response.status_code == 200, response.text
    return response.json()


def _payload(request):
    user = next(message for message in request.messages if message.role == "user")
    text = user.content if isinstance(user.content, str) else next(
        block.text for block in user.content if isinstance(block, LLMTextBlock))
    return json.loads(text)


def _main(env):
    return [request for request in env.provider.requests if request.metadata.get("stage") in MAIN_STAGES]


def _assert_sources(env, ids, text=None):
    requests = _main(env)
    assert requests
    for request in requests:
        sources = _payload(request)["input_sources"]
        assert [source["attachment_id"] for source in sources] == ids
        for source in sources:
            assert {"filename", "sha256", "processor", "text", "total_chars", "start_char",
                    "end_char", "next_start_char", "truncated", "extraction"} <= source.keys()
            assert source["start_char"] == 0
            assert source["end_char"] == len(source["text"])
            if text is not None:
                assert source["text"] == text
        assert env.service._count_request(request, None) + (request.max_output_tokens or 0) <= (
            env.service.config.clients[0].context_window_tokens)
    for request in env.provider.requests:
        if request.metadata.get("stage") not in MAIN_STAGES:
            assert not _payload(request).get("input_sources")


def _assert_public_refs(env, sid, result, source_text):
    detail = env.client.get(f"/sessions/{sid}").json()
    assert source_text not in json.dumps(result, ensure_ascii=False)
    assert source_text not in json.dumps(detail, ensure_ascii=False)
    assert base64.b64encode(source_text.encode()).decode() not in json.dumps(result)
    assert all(source_text not in message["content"] for message in detail["messages"])
    return detail


@pytest.mark.parametrize("stream", [False, True])
def test_document_only_dispatches_real_source_on_nonvision_model(document_runtime, stream):
    env = document_runtime
    sid = _session(env)
    text = "独立来源证据：合同日期为二零二七年五月十日。"
    attachment = _upload(env, sid, text)
    endpoint = "/agent/turn/stream" if stream else "/agent/turn"
    response = env.client.post(endpoint, json={"session_id": sid, "user_input": "Read the date.",
        "attachment_ids": [attachment], "llm": {"model": "text_only"}})
    assert response.status_code == 200, response.text
    if stream:
        result = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert any(frame["type"] == "run_completed" for frame in result)
        assert any(frame["type"] == "llm_delta" and frame["payload"].get("content_role") == "final_answer"
                   for frame in result)
    else:
        result = response.json()
        assert result["answer"]
    _assert_sources(env, [attachment], text)
    assert all(_images(request) == [] for request in env.provider.requests)
    detail = _assert_public_refs(env, sid, result, text)
    user = next(message for message in detail["messages"] if message["role"] == "user")
    assert user["payload"]["attachment_ids"] == [attachment]


def test_real_pdf_and_docx_extraction_reaches_main_prompts(document_runtime):
    env = document_runtime
    sid = _session(env)
    docx = _zip({
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCXIntegrationSentinel</w:t></w:r></w:p></w:body></w:document>',
    })
    for name, mime, raw, sentinel in (
        ("evidence.pdf", "application/pdf", _pdf("PDF_INTEGRATION_SENTINEL"), "PDF_INTEGRATION_SENTINEL"),
        ("evidence.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         docx, "DOCXIntegrationSentinel"),
    ):
        doc = _upload(env, sid, raw, name, mime)
        env.provider.requests.clear()
        result = _turn(env, sid, user_input="Read the source.", attachment_ids=[doc],
                       llm={"model": "text_only"})
        assert result["answer"]
        _assert_sources(env, [doc])
        for request in _main(env):
            source = _payload(request)["input_sources"][0]
            assert sentinel in source["text"]
            assert source["processor"] in {"native_pdf", "markitdown"}
            assert not _images(request)
            assert base64.b64encode(raw).decode() not in json.dumps(request.model_dump())


@pytest.mark.parametrize("mixed", [False, True])
def test_file_only_marker_and_mixed_real_image_reservation(document_runtime, mixed):
    env = document_runtime
    env.provider.route_package = None
    sid = _session(env)
    text = "Source input is ordinary text."
    doc = _upload(env, sid, text)
    ids = [doc]
    image = _png(21)
    if mixed:
        ids.append(_upload(env, sid, image, "pixels.png", "image/png"))
    result = _turn(env, sid, attachment_ids=ids)
    assert result["answer"]
    _assert_sources(env, [doc], text)
    request = _main(env)[0]
    assert _images(request) == ([image] if mixed else [])
    # Counting identical text without image blocks isolates the 32k image reserve.
    without_images = request.model_copy(deep=True)
    for message in without_images.messages:
        if isinstance(message.content, list):
            message.content = [block for block in message.content if isinstance(block, LLMTextBlock)]
    assert env.service._count_request(request, None) - env.service._count_request(without_images, None) == (
        IMAGE_INPUT_TOKEN_RESERVATION if mixed else 0)
    detail = env.client.get(f"/sessions/{sid}").json()
    assert next(message for message in detail["messages"] if message["role"] == "user")["content"] == "[File input]"


def test_document_selection_followup_reuses_and_explicit_clear_persists(document_runtime):
    env = document_runtime
    sid = _session(env)
    text = "FOLLOWUP_SOURCE_ONLY: meeting at 16:45."
    doc = _upload(env, sid, text)
    _turn(env, sid, user_input="Read.", attachment_ids=[doc])
    for selection in ({}, {"attachment_ids": None}):
        env.provider.requests.clear()
        _turn(env, sid, user_input="Read again.", **selection)
        _assert_sources(env, [doc], text)
    for selection in ({"attachment_ids": []}, {}):
        env.provider.requests.clear()
        _turn(env, sid, user_input="Continue.", **selection)
        assert _main(env)
        assert all(not _payload(request).get("input_sources") for request in _main(env))
        assert all(text not in json.dumps(request.model_dump(), ensure_ascii=False) for request in env.provider.requests)


def test_invalid_document_and_mixed_selection_rejects_before_sse(document_runtime):
    env = document_runtime
    sid, other = _session(env), _session(env)
    doc = _upload(env, sid, "VALID_SOURCE")
    image = _upload(env, sid, _png(23), "pixels.png", "image/png")
    before = len(env.runtime.agent_run_manager._runs)
    for payload, status in (
        ({"session_id": other, "attachment_ids": [doc]}, 404),
        ({"session_id": sid, "attachment_ids": [doc, image], "llm": {"model": "text_only"}}, 422),
        ({"session_id": sid, "attachment_ids": [doc] * 5}, 422),
    ):
        response = env.client.post("/agent/turn/stream", json={"user_input": "Read.", **payload})
        assert response.status_code == status, response.text
        assert "text/event-stream" not in response.headers.get("content-type", "")
        assert len(env.runtime.agent_run_manager._runs) == before
    for filename, mime, raw in (("run.exe", "application/octet-stream", b"MZ"),
                                ("bad.pdf", "application/pdf", b"not PDF")):
        response = env.client.post(f"/sessions/{sid}/attachments", content=raw,
            headers={"X-Filename": filename, "Content-Type": mime})
        assert response.status_code in {415, 422}, response.text
    source = next(env.data_dir.rglob(doc + ".document"))
    source.write_bytes(b"CORRUPTED_SOURCE")
    response = env.client.post("/agent/turn/stream", json={"session_id": sid,
        "user_input": "Read.", "attachment_ids": [doc]})
    assert response.status_code == 404, response.text
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert len(env.runtime.agent_run_manager._runs) == before
    assert env.provider.requests == []


def _reviewed_execute(env, sid, run_id, name, data, invocation, tool_view=None):
    manager = env.runtime.agent_run_manager
    review = manager.create_safety_review(SafetyReviewRequest(
        review_id=f"review-{invocation}", run_id=run_id, session_id=sid, trace_id="offline",
        invocation_id=invocation, tool_name=name, tool_input=data, read_only=True,
        mode=SafetyReviewMode.SKIP, reason="Explicit attachment read in offline test",
        created_at=datetime.now(UTC).isoformat()))
    manager.decide_safety_review(review_id=review.review_id, decision=SafetyReviewDecision.APPROVE,
                                decided_by="test", reason="Read selected attachment")
    result = env.runtime.tool_executor.execute(invocation_id=invocation, tool_name=name,
        tool_input=data, context=ToolContext(session_id=sid, run_id=run_id,
                           safety_review_approved=True, safety_review_id=review.review_id, tool_view=tool_view))
    assert manager.get_safety_review(review.review_id) is not None
    return result


def test_long_document_preview_continues_via_reviewed_schema_validated_tools(document_runtime):
    env = document_runtime
    sid = _session(env)
    tail = "TAIL_EVIDENCE: approval dated 2029-03-04."
    text = "p" * 14000 + tail
    doc = _upload(env, sid, text)
    turn = _turn(env, sid, user_input="Read the full document.", attachment_ids=[doc])
    for request in _main(env):
        source = _payload(request)["input_sources"][0]
        assert source["text"] == text[:12000]
        assert source["total_chars"] == len(text)
        assert source["truncated"] is True
        assert source["next_start_char"] == 12000
        assert tail not in json.dumps(request.model_dump())
        assert "attachments.read" in json.dumps(source)
        assert "attachments.find" in json.dumps(source)
    assert turn["answer"]
    read_run = env.runtime.create_agent_run(session_id=sid, user_input="Continue source read.")
    unreviewed = env.runtime.tool_executor.execute(invocation_id="missing-review",
        tool_name="attachments.read", tool_input={"attachment_id": doc, "start_char": 12000},
        context=ToolContext(session_id=sid, run_id=read_run.run_id))
    assert unreviewed.status == "rejected"
    assert tail not in json.dumps(unreviewed.model_dump())
    read = _reviewed_execute(env, sid, read_run.run_id, "attachments.read",
        {"attachment_id": doc, "start_char": 12000, "max_chars": 4000}, "read-tail")
    assert read.status == "completed", read.model_dump()
    assert read.output["text"] == text[12000:]
    assert read.output["next_start_char"] is None
    found = _reviewed_execute(env, sid, read_run.run_id, "attachments.find",
        {"attachment_id": doc, "query": "TAIL_EVIDENCE"}, "find-tail")
    assert found.status == "completed", found.model_dump()
    assert found.output["matches"][0]["start_char"] == 14000
    assert tail in found.output["matches"][0]["text"]
    invalid = _reviewed_execute(env, sid, read_run.run_id, "attachments.read",
        {"attachment_id": doc, "start_char": "bad"}, "invalid-offset")
    assert invalid.status == "rejected"
    child_view = ToolView(snapshot_id="bounded-child", child_run_id=read_run.run_id,
        allowed_packages=("attachments",), allowed_tools=("attachments.read",),
        full_data_authority=True, side_effect_level=SideEffectLevel.READ)
    child = _reviewed_execute(env, sid, read_run.run_id, "attachments.read",
        {"attachment_id": doc}, "child-read", tool_view=child_view)
    assert child.status in {"failed", "rejected"}
    assert "child" in (child.error or "").lower()
    assert tail not in json.dumps(child.model_dump())


def test_child_main_prompt_does_not_inherit_parent_input_sources(document_runtime):
    from app.core.agent_turn import _turn_input_content, _turn_run_id, _turn_run_manager

    env = document_runtime
    sid = _session(env)
    text = "PARENT_PRIVATE_SOURCE_ONLY"
    doc = _upload(env, sid, text)
    parent = env.runtime.create_agent_run(session_id=sid, user_input="Parent task.")
    child = env.runtime.agent_run_manager.create_run(session_id=sid, user_input="Bounded child task.",
                                                    parent_run_id=parent.run_id)
    content_token = _turn_input_content.set((sid, (doc,)))
    run_token = _turn_run_id.set(child.run_id)
    manager_token = _turn_run_manager.set(env.runtime.agent_run_manager)
    try:
        response = env.runtime.agent_turn_loop._complete_text_with_retry(
            stage="context_answer", system_prompt="Answer the bounded child task.",
            user_prompt=json.dumps({"user_input": child.user_input}),
            prompt_summary="offline child input isolation", max_output_tokens=None, llm_events=[])
        assert response is not None
    finally:
        _turn_run_manager.reset(manager_token)
        _turn_run_id.reset(run_token)
        _turn_input_content.reset(content_token)
    assert len(env.provider.requests) == 1
    request = env.provider.requests[0]
    assert not _payload(request).get("input_sources")
    assert text not in json.dumps(request.model_dump())
    assert not _images(request)


def test_small_model_context_never_dispatches_oversized_source_preview(document_runtime):
    env = document_runtime
    env.provider.route_package = None
    env.service.config.clients[0].context_window_tokens = 20000
    env.service.config.clients[0].model_overrides["text_only"].context_window_tokens = 20000
    env.service.config.clients[0].output_reserve_tokens = 512
    env.service.config.clients[0].model_overrides["text_only"].output_reserve_tokens = 512
    sid = _session(env)
    doc = _upload(env, sid, "LONG_CONTEXT_SOURCE " * 1600)
    response = env.client.post("/agent/turn", json={"session_id": sid,
        "user_input": "Read.", "attachment_ids": [doc], "llm": {"model": "text_only"}})
    assert response.status_code == 200, response.text
    for request in env.provider.requests:
        assert env.service._count_request(request, None) + (request.max_output_tokens or 0) <= 20000
        if request.metadata.get("stage") in MAIN_STAGES:
            source = _payload(request)["input_sources"][0]
            assert 0 < len(source["text"]) < 12000
            assert source["truncated"] is True
            assert "attachments.read" in json.dumps(source.get("read_more"))
            assert source["next_start_char"] == source["end_char"]
    assert response.json()["answer"]
    assert _main(env), "A successful source answer must dispatch a bounded source prompt."


@pytest.mark.parametrize("corrupt", [False, True])
def test_document_refs_durable_resume_revalidates_source(document_runtime, corrupt):
    env = document_runtime
    if env.orchestrator != "langgraph":
        pytest.skip("Durable checkpoint resume is implemented by langgraph.")
    sid = _session(env)
    text = "DURABLE_DOCUMENT_SOURCE: persisted independently of graph state."
    doc = _upload(env, sid, text)
    runner = env.runtime.agent_turn_runner
    runner.interrupt_after = ["initialize_run"]
    run = env.runtime.create_agent_run(session_id=sid, user_input="Read after restart.")
    asyncio.run(runner._invoke(run_id=run.run_id, session_id=sid,
        user_input=run.user_input, attachment_ids=[doc]))
    paused = runner.get_state(run.run_id)
    assert paused.values["request"]["attachment_ids"] == [doc]
    assert text not in json.dumps(paused.values, ensure_ascii=False, default=str)
    assert not env.provider.requests
    env.runtime.stop()
    get_settings.cache_clear()
    restored = create_app().state.runtime
    restored.agent_llm_client = env.service
    restored.agent_turn_loop.llm_client = env.service
    try:
        source = next(env.data_dir.rglob(doc + ".document"))
        if corrupt:
            source.write_bytes(b"CORRUPTED_DURABLE_SOURCE")
            try:
                rejected = restored.resume_agent_run(run.run_id)
            except (ValueError, KeyError):
                rejected = None
            assert not env.provider.requests
            assert rejected is None or not rejected.answer
        else:
            result = restored.resume_agent_run(run.run_id)
            assert result.answer
            _assert_sources(env, [doc], text)
    finally:
        restored.stop()
