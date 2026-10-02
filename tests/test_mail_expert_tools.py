from __future__ import annotations

import sqlite3

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext, ToolInvocation
from app.domains.mail import MailAccountInput, MailMessageInput, MailService
from app.domains.mail_knowledge import MailKnowledgeMirror
from app.tool_packages.mail_expert_tools import MailBatchLoadTool, MailSnapshotTool


@pytest.fixture
def services(tmp_path):
    path = tmp_path / "mail.sqlite3"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    from app.storage.db import init_db

    init_db(path)
    service = MailService(connect)
    mirror = _AllMailMirror(service)
    return service, mirror


class _AllMailMirror:
    def __init__(self, service):
        self.service = service

    def active_account_ids(self, *, session_id=None):
        return self.service.list_authorized_account_ids()


def add_mail(service, email: str, prefix: str, count: int, *, folder="Inbox", body="mail body"):
    return service.import_messages(
        account=MailAccountInput(email_address=email),
        messages=[MailMessageInput(
            external_id=f"{prefix}-{i}", folder=folder, subject=f"subject {i}",
            sender="sender@example.com", received_at="2026-06-01T12:00:00Z", body_text=f"{body} {i}",
        ) for i in range(count)],
    )


def invoke(tool, data, *, context=None):
    return tool.invoke(
        invocation=ToolInvocation(invocation_id="test", tool=tool.spec, session_id="session", context_id="ctx", input=data),
        context=context or ToolContext(session_id="session"),
    )


def child_context(account_ids=(), source_ids=()):
    return ToolContext(session_id="child-session", tool_view=ToolView(
        snapshot_id="snap", child_run_id="child", allowed_packages=("mail",),
        allowed_tools=("mail.snapshot", "mail.batch_load"), allowed_account_ids=tuple(account_ids),
        allowed_source_ids=tuple(source_ids), side_effect_level=SideEffectLevel.READ,
    ))


def test_specs_are_read_only_scoped_and_have_expected_caps(services):
    service, mirror = services
    snapshot = MailSnapshotTool(service, mirror).spec
    batch = MailBatchLoadTool(service).spec
    for spec in (snapshot, batch):
        assert spec.package == "mail"
        assert spec.read_only is True
        assert spec.side_effects == ["read_local_db"]
        assert spec.scope_uses_sources and spec.scope_uses_accounts and spec.scope_filtering_required
    assert snapshot.input_schema["properties"]["max_messages"]["maximum"] == 500
    assert batch.input_schema["properties"]["message_ids"]["maxItems"] == 100
    assert batch.input_schema["properties"]["max_chars_per_message"]["maximum"] == 1500


def test_snapshot_reads_pages_under_one_listing_and_reports_complete(services):
    service, mirror = services
    add_mail(service, "many@example.com", "m", 45)
    result = invoke(MailSnapshotTool(service, mirror), {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
    })
    assert result.status == "completed"
    assert result.output["total_matches"] == result.output["returned_count"] == 45
    assert result.output["complete"] is True
    assert result.output["next_range"] is None
    assert result.output["coverage"] == {"start_rank": 1, "end_rank": 45}
    assert result.output["listing_id"]
    assert len(result.output["messages"]) == 45
    assert all("body_text" not in row and "snippet" not in row for row in result.output["messages"])


def test_snapshot_cap_and_folder_filter_never_claim_complete(services):
    service, mirror = services
    add_mail(service, "many@example.com", "in", 25, folder="Inbox")
    add_mail(service, "many@example.com", "out", 3, folder="Archive")
    tool = MailSnapshotTool(service, mirror)
    partial = invoke(tool, {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
        "folder": "inbox", "max_messages": 5,
    })
    assert partial.status == "completed"
    assert partial.output["total_matches"] == 25
    assert partial.output["returned_count"] == 5
    assert partial.output["complete"] is False
    assert partial.output["next_range"] == {"start_rank": 6, "end_rank": 25}
    all_rows = invoke(tool, {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
        "max_messages": 500,
    })
    assert all_rows.output["total_matches"] == 28
    assert {r["folder"] for r in all_rows.output["messages"]} == {"Inbox", "Archive"}


