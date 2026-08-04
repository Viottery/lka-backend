from __future__ import annotations

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

    assert fake_llm.calls == 3
    assert response.answer == "LLM final answer after retry."
    assert [event.status for event in response.llm_events] == [
        "rate_limited",
        "completed",
        "completed",
    ]
    assert response.llm_events[0].status_code == 429
    assert response.llm_events[0].retry_after == "0"

    log_text = Path(response.log_path or "").read_text(encoding="utf-8")
    assert "rate_limited" in log_text
    assert "Choose at most one tool package" in log_text
    assert "loaded_mail_messages" in log_text


class _RateLimitedThenWorkingLLM:
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
    ) -> LLMResponse:
        self.calls += 1
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
        else:
            content = "LLM final answer after retry."
        return LLMResponse(
            provider="fake_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )
