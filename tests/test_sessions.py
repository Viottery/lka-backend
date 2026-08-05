from __future__ import annotations

from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.mail import import_mail
from app.api.routes.sessions import (
    append_session_message,
    create_session,
    get_session,
    list_sessions,
)
from app.api.schemas import MailImportRequest
from app.api.schemas import SessionAppendMessageRequest, SessionCreateRequest
from app.core.config import get_settings
from app.core.tools import ToolContext


def test_parallel_sessions_and_mail_tool_access_are_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)

    first = create_session(
        SessionCreateRequest(
            title="Coliwoo follow-up",
            initial_message="Check Coliwoo notices.",
        ),
        request,
    )
    second = create_session(
        SessionCreateRequest(
            title="Visa follow-up",
            initial_message="Track visa documents.",
        ),
        request,
    )

    first_session_id = first.session.session_id
    second_session_id = second.session.session_id
    assert first_session_id != second_session_id

    appended = append_session_message(
        first_session_id,
        SessionAppendMessageRequest(
            role="user",
            content="Only use the Coliwoo thread for this session.",
        ),
        request,
    )

    assert appended.session_id == first_session_id

    imported = import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "session_mail_001",
                    "folder": "Inbox",
                    "subject": "Coliwoo admin notice",
                    "sender": "hello@coliwoo.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-03T09:30:00Z",
                    "body_text": "Coliwoo moved the parcel collection point to Block B.",
                }
            ],
        ),
        request,
    )
    assert imported.imported_messages == 1

    tool_result = app.state.runtime.tool_executor.execute(
        invocation_id="session_mail_search",
        tool_name="mail.search",
        tool_input={"query": "coliwoo notice", "limit": 10},
        context=ToolContext(
            session_id=first_session_id,
            trace_id="trace_session_mail_search",
            context_id="ctx_session_mail_search",
        ),
    )

    assert tool_result.status == "completed"
    assert len(tool_result.output["messages"]) == 1

    first_detail = get_session(first_session_id, request)
    second_detail = get_session(second_session_id, request)

    first_messages = first_detail.messages
    second_messages = second_detail.messages
    assert [message.role for message in first_messages] == ["user", "user"]
    assert [message.role for message in second_messages] == ["user"]

    listed = list_sessions(request, limit=50)

    listed_ids = {session.session_id for session in listed.sessions}
    assert {first_session_id, second_session_id}.issubset(listed_ids)


def test_session_context_window_summarizes_old_core_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    service = app.state.runtime.session_service

    service.ensure_session(session_id="session_small_window")
    window = service.record_context_exchange(
        session_id="session_small_window",
        user_input="first user message " + ("alpha " * 10),
        agent_answer="first agent answer " + ("beta " * 10),
        trace_id="trace_first",
        token_budget=75,
    )
    window = service.record_context_exchange(
        session_id="session_small_window",
        user_input="second user message " + ("gamma " * 10),
        agent_answer="second agent answer " + ("delta " * 10),
        trace_id="trace_second",
        token_budget=75,
    )

    assert window.token_budget == 75
    assert window.token_estimate <= 75
    assert "alpha" in window.summary
    assert window.core_messages
    assert any("second" in message.content for message in window.core_messages)
