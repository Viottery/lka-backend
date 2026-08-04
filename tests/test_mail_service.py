from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.mail import import_mail, list_mail_matters, process_mail, search_mail
from app.api.schemas import MailImportRequest, MailProcessRequest
from app.core.config import get_settings
from app.core.llm import LLMRateLimitError, LLMResponse


class FakeMailLLMClient:
    def __init__(self) -> None:
        self.user_prompt = ""
        self.max_output_tokens = -1

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        self.user_prompt = user_prompt
        self.max_output_tokens = max_output_tokens
        message_id = user_prompt.split("message_id: ", 1)[1].splitlines()[0]
        return LLMResponse(
            provider="fake_llm",
            status="completed",
            content=(
                '{"matters":[{"title":"LLM visa matter","summary":"Full body was read.",'
                f'"status":"open","priority":"high","source_message_ids":["{message_id}"]'
                "}]}"
            ),
            prompt_summary=prompt_summary,
        )


class RateLimitedMailLLMClient:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        raise LLMRateLimitError(
            status_code=429,
            message="LLM provider returned HTTP 429: too many requests",
            retry_after="30",
        )


def test_mail_import_search_process_and_matters(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    assert "/mail/import" in app.openapi()["paths"]
    assert "/mail/search" in app.openapi()["paths"]
    assert "/mail/process" in app.openapi()["paths"]
    assert "/mail/matters" in app.openapi()["paths"]

    imported = import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
                "display_name": "User",
            },
            messages=[
                {
                    "external_id": "msg_001",
                    "folder": "Inbox",
                    "subject": "Urgent visa document reminder",
                    "sender": "admin@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-03T09:30:00Z",
                    "body_text": "Please submit the missing document by Friday.",
                    "attachments": [
                        {
                            "external_id": "att_001",
                            "name": "checklist.pdf",
                            "content_type": "application/pdf",
                            "size": 12345,
                        }
                    ],
                },
                {
                    "external_id": "msg_002",
                    "folder": "Inbox",
                    "subject": "Campus newsletter",
                    "sender": "news@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-02T09:30:00Z",
                    "body_text": "This week has several campus events.",
                },
            ],
        ),
        request,
    )

    assert imported.imported_messages == 2
    assert imported.imported_attachments == 1
    conn = sqlite3.connect(app.state.runtime.db_path)
    try:
        chunk_count = conn.execute("SELECT COUNT(*) FROM mail_chunks").fetchone()[0]
    finally:
        conn.close()
    assert chunk_count == 2

    search_result = search_mail(request, q="visa document", limit=10)

    assert len(search_result.messages) == 1
    assert search_result.messages[0].subject == "Urgent visa document reminder"
    assert "document" in search_result.messages[0].snippet

    process_result = process_mail(MailProcessRequest(query="visa document", limit=10), request)

    assert process_result.status == "completed"
    assert process_result.processed_messages == 1
    assert process_result.matters_created == 1
    assert process_result.log_path is not None
    conn = sqlite3.connect(app.state.runtime.db_path)
    try:
        provider = conn.execute(
            "SELECT provider FROM mail_processing_runs WHERE run_id = ?",
            (process_result.run_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert provider == "agent_local_heuristic"
    log_text = (tmp_path / "data" / "agent_logs" / f"{process_result.run_id}.md").read_text(
        encoding="utf-8"
    )
    assert "Agent Run Log" in log_text
    assert "mail.search" in log_text
    assert "mail.load_messages" in log_text
    assert "mail.persist_matters" in log_text

    matters = list_mail_matters(request, limit=10)

    assert len(matters.matters) == 1
    assert matters.matters[0].title == "Urgent visa document reminder"
    assert matters.matters[0].priority == "high"


def test_mail_process_sends_full_message_body_to_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    fake_llm = FakeMailLLMClient()
    app.state.runtime.mail_agent_loop.llm_client = fake_llm
    request = SimpleNamespace(app=app)
    long_tail = "FULL_BODY_SENTINEL_" + ("x" * 400)

    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "msg_llm_001",
                    "folder": "Inbox",
                    "subject": "Visa document reminder",
                    "sender": "admin@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-03T09:30:00Z",
                    "body_text": f"Please submit the visa document by Friday. {long_tail}",
                },
            ],
        ),
        request,
    )

    process_result = process_mail(MailProcessRequest(query="visa document", limit=10), request)

    assert process_result.status == "completed"
    assert process_result.processed_messages == 1
    assert process_result.matters_created == 1
    assert process_result.log_path is not None
    conn = sqlite3.connect(app.state.runtime.db_path)
    try:
        provider = conn.execute(
            "SELECT provider FROM mail_processing_runs WHERE run_id = ?",
            (process_result.run_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert provider == "agent_llm"
    assert long_tail in fake_llm.user_prompt
    assert fake_llm.max_output_tokens is None
    log_text = (tmp_path / "data" / "agent_logs" / f"{process_result.run_id}.md").read_text(
        encoding="utf-8"
    )
    assert "System Prompt:" in log_text
    assert "User Prompt:" in log_text
    assert "Output:" in log_text
    assert "LLM visa matter" in log_text
    assert long_tail in log_text

    matters = list_mail_matters(request, limit=10)

    assert len(matters.matters) == 1
    assert matters.matters[0].title == "LLM visa matter"
    assert matters.matters[0].summary == "Full body was read."


def test_mail_process_marks_rate_limit_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    app.state.runtime.mail_agent_loop.llm_client = RateLimitedMailLLMClient()
    request = SimpleNamespace(app=app)

    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "msg_rate_limit_001",
                    "folder": "Inbox",
                    "subject": "Coliwoo notice",
                    "sender": "hello@coliwoo.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-03T09:30:00Z",
                    "body_text": "Coliwoo sent a notice about parcel collection.",
                },
            ],
        ),
        request,
    )

    process_result = process_mail(MailProcessRequest(query="coliwoo notice", limit=10), request)

    conn = sqlite3.connect(app.state.runtime.db_path)
    try:
        provider = conn.execute(
            "SELECT provider FROM mail_processing_runs WHERE run_id = ?",
            (process_result.run_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert provider == "agent_local_heuristic_after_rate_limit"

    log_text = (tmp_path / "data" / "agent_logs" / f"{process_result.run_id}.md").read_text(
        encoding="utf-8"
    )
    assert "rate_limited" in log_text
    assert "retry_after: `30`" in log_text
    assert "fallback: `local_heuristic`" in log_text
    assert "Coliwoo sent a notice about parcel collection." in log_text
