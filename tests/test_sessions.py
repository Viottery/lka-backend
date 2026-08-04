from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.mail import import_mail, process_mail
from app.api.routes.sessions import (
    append_session_message,
    create_session,
    get_session,
    list_sessions,
)
from app.api.schemas import MailImportRequest, MailProcessRequest
from app.api.schemas import SessionAppendMessageRequest, SessionCreateRequest
from app.core.config import get_settings


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

    processed = process_mail(
        MailProcessRequest(session_id=first_session_id, query="coliwoo notice", limit=10),
        request,
    )

    assert processed.processed_messages == 1
    assert processed.matters_created == 1

    first_detail = get_session(first_session_id, request)
    second_detail = get_session(second_session_id, request)

    first_messages = first_detail.messages
    second_messages = second_detail.messages
    assert [message.role for message in first_messages] == ["user", "user"]
    assert [message.role for message in second_messages] == ["user"]

    listed = list_sessions(request, limit=50)

    listed_ids = {session.session_id for session in listed.sessions}
    assert {first_session_id, second_session_id}.issubset(listed_ids)

    conn = sqlite3.connect(app.state.runtime.db_path)
    try:
        run_session_id = conn.execute(
            "SELECT session_id FROM mail_processing_runs WHERE run_id = ?",
            (processed.run_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert run_session_id == first_session_id