def test_snapshot_enforces_child_scope_and_scope_filters_accounts(services):
    service, mirror = services
    allowed = add_mail(service, "allowed@example.com", "a", 2)
    add_mail(service, "denied@example.com", "b", 4)
    tool = MailSnapshotTool(service, mirror)
    denied = invoke(tool, {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
    }, context=child_context())
    assert denied.status == "rejected"
    scoped = invoke(tool, {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
    }, context=child_context((allowed.account_id,), (MailKnowledgeMirror.source_id_for_account(allowed.account_id),)))
    assert scoped.status == "completed"
    assert scoped.output["total_matches"] == scoped.output["returned_count"] == 2


def test_child_explicit_grant_survives_isolated_session(services):
    service, _ = services
    account = add_mail(service, "session@example.com", "a", 1)

    class SessionMirror:
        def active_account_ids(self, *, session_id=None):
            return () if session_id == "child-session" else (account.account_id,)

    result = invoke(MailSnapshotTool(service, SessionMirror()), {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
    }, context=child_context((account.account_id,), (MailKnowledgeMirror.source_id_for_account(account.account_id),)))
    assert result.status == "completed"
    assert result.output["total_matches"] == 1


def test_snapshot_caps_at_500_and_validates_cap(services):
    service, mirror = services
    add_mail(service, "large@example.com", "x", 505)
    tool = MailSnapshotTool(service, mirror)
    capped = invoke(tool, {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
        "max_messages": 500,
    })
    assert capped.status == "completed"
    assert capped.output["total_matches"] == 505
    assert capped.output["returned_count"] == 500
    assert capped.output["complete"] is False
    assert capped.output["next_range"]["start_rank"] == 501
    continued = invoke(tool, {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
        "start_rank": 501, "listing_id": capped.output["listing_id"], "max_messages": 500,
    })
    assert continued.status == "completed"
    assert continued.output["returned_count"] == 5
    assert continued.output["coverage"] == {"start_rank": 501, "end_rank": 505}
    assert continued.output["complete"] is True
    assert not ({row["message_id"] for row in capped.output["messages"]}
                & {row["message_id"] for row in continued.output["messages"]})
    stale = invoke(tool, {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
        "start_rank": 501, "listing_id": "stale-listing-id", "max_messages": 500,
    })
    assert stale.status == "rejected"
    invalid = invoke(tool, {
        "received_from": "2026-06-01T00:00:00Z", "received_before": "2026-06-02T00:00:00Z",
        "max_messages": 501,
    })
    assert invalid.status == "rejected"


def test_batch_load_bounds_body_and_reports_missing_ids(services):
    service, _ = services
    add_mail(service, "load@example.com", "body", 2, body="z" * 1800)
    records = service.list_messages(received_from="2026-06-01T00:00:00Z", received_before="2026-06-02T00:00:00Z")
    message_ids = [card.message_id for card in records.messages]
    result = invoke(MailBatchLoadTool(service), {"message_ids": [*message_ids, "missing-id"], "max_chars_per_message": 50})
    assert result.status == "completed"
    assert result.output["returned_count"] == 2
    assert result.output["missing_ids"] == ["missing-id"]
    for message in result.output["messages"]:
        assert len(message["body_text"]) == 50 + len("\n...[truncated]")
        assert message["body_truncated"] is True
        assert message["body_text"].endswith("...[truncated]")


def test_batch_load_child_scope_denial_and_account_source_filter(services):
    service, _ = services
    permitted = add_mail(service, "permitted@example.com", "p", 1)
    forbidden = add_mail(service, "forbidden@example.com", "f", 1)
    cards = service.list_messages(
        received_from="2026-06-01T00:00:00Z", received_before="2026-06-02T00:00:00Z",
    ).messages
    ids = [card.message_id for card in cards]
    tool = MailBatchLoadTool(service)
    denied = invoke(tool, {"message_ids": ids}, context=child_context())
    assert denied.status == "rejected"
    scoped = invoke(tool, {"message_ids": ids}, context=child_context(
        (permitted.account_id,), (MailKnowledgeMirror.source_id_for_account(permitted.account_id),),
    ))
    assert scoped.status == "completed"
    assert scoped.output["returned_count"] == 1
    assert scoped.output["messages"][0]["message_id"] not in {
        r.message_id for r in service.load_messages(ids, account_ids=[forbidden.account_id])
    }


def test_batch_load_enforces_id_and_character_caps(services):
    service, _ = services
    tool = MailBatchLoadTool(service)
    assert invoke(tool, {"message_ids": [str(i) for i in range(101)]}).status == "rejected"
    assert invoke(tool, {"message_ids": ["x"], "max_chars_per_message": 1501}).status == "rejected"
