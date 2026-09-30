from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.knowledge import search_knowledge, sync_mail_knowledge_mirror
from app.api.routes.mail import import_mail, list_mail_matters, search_mail
from app.api.schemas import MailImportRequest, MailKnowledgeMirrorSyncRequest
from app.core.config import get_settings
from app.domains.mail import MailMatterDraft
from app.tool_packages.mail import PersistMailMattersTool, SyncMailTool
from app.integrations.outlook import OutlookSyncResult
from app.core.tools import ToolContext, ToolInvocation
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.domains.mail_knowledge import MailKnowledgeMirror


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
    mail_tool_names = [
        tool.name for tool in app.state.runtime.tool_registry.list_tools(package="mail")
    ]
    assert "mail.sync" in mail_tool_names
    assert "mail.persist_matters" not in mail_tool_names
    package_capabilities = [
        capability
        for capability in app.state.runtime.list_capabilities()
        if capability.type == "tool_package"
    ]
    assert [capability.name for capability in package_capabilities] == [
        package.name for package in app.state.runtime.tool_registry.list_packages()
    ]
    mail_search_tool = next(
        tool
        for tool in app.state.runtime.tool_registry.list_tools(package="mail")
        if tool.name == "mail.search"
    )
    assert "knowledge retrieval layer" in mail_search_tool.description
    assert "empty string" in mail_search_tool.input_schema["properties"]["query"]["description"]
    assert mail_search_tool.input_schema["properties"]["order_by"]["enum"] == [
        "relevance",
        "source_time_desc",
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
    assert search_result.messages[0].source_ref is not None
    assert search_result.messages[0].chunk_id is not None

    matters = list_mail_matters(request, limit=10)

    assert matters.matters == []


def test_mail_import_automatically_mirrors_to_knowledge_with_stable_provenance(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    payload = MailImportRequest(
        account={"provider": "local_json", "email_address": "user@example.com"},
        messages=[
            {
                "external_id": "msg_mirror_001",
                "folder": "Inbox",
                "subject": "Launch dependency risk",
                "sender": "owner@example.com",
                "to": ["user@example.com"],
                "received_at": "2026-08-04T09:30:00Z",
                "body_text": "The deployment depends on final security approval.",
            }
        ],
    )

    imported = import_mail(payload, request)
    result = search_knowledge(
        request,
        q="security approval",
        limit=10,
        source_type=["mail_message"],
        mode="keyword",
    )

    assert imported.imported_messages == 1
    assert len(result.results) == 1
    item = result.results[0]
    assert item.source_type == "mail_message"
    assert item.source_ref.startswith("mail_message:mail_msg_")
    message_id = item.source_ref.split(":", 1)[1].split("#", 1)[0]
    assert item.uri == f"mail://{imported.account_id}/messages/{message_id}"

    updated_payload = payload.model_copy(deep=True)
    updated_payload.messages[0].body_text = "The deployment now has final security approval."
    import_mail(updated_payload, request)
    old_result = search_knowledge(
        request,
        q="depends",
        limit=10,
        source_type=["mail_message"],
        mode="keyword",
    )
    updated_result = search_knowledge(
        request,
        q="now has final",
        limit=10,
        source_type=["mail_message"],
        mode="keyword",
    )

    assert old_result.results == []
    assert len(updated_result.results) == 1

    mirror = sync_mail_knowledge_mirror(
        MailKnowledgeMirrorSyncRequest(account_id=imported.account_id),
        request,
    )
    assert mirror.scope == "account"
    assert mirror.mirrored_messages == 1


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
    assert search_result.output["messages"][0]["chunk_id"]
    assert search_result.output["messages"][0]["source_ref"].startswith("mail_message:")

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
    legacy_tool = PersistMailMattersTool(app.state.runtime.mail_service)
    persist_result = legacy_tool.invoke(
        invocation=ToolInvocation(
            invocation_id="tool_persist_001",
            tool=legacy_tool.spec,
            session_id=context.session_id,
            context_id=context.context_id or "",
            input={
                "drafts": [draft.model_dump(mode="json")],
                "provider": "test_tool_executor",
                "link_reason": "Tool executor test.",
            },
        ),
        context=context,
    )

    assert persist_result.status == "completed"
    assert persist_result.output["matters_created"] == 1

    matters = list_mail_matters(request, limit=10)

    assert len(matters.matters) == 1
    assert matters.matters[0].title == "Visa document reminder"
    assert matters.matters[0].priority == "high"


def test_child_mail_search_and_load_require_source_and_account_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    request = SimpleNamespace(app=app)
    imports = []
    for account, message in (
        ("one@example.com", "msg_scope_one"),
        ("two@example.com", "msg_scope_two"),
    ):
        imports.append(import_mail(
            MailImportRequest(
                account={"provider": "local_json", "email_address": account},
                messages=[{
                    "external_id": message, "folder": "Inbox", "subject": f"scope sentinel {message}",
                    "sender": "sender@example.com", "body_text": f"scope sentinel body for {account}",
                }],
            ), request,
        ))
    first, second = imports
    manager = app.state.runtime.agent_run_manager
    parent = manager.create_run(session_id="parent_session", user_input="parent")
    manager.mark_running(parent.run_id)
    child_run = manager.create_child_run(
        parent_run_id=parent.run_id, plan_id="scope_plan", step_id="scope_step",
        attempt=1, user_input="child",
    )
    manager.mark_child_running(child_run.run_id)
    root = ToolContext(session_id="mail_root")
    root_result = app.state.runtime.tool_executor.execute(
        invocation_id="mail-root-search", tool_name="mail.search",
        tool_input={"query": "scope sentinel", "limit": 10}, context=root,
    )
    assert root_result.status == "completed"
    by_account = {
        account: next(item["message_id"] for item in root_result.output["messages"] if account.split("@")[0] in item["subject"])
        for account in ("one@example.com", "two@example.com")
    }
    child_view = ToolView(
        snapshot_id="mail_child_snapshot", child_run_id=child_run.run_id,
        allowed_packages=("mail",), allowed_tools=("mail.search", "mail.load_messages"),
        allowed_source_ids=(MailKnowledgeMirror.source_id_for_account(first.account_id),),
        allowed_account_ids=(first.account_id,),
        side_effect_level=SideEffectLevel.READ,
    )
    child = ToolContext(session_id="mail_child_session", tool_view=child_view)
    search = app.state.runtime.tool_executor.execute(
        invocation_id="mail-child-search", tool_name="mail.search",
        tool_input={"query": "scope sentinel", "limit": 10}, context=child,
    )
    assert search.status == "completed"
    assert {item["message_id"] for item in search.output["messages"]} == {by_account["one@example.com"]}
    load = app.state.runtime.tool_executor.execute(
        invocation_id="mail-child-known-id", tool_name="mail.load_messages",
        tool_input={"message_ids": [by_account["two@example.com"]]}, context=child,
    )
    assert load.status == "completed"
    assert load.output["messages"] == []
def test_mail_search_uses_knowledge_time_order_and_bounded_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    import_mail(
        MailImportRequest(
            account={"provider": "local_json", "email_address": "user@example.com"},
            messages=[
                {
                    "external_id": "older",
                    "folder": "Inbox",
                    "subject": "Older action",
                    "sender": "older@example.com",
                    "received_at": "2026-08-01T09:00:00Z",
                    "body_text": "older evidence " + ("x" * 900),
                },
                {
                    "external_id": "newer",
                    "folder": "Inbox",
                    "subject": "Newer action",
                    "sender": "newer@example.com",
                    "received_at": "2026-08-02T09:00:00Z",
                    "body_text": "newer evidence " + ("y" * 900),
                },
            ],
        ),
        request,
    )
    result = app.state.runtime.tool_executor.execute(
        invocation_id="mail_search_time_order",
        tool_name="mail.search",
        tool_input={
            "query": "",
            "limit": 10,
            "order_by": "source_time_desc",
            "max_snippet_chars": 120,
        },
        context=ToolContext(session_id="mail_search", trace_id="mail_search"),
    )

    assert result.status == "completed"
    messages = result.output["messages"]
    assert [message["subject"] for message in messages] == ["Newer action", "Older action"]
    assert all(len(message["snippet"]) <= 120 for message in messages)
    assert all(message["source_ref"].startswith("mail_message:") for message in messages)

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
