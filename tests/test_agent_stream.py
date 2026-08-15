from __future__ import annotations

import json
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.agent import stream_agent_turn
from app.api.schemas import AgentTurnRequest
from app.core.config import get_settings
from app.domains.mail import MailAccountInput, MailMessageInput


async def _is_never_disconnected() -> bool:
    return False


def test_agent_turn_stream_emits_run_tool_and_final_events(tmp_path, monkeypatch):
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


def test_agent_turn_stream_endpoint_is_registered_without_changing_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    openapi = app.openapi()["paths"]

    assert "/agent/turn" in openapi
    assert "/agent/turn/stream" in openapi


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
