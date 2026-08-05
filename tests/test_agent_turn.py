from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.agent import run_agent_turn
from app.api.routes.mail import import_mail
from app.api.routes.sessions import get_session
from app.api.schemas import AgentTurnRequest, MailImportRequest
from app.core.config import get_settings
from app.core.llm import LLMRateLimitError, LLMResponse


def test_agent_turn_expands_mail_package_and_records_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    assert "/agent/turn" in app.openapi()["paths"]
    assert "/mail/process" not in app.openapi()["paths"]

    full_body_sentinel = "FULL_BODY_SENTINEL_NTUSO_REQUIREMENTS"
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_ntuso_001",
                    "folder": "Inbox",
                    "subject": "NTUSO Audition requirements",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": (
                        "NTUSO audition requires scales and a prepared piece. "
                        f"{full_body_sentinel}"
                    ),
                },
            ],
        ),
        request,
    )

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_agent_turn_ntuso",
            user_input="帮我查询 NTUSO 的乐团考试相关要求，并给我建议",
        ),
        request,
    )

    assert response.session_id == "session_agent_turn_ntuso"
    assert response.selected_package == "mail"
    assert [event.tool_name for event in response.tool_events] == [
        "mail.search",
        "mail.load_messages",
    ]
    assert [event.action for event in response.decision_events] == [
        "select_package",
        "call_tool",
        "call_tool",
        "answer",
    ]
    assert full_body_sentinel in response.answer
    assert response.log_path is not None

    log_text = Path(response.log_path).read_text(encoding="utf-8")
    assert "## Package Catalog" in log_text
    assert "mail.search" in log_text
    assert "mail.load_messages" in log_text
    assert full_body_sentinel in log_text

    session = get_session("session_agent_turn_ntuso", request)
    assert [message.role for message in session.messages] == ["user", "agent"]
    assert session.messages[1].payload["trace_id"] == response.trace_id
    assert session.messages[1].payload["selected_package"] == "mail"


