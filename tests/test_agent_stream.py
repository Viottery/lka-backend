from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.api.routes.agent import _sse_event_frame, stream_agent_turn
from app.api.schemas import AgentTurnRequest
from app.core.agent_runs import AgentRunEvent
from app.core.config import get_settings
from app.core.llm import LLMResponse, LLMStreamEvent
from app.domains.mail import MailAccountInput, MailMessageInput


async def _is_never_disconnected() -> bool:
    return False


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_agent_turn_stream_emits_run_tool_and_final_events(
    tmp_path,
    monkeypatch,
    orchestrator,
):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        f'[agent]\norchestrator = "{orchestrator}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()

    app = create_app()
    app.state.runtime.import_mail(
        account=MailAccountInput(
            provider="local_json",
            email_address="user@example.com",
        ),
        messages=[
            MailMessageInput(
                external_id="agent_stream_ntuso_001",
                folder="Inbox",
                subject="NTUSO Audition stream",
                sender="ntuso@example.com",
                to=["user@example.com"],
                received_at="2026-08-05T09:30:00Z",
                body_text="NTUSO stream endpoint sentinel.",
            )
        ],
    )
    app.state.runtime.agent_turn_loop.llm_client = _StreamingAnswerLLM()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)

    response = _run_async(
        stream_agent_turn(
            AgentTurnRequest(
                session_id="session_agent_stream_ntuso",
                user_input="帮我查询 NTUSO 的乐团考试相关要求",
            ),
            request,
        )
    )
    assert response.media_type == "text/event-stream"
    text = _run_async(_consume_stream_response(response))

    frames = _parse_sse(text)
    event_types = [frame["event"] for frame in frames]

    assert event_types[0] == "run_started"
    assert "package_selected" in event_types
    assert "tool_started" in event_types
    assert "tool_completed" in event_types
    assert "final_answer" in event_types
    assert event_types[-1] == "run_completed"

    run_ids = {frame["data"]["run_id"] for frame in frames}
    assert len(run_ids) == 1
    run_id = run_ids.pop()
    sequences = [frame["data"]["sequence"] for frame in frames]
    assert sequences == list(range(1, len(sequences) + 1))
    assert all(frame["id"] == f"{run_id}:{frame['data']['sequence']}" for frame in frames)

    run = app.state.runtime.agent_run_manager.get_run(run_id)
    assert run is not None
    assert run.status == "completed"
    assert run.session_id == "session_agent_stream_ntuso"
    assert run.result_snapshot["selected_package"] == "mail"


def test_sse_event_frame_marks_safety_review_events():
    event = AgentRunEvent(
        event_id="evt_safety_review_required",
        run_id="run_safety_review",
        sequence=3,
        type="safety_review_required",
        stage="safety_review",
        message="Safety review required.",
        payload={"review": {"review_id": "review_001"}},
        created_at="2026-08-22T00:00:00Z",
    )

    [frame] = _parse_sse(_sse_event_frame(event))

    assert frame["event"] == "safety_review_required"
    assert frame["data"]["stream_part"] == "safety_review"


