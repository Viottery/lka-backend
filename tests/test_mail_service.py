from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.api.routes.mail import import_mail, list_mail_matters, process_mail, search_mail
from app.api.schemas import MailImportRequest, MailProcessRequest
from app.core.config import get_settings
from app.core.llm import (
    LLMAuthenticationError,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseParseError,
    LLMTimeoutError,
)


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
        raise LLMRateLimitError(
            status_code=429,
            message="LLM provider returned HTTP 429: too many requests",
            retry_after="30",
        )


class TransientRateLimitedMailLLMClient:
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
                message="LLM provider returned HTTP 429: too many requests",
                retry_after="0",
            )
        message_id = user_prompt.split("message_id: ", 1)[1].splitlines()[0]
        return LLMResponse(
            provider="fake_llm",
            status="completed",
            content=(
                '{"matters":[{"title":"Retried LLM matter","summary":"LLM succeeded after '
                'rate limit retry.","status":"open","priority":"normal",'
                f'"source_message_ids":["{message_id}"]'
                "}]}"
            ),
            prompt_summary=prompt_summary,
        )


class FailingMailLLMClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        raise self.error


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
    fake_llm = RateLimitedMailLLMClient()
    app.state.runtime.mail_agent_loop.llm_client = fake_llm
    app.state.runtime.mail_agent_loop.sleep = lambda _: None
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
    assert fake_llm.calls == 2

    log_text = (tmp_path / "data" / "agent_logs" / f"{process_result.run_id}.md").read_text(
        encoding="utf-8"
    )
    assert "rate_limited" in log_text
    assert "retry_after: `30`" in log_text
    assert "retry_count: `1`" in log_text
    assert '"retry_scheduled": true' in log_text
    assert "fallback: `local_heuristic`" in log_text
    assert "Coliwoo sent a notice about parcel collection." in log_text


def test_mail_process_retries_rate_limit_then_uses_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    fake_llm = TransientRateLimitedMailLLMClient()
    app.state.runtime.mail_agent_loop.llm_client = fake_llm
    app.state.runtime.mail_agent_loop.sleep = lambda _: None
    request = SimpleNamespace(app=app)

    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "msg_transient_rate_limit_001",
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
    assert provider == "agent_llm"
    assert fake_llm.calls == 2

    log_text = (tmp_path / "data" / "agent_logs" / f"{process_result.run_id}.md").read_text(
        encoding="utf-8"
    )
    assert "rate_limited" in log_text
    assert "Retried LLM matter" in log_text
    assert "retry_count: `1`" in log_text

    matters = list_mail_matters(request, limit=10)

    assert len(matters.matters) == 1
    assert matters.matters[0].title == "Retried LLM matter"


@pytest.mark.parametrize(
    ("error", "expected_provider", "expected_status"),
    [
        (
            LLMAuthenticationError(status_code=401, message="bad key"),
            "agent_local_heuristic_after_auth_error",
            "auth_failed",
        ),
        (
            LLMNetworkError("dns failed"),
            "agent_local_heuristic_after_network_error",
            "network_failed",
        ),
        (
            LLMTimeoutError("timed out"),
            "agent_local_heuristic_after_timeout",
            "timeout",
        ),
        (
            LLMProviderHTTPError(status_code=500, message="provider error"),
            "agent_local_heuristic_after_provider_http_error",
            "http_failed",
        ),
        (
            LLMResponseParseError("bad response"),
            "agent_local_heuristic_after_parse_error",
            "response_parse_failed",
        ),
    ],
)
def test_mail_process_classifies_llm_errors(
    tmp_path,
    monkeypatch,
    error,
    expected_provider,
    expected_status,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    app.state.runtime.mail_agent_loop.llm_client = FailingMailLLMClient(error)
    app.state.runtime.mail_agent_loop.sleep = lambda _: None
    request = SimpleNamespace(app=app)

    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "msg_llm_error_001",
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
    assert provider == expected_provider

    log_text = (tmp_path / "data" / "agent_logs" / f"{process_result.run_id}.md").read_text(
        encoding="utf-8"
    )
    assert expected_status in log_text
    assert type(error).__name__ in log_text
