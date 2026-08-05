from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.mail import import_mail, list_mail_matters, search_mail
from app.api.schemas import MailImportRequest
from app.core.config import get_settings
from app.core.mail import MailMatterDraft
from app.core.mail_tools import SyncMailTool
from app.core.outlook import OutlookSyncResult
from app.core.tools import ToolContext, ToolInvocation


def test_mail_import_search_and_matters_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    assert "/mail/import" in app.openapi()["paths"]
    assert "/mail/search" in app.openapi()["paths"]
    assert "/mail/process" not in app.openapi()["paths"]
    assert "/mail/matters" in app.openapi()["paths"]
    assert "mail.sync" in [
        tool.name for tool in app.state.runtime.tool_registry.list_tools(package="mail")
    ]

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

    matters = list_mail_matters(request, limit=10)

    assert matters.matters == []


def test_mail_tools_search_load_and_persist_without_mail_agent_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
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
                    "external_id": "msg_tool_001",
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
    context = ToolContext(
        session_id="session_mail_tools",
        trace_id="trace_mail_tools",
        context_id="ctx_mail_tools",
    )

    search_result = app.state.runtime.tool_executor.execute(
        invocation_id="tool_search_001",
        tool_name="mail.search",
        tool_input={"query": "visa document", "limit": 10},
        context=context,
    )
    message_ids = [
        message["message_id"]
        for message in search_result.output["messages"]
        if isinstance(message, dict)
    ]

    assert search_result.status == "completed"
    assert len(message_ids) == 1

    load_result = app.state.runtime.tool_executor.execute(
        invocation_id="tool_load_001",
        tool_name="mail.load_messages",
        tool_input={"message_ids": message_ids},
        context=context,
    )

    assert load_result.status == "completed"
    assert long_tail in load_result.output["messages"][0]["body_text"]

    draft = MailMatterDraft(
        title="Visa document reminder",
        summary="Please submit the visa document by Friday.",
        status="open",
        priority="high",
        source_message_ids=message_ids,
    )
    persist_result = app.state.runtime.tool_executor.execute(
        invocation_id="tool_persist_001",
        tool_name="mail.persist_matters",
        tool_input={
            "drafts": [draft.model_dump(mode="json")],
            "provider": "test_tool_executor",
            "link_reason": "Tool executor test.",
        },
        context=context,
    )

    assert persist_result.status == "completed"
    assert persist_result.output["matters_created"] == 1

    matters = list_mail_matters(request, limit=10)

    assert len(matters.matters) == 1
    assert matters.matters[0].title == "Visa document reminder"
    assert matters.matters[0].priority == "high"


def test_mail_sync_tool_invokes_runtime_sync_with_tool_trigger():
    calls: list[dict] = []

    def sync_mail(*, folder, limit, max_pages, trigger):
        calls.append(
            {
                "folder": folder,
                "limit": limit,
                "max_pages": max_pages,
                "trigger": trigger,
            }
        )
        return OutlookSyncResult(
            account_id="mail_account_test",
            folder=folder or "Inbox",
            imported_messages=2,
            imported_attachments=1,
            delta_link="https://graph.test/delta",
        )

    tool = SyncMailTool(sync_mail)
    context = ToolContext(
        session_id="session_mail_sync_tool",
        trace_id="trace_mail_sync_tool",
        context_id="ctx_mail_sync_tool",
    )

    result = tool.invoke(
        invocation=ToolInvocation(
            invocation_id="sync_invocation_001",
            tool=tool.spec,
            session_id=context.session_id,
            context_id=context.context_id or "",
            input={"folder": "Inbox", "limit": 5, "max_pages": 2},
        ),
        context=context,
    )

    assert calls == [
        {
            "folder": "Inbox",
            "limit": 5,
            "max_pages": 2,
            "trigger": "tool",
        }
    ]
    assert result.status == "completed"
    assert result.output["provider"] == "outlook"
    assert result.output["imported_messages"] == 2
    assert result.output["delta_link"] == "https://graph.test/delta"