def test_agent_turn_stream_emits_provider_token_delta_events(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    app.state.runtime.import_mail(
        account=MailAccountInput(
            provider="local_json",
            email_address="user@example.com",
        ),
        messages=[
            MailMessageInput(
                external_id="agent_stream_delta_001",
                folder="Inbox",
                subject="NTUSO Delta stream",
                sender="ntuso@example.com",
                to=["user@example.com"],
                received_at="2026-08-05T09:30:00Z",
                body_text="Token delta stream source mail.",
            )
        ],
    )
    app.state.runtime.agent_turn_loop.llm_client = _StreamingAnswerLLM()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)

    response = _run_async(
        stream_agent_turn(
            AgentTurnRequest(
                session_id="session_agent_stream_delta",
                user_input="帮我查询 NTUSO 的乐团考试相关要求",
                llm={"response_mode": "stream"},
            ),
            request,
        )
    )
    text = _run_async(_consume_stream_response(response))
    frames = _parse_sse(text)
    event_types = [frame["event"] for frame in frames]

    assert "tool_completed" in event_types
    assert "llm_delta" in event_types
    assert event_types[-1] == "run_completed"
    assert {frame["data"]["stream_part"] for frame in frames}.issuperset(
        {"lifecycle", "tool_result", "llm_delta", "llm_audit", "final_answer"}
    )

    delta_frames = [frame for frame in frames if frame["event"] == "llm_delta"]
    answer_delta_frames = [
        frame
        for frame in delta_frames
        if frame["data"]["payload"]["content_role"] == "final_answer"
    ]
    assert [frame["data"]["payload"]["delta"] for frame in answer_delta_frames] == [
        "流式",
        "回答",
    ]
    assert all(
        frame["data"]["payload"]["display_target"] == "assistant_answer"
        for frame in answer_delta_frames
    )
    assert answer_delta_frames[-1]["data"]["payload"]["content_snapshot"] == "流式回答"
    assert {frame["data"]["payload"]["content_role"] for frame in delta_frames}.issuperset(
        {"route_decision", "agent_decision", "final_answer"}
    )
    assert all(
        frame["data"]["payload"]["display_target"] == "agent_process"
        for frame in delta_frames
        if frame["data"]["payload"]["content_role"] != "final_answer"
    )

    final_answer = next(frame for frame in frames if frame["event"] == "final_answer")
    assert final_answer["data"]["stream_part"] == "final_answer"
    assert "流式回答" in final_answer["data"]["message"]
    first_answer_delta_index = frames.index(answer_delta_frames[0])
    final_answer_index = frames.index(final_answer)
    assert first_answer_delta_index < final_answer_index


def test_agent_turn_stream_defaults_llm_options_without_response_mode_to_stream(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    app.state.runtime.import_mail(
        account=MailAccountInput(
            provider="local_json",
            email_address="user@example.com",
        ),
        messages=[
            MailMessageInput(
                external_id="agent_stream_llm_options_001",
                folder="Inbox",
                subject="NTUSO Delta stream with model override",
                sender="ntuso@example.com",
                to=["user@example.com"],
                received_at="2026-08-05T09:30:00Z",
                body_text="Token delta stream source mail with model override.",
            )
        ],
    )
    app.state.runtime.agent_turn_loop.llm_client = _StreamingAnswerLLM()
    request = SimpleNamespace(app=app, is_disconnected=_is_never_disconnected)

    response = _run_async(
        stream_agent_turn(
            AgentTurnRequest(
                session_id="session_agent_stream_llm_options",
                user_input="帮我查询 NTUSO 的乐团考试相关要求",
                llm={"model": "streaming-model"},
            ),
            request,
        )
    )
    frames = _parse_sse(_run_async(_consume_stream_response(response)))

    answer_delta_frames = [
        frame
        for frame in frames
        if frame["event"] == "llm_delta"
        and frame["data"]["payload"]["display_target"] == "assistant_answer"
    ]
    assert [frame["data"]["payload"]["delta"] for frame in answer_delta_frames] == [
        "流式",
        "回答",
    ]
    final_answer = next(frame for frame in frames if frame["event"] == "final_answer")
    assert frames.index(answer_delta_frames[0]) < frames.index(final_answer)


def test_agent_turn_stream_endpoint_is_registered_without_changing_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    openapi = app.openapi()["paths"]

    assert "/agent/turn" in openapi
    assert "/agent/turn/stream" in openapi


def test_final_answer_sse_frame_uses_full_answer_from_metadata():
    answer = "完整答案" * 220
    event = AgentRunEvent(
        event_id="agent_run_long_event_000001",
        run_id="agent_run_long",
        sequence=1,
        type="final_answer",
        stage="answer",
        message=answer[:497] + "...",
        payload={"metadata": {"answer": answer}},
        created_at="2026-08-15T00:00:00+00:00",
    )

    frame = _parse_sse(_sse_event_frame(event))[0]

    assert frame["event"] == "final_answer"
    assert frame["data"]["message"] == answer
    assert not frame["data"]["message"].endswith("...")
    assert frame["data"]["stream_part"] == "final_answer"


def _parse_sse(text: str) -> list[dict]:
    frames = []
    for raw_frame in text.strip().split("\n\n"):
        if not raw_frame.strip():
            continue
        fields = {}
        for line in raw_frame.splitlines():
            name, value = line.split(": ", 1)
            fields[name] = value
        frames.append(
            {
                "id": fields["id"],
                "event": fields["event"],
                "data": json.loads(fields["data"]),
            }
        )
    return frames


async def _consume_stream_response(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return "".join(chunks)


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)


class _StreamingAnswerLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        client_name: str | None = None,
        model: str | None = None,
        response_mode=None,
        require_json: bool = False,
        metadata: dict | None = None,
    ) -> LLMResponse:
        self.calls += 1
        content = self._content_for_prompts(system_prompt=system_prompt, user_prompt=user_prompt)
        return LLMResponse(
            provider="streaming_fake",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )

    async def stream(self, request):
        stage = str(request.metadata.get("stage") or "answer")
        messages = {message.role: message.content for message in request.messages}
        content = self._content_for_prompts(
            system_prompt=messages.get("system", ""),
            user_prompt=messages.get("user", ""),
        )
        yield LLMStreamEvent(
            event_type="llm_started",
            stage=stage,
            client_name=request.client_name or "streaming_fake",
            provider="streaming_fake",
            model=request.model or "streaming-model",
        )
        snapshot = ""
        deltas = ["流式", "回答"] if stage == "answer" else [content]
        for delta in deltas:
            snapshot += delta
            yield LLMStreamEvent(
                event_type="llm_delta",
                stage=stage,
                client_name=request.client_name or "streaming_fake",
                provider="streaming_fake",
                model=request.model or "streaming-model",
                delta=delta,
                content_snapshot=snapshot,
            )
        yield LLMStreamEvent(
            event_type="llm_completed",
            stage=stage,
            client_name=request.client_name or "streaming_fake",
            provider="streaming_fake",
            model=request.model or "streaming-model",
            content_snapshot=snapshot,
        )

    def _content_for_prompts(self, *, system_prompt: str, user_prompt: str) -> str:
        if "Choose at most one tool package" in system_prompt:
            return (
                '{"selected_package":"mail","reason":"test route",'
                '"search_query":"NTUSO"}'
            )
        if "Tool Result Checker" in system_prompt:
            return '{"status":"accepted","message":"ok","remaining_work":""}'
        if "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            if not observations:
                return json.dumps(
                    {
                        "operation": {
                            "type": "tool_call",
                            "tool_name": "mail.search",
                            "tool_input": {"query": "NTUSO", "limit": 8},
                            "reason": "Search mail.",
                        },
                        "assistant_message": "检索邮件。",
                    }
                )
            if observations[-1]["tool_name"] == "mail.search":
                message_ids = [
                    message["message_id"]
                    for message in observations[-1]["result"]["output"]["messages"][:1]
                ]
                return json.dumps(
                    {
                        "operation": {
                            "type": "tool_call",
                            "tool_name": "mail.load_messages",
                            "tool_input": {"message_ids": message_ids},
                            "reason": "Load message.",
                        },
                        "assistant_message": "读取邮件。",
                    }
                )
            return json.dumps(
                {
                    "operation": {
                        "type": "final_answer",
                        "final_answer": "decision text must not stream as the answer",
                        "reason": "Loaded mail evidence is sufficient.",
                    },
                    "assistant_message": "准备生成最终回答。",
                }
            )
        return "流式回答"