def test_agent_turn_retries_rate_limited_llm_and_logs_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)

    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_rate_limit_001",
                    "folder": "Inbox",
                    "subject": "NTUSO Audition",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": "NTUSO audition details for retry test.",
                },
            ],
        ),
        request,
    )
    fake_llm = _RateLimitedThenWorkingLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm
    app.state.runtime.agent_turn_loop.default_rate_limit_wait_seconds = 0.0

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_agent_turn_rate_limit",
            user_input="帮我查询 NTUSO 的乐团考试相关要求",
        ),
        request,
    )

    assert fake_llm.calls == 5
    assert fake_llm.max_output_tokens_seen == [8192, 8192, 8192, 8192, 8192]
    assert response.answer == "LLM final answer after retry."
    assert [event.status for event in response.llm_events] == [
        "rate_limited",
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    assert [event.stage for event in response.llm_events] == [
        "route",
        "route",
        "decision",
        "decision",
        "decision",
    ]
    assert response.llm_events[0].status_code == 429
    assert response.llm_events[0].retry_after == "0"
    assert [event.action for event in response.decision_events] == [
        "select_package",
        "call_tool",
        "call_tool",
        "answer",
    ]
    assert [event.tool_name for event in response.tool_events] == [
        "mail.search",
        "mail.load_messages",
    ]

    log_text = Path(response.log_path or "").read_text(encoding="utf-8")
    assert "rate_limited" in log_text
    assert "Choose at most one tool package" in log_text
    assert "Choose the next single action" in log_text
    assert "## Decision Events" in log_text


def test_agent_turn_maintains_recent_session_context_window(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _ContextAwareLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    first = run_agent_turn(
        AgentTurnRequest(
            session_id="session_context_window",
            user_input="第一轮问题：记住我的关注点是ICA学生签证。",
        ),
        request,
    )
    second = run_agent_turn(
        AgentTurnRequest(
            session_id="session_context_window",
            user_input="第二轮问题：继续刚才的话题。",
        ),
        request,
    )

    assert first.answer == "第一轮回答：已记录ICA学生签证关注点。"
    assert second.answer == "第二轮回答：我看到了上一轮关于ICA学生签证的近期问答。"
    assert fake_llm.route_contexts[0]["recent_messages"] == []
    second_context = fake_llm.route_contexts[1]
    assert second_context["token_budget"] == 65_536
    assert second_context["token_estimate"] > 0
    assert [message["role"] for message in second_context["recent_messages"]] == [
        "user",
        "agent",
    ]
    assert "第一轮问题" in second_context["recent_messages"][0]["content"]
    assert "第一轮回答" in second_context["recent_messages"][1]["content"]
    assert "tool_events" not in json.dumps(second_context, ensure_ascii=False)

    window = app.state.runtime.session_service.get_context_window(
        session_id="session_context_window"
    )
    assert len(window.recent_messages) == 4
    assert "第二轮回答" in window.recent_messages[-1].content

    log_text = Path(second.log_path or "").read_text(encoding="utf-8")
    assert "## Session Context Window" in log_text
    assert "第一轮问题" in log_text


def test_agent_turn_summarizes_context_window_with_llm_when_full(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _ContextSummarizingLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm
    app.state.runtime.agent_turn_loop.session_context_token_budget = 60

    first = run_agent_turn(
        AgentTurnRequest(
            session_id="session_context_summarize",
            user_input="first historical topic " + ("alpha " * 15),
        ),
        request,
    )
    second = run_agent_turn(
        AgentTurnRequest(
            session_id="session_context_summarize",
            user_input="second current topic " + ("beta " * 15),
        ),
        request,
    )

    window = app.state.runtime.session_service.get_context_window(
        session_id="session_context_summarize"
    )
    assert fake_llm.summary_inputs
    assert fake_llm.summary_inputs[0]["existing_summary"] == ""
    assert len(fake_llm.summary_inputs[0]["messages_to_summarize"]) == 2
    assert window.summary.startswith("LLM SUMMARY:")
    assert len(window.recent_messages) == 2
    assert "second current topic" in window.recent_messages[0].content
    assert "first historical topic" not in json.dumps(
        [message.model_dump(mode="json") for message in window.recent_messages],
        ensure_ascii=False,
    )

    assert not any(event.stage == "context_summarize" for event in first.llm_events)
    assert any(event.stage == "context_summarize" for event in second.llm_events)
    log_text = Path(second.log_path or "").read_text(encoding="utf-8")
    assert "context_summarize" in log_text
    assert "Session Context Compressor" in log_text


class _RateLimitedThenWorkingLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.max_output_tokens_seen: list[int | None] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls += 1
        self.max_output_tokens_seen.append(max_output_tokens)
        if self.calls == 1:
            raise LLMRateLimitError(
                status_code=429,
                message="LLM provider returned HTTP 429: retry later",
                retry_after="0",
            )
        if "Choose at most one tool package" in system_prompt:
            content = (
                '{"selected_package":"mail","reason":"test route",'
                '"search_query":"NTUSO"}'
            )
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            if not observations:
                content = json.dumps(
                    {
                        "action": "call_tool",
                        "tool_name": "mail.search",
                        "tool_input": {"query": "NTUSO", "limit": 8},
                        "reason": "Search first.",
                    }
                )
            elif observations[-1]["tool_name"] == "mail.search":
                messages = observations[-1]["result"]["output"]["messages"]
                message_ids = [message["message_id"] for message in messages[:1]]
                content = json.dumps(
                    {
                        "action": "call_tool",
                        "tool_name": "mail.load_messages",
                        "tool_input": {"message_ids": message_ids},
                        "reason": "Load the matching message.",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "action": "answer",
                        "answer": "LLM final answer after retry.",
                        "reason": "Loaded message is enough.",
                    }
                )
        else:
            content = "LLM final answer after retry."
        return LLMResponse(
            provider="fake_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _ContextAwareLLM:
    def __init__(self) -> None:
        self.route_contexts: list[dict] = []
        self.decision_calls = 0

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            payload = json.loads(user_prompt)
            self.route_contexts.append(payload["session_context_window"])
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Use mail package for context-window test.",
                    "search_query": "ICA",
                }
            )
        elif "Choose the next single action" in system_prompt:
            self.decision_calls += 1
            answer = (
                "第一轮回答：已记录ICA学生签证关注点。"
                if self.decision_calls == 1
                else "第二轮回答：我看到了上一轮关于ICA学生签证的近期问答。"
            )
            content = json.dumps(
                {
                    "action": "answer",
                    "answer": answer,
                    "reason": "Context window is enough for this test.",
                }
            )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_context_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _ContextSummarizingLLM:
    def __init__(self) -> None:
        self.summary_inputs: list[dict] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": None,
                    "reason": "No package needed for context summarize test.",
                    "search_query": "",
                }
            )
        elif "Session Context Compressor" in system_prompt:
            payload = json.loads(user_prompt)
            self.summary_inputs.append(payload)
            content = json.dumps(
                {
                    "summary": "LLM SUMMARY: prior first topic."
                }
            )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_context_summarizer",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )
