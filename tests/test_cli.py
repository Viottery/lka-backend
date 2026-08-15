from __future__ import annotations

import json
from urllib.request import Request

from debug_frontend import cli


def test_cli_health_uses_backend_base_url(monkeypatch, capsys):
    calls = []

    def fake_urlopen(request: Request, timeout: float):
        calls.append((request, timeout))
        return _FakeResponse(
            body={
                "status": "ok",
                "version": "0.1.0",
                "service": "local-knowledge-agent-os",
            }
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = cli.main(["--base-url", "http://backend.local", "health"])

    assert result == 0
    assert calls[0][0].full_url == "http://backend.local/health"
    assert calls[0][1] == 60.0
    assert capsys.readouterr().out == "ok local-knowledge-agent-os 0.1.0\n"


def test_cli_ask_stream_prints_deltas_and_collapsed_agent_events(monkeypatch, capsys):
    calls = []

    def fake_urlopen(request: Request, timeout: float):
        calls.append((request, timeout))
        return _FakeResponse(lines=_agent_stream_lines())

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = cli.main(
        [
            "--base-url",
            "http://backend.local",
            "ask",
            "--session-id",
            "session_cli",
            "查一下 NTUSO audition",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(calls[0][0].data.decode("utf-8"))

    assert result == 0
    assert calls[0][0].full_url == "http://backend.local/agent/turn/stream"
    assert calls[0][0].headers["Accept"] == "text/event-stream"
    assert payload == {
        "session_id": "session_cli",
        "user_input": "查一下 NTUSO audition",
        "llm": {"response_mode": "stream"},
    }
    assert captured.out == "流式回答\n"
    assert '{"selected_package":"mail"}' not in captured.out
    assert "[agent] streaming route_decision" in captured.err
    assert "[agent] Selected `mail` package." in captured.err
    assert "[agent] 正在检索相关邮件。" in captured.err
    assert "[agent] tool_started: mail.search" in captured.err
    assert "[agent] tool_completed: mail.search" in captured.err
    assert "[agent] `mail.search` completed with 1 matched messages." in captured.err
    assert "run started" not in captured.err
    assert "run completed" not in captured.err


def test_cli_ask_stream_can_hide_agent_events(monkeypatch, capsys):
    monkeypatch.setattr(cli, "urlopen", lambda request, timeout: _FakeResponse(lines=_agent_stream_lines()))

    result = cli.main(
        [
            "ask",
            "--agent-events",
            "hidden",
            "查一下 NTUSO audition",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "流式回答\n"
    assert '{"selected_package":"mail"}' not in captured.out
    assert captured.err == ""


def test_cli_ask_stream_can_expand_agent_events(monkeypatch, capsys):
    monkeypatch.setattr(cli, "urlopen", lambda request, timeout: _FakeResponse(lines=_agent_stream_lines()))

    result = cli.main(
        [
            "ask",
            "--agent-events",
            "expanded",
            "查一下 NTUSO audition",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "流式回答\n"
    assert '{"selected_package":"mail"}' not in captured.out
    assert "[agent:2] llm_delta stage=run " in captured.err
    assert '"display_target": "agent_process"' in captured.err
    assert "[agent:1] run_started stage=run Agent run started." in captured.err
    assert '"session_id": "session_cli"' in captured.err


def test_cli_stream_supplements_missing_suffix_from_final_answer(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            lines=_agent_stream_lines(final_answer="流式回答完整")
        ),
    )

    result = cli.main(["ask", "--agent-events", "hidden", "查一下 NTUSO audition"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "流式回答完整\n"
    assert captured.err == ""


def test_cli_iter_sse_handles_multiline_data():
    response = _FakeResponse(
        lines=[
            b"id: one\n",
            b"event: note\n",
            b'data: {"message":\n',
            b'data: "hello"}\n',
            b"\n",
        ]
    )

    assert list(cli._iter_sse(response)) == [
        {"id": "one", "event": "note", "data": {"message": "hello"}}
    ]


def _agent_stream_lines(*, final_answer: str = "流式回答") -> list[bytes]:
    frames = [
        _frame(
            sequence=1,
            event_type="run_started",
            stream_part="lifecycle",
            message="Agent run started.",
            payload={"session_id": "session_cli", "trace_id": "trace_cli"},
        ),
        _frame(
            sequence=2,
            event_type="llm_delta",
            stream_part="llm_delta",
            message='{"selected_package":"mail"}',
            payload={
                "content_role": "route_decision",
                "display_target": "agent_process",
                "delta": '{"selected_package":"mail"}',
                "content_snapshot": '{"selected_package":"mail"}',
            },
        ),
        _frame(
            sequence=3,
            event_type="package_selected",
            stream_part="progress",
            message="Selected `mail` package.",
            payload={"package_name": "mail", "status": "completed"},
        ),
        _frame(
            sequence=4,
            event_type="assistant_message",
            stream_part="progress",
            message="正在检索相关邮件。",
            payload={"status": "completed"},
        ),
        _frame(
            sequence=5,
            event_type="tool_started",
            stream_part="tool_result",
            message="Tool started.",
            payload={"tool_name": "mail.search"},
        ),
        _frame(
            sequence=6,
            event_type="tool_completed",
            stream_part="tool_result",
            message="Tool completed.",
            payload={"tool_name": "mail.search"},
        ),
        _frame(
            sequence=7,
            event_type="tool_feedback",
            stream_part="progress",
            message="`mail.search` completed with 1 matched messages.",
            payload={"tool_name": "mail.search"},
        ),
        _frame(
            sequence=8,
            event_type="llm_delta",
            stream_part="llm_delta",
            message="流式",
            payload={
                "content_role": "final_answer",
                "display_target": "assistant_answer",
                "delta": "流式",
                "content_snapshot": "流式",
            },
        ),
        _frame(
            sequence=9,
            event_type="llm_delta",
            stream_part="llm_delta",
            message="回答",
            payload={
                "content_role": "final_answer",
                "display_target": "assistant_answer",
                "delta": "回答",
                "content_snapshot": "流式回答",
            },
        ),
        _frame(
            sequence=10,
            event_type="final_answer",
            stream_part="final_answer",
            message=final_answer,
        ),
        _frame(
            sequence=11,
            event_type="run_completed",
            stream_part="lifecycle",
            message="Agent run completed.",
        ),
    ]
    return "".join(frames).encode("utf-8").splitlines(keepends=True)


def _frame(
    *,
    sequence: int,
    event_type: str,
    stream_part: str,
    message: str,
    payload: dict | None = None,
) -> str:
    data = {
        "event_id": f"agent_run_cli_event_{sequence:06d}",
        "run_id": "agent_run_cli",
        "sequence": sequence,
        "type": event_type,
        "stage": "run",
        "message": message,
        "payload": payload or {},
        "created_at": "2026-08-15T00:00:00+00:00",
        "stream_part": stream_part,
    }
    body = json.dumps(data, ensure_ascii=False)
    return f"id: agent_run_cli:{sequence}\nevent: {event_type}\ndata: {body}\n\n"


class _FakeResponse:
    def __init__(
        self,
        *,
        body: dict | None = None,
        lines: list[bytes] | None = None,
    ) -> None:
        self._body = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        self._lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> bytes:
        return self._body
