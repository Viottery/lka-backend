from __future__ import annotations

import sqlite3

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext, ToolInvocation
from app.domains.knowledge import KnowledgeSearchResult, KnowledgeService
from app.domains.mail import MailAccountInput, MailMessageInput, MailService
from app.domains.mail_knowledge import MailKnowledgeMirror
from app.tool_packages.mail import ListMailTool


@pytest.fixture
def mail_service(tmp_path):
    path = tmp_path / "mail.sqlite3"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    from app.storage.db import init_db

    init_db(path)
    return MailService(connect)


def _add(service, email: str, prefix: str, count: int, *, base="2026-05-01T00:00:00Z"):
    messages = [
        MailMessageInput(
            external_id=f"{prefix}-{i:03}",
            folder="Inbox",
            subject=f"Subject {i}",
            sender="sender@example.com",
            received_at=base,
            body_text=f"private body {prefix}-{i:03}",
        )
        for i in range(count)
    ]
    return service.import_messages(account=MailAccountInput(email_address=email), messages=messages)


class _AllMailSourcesMirror:
    def __init__(self, service):
        self.service = service

    def active_account_ids(self, *, session_id=None):
        return self.service.list_authorized_account_ids()


def test_mail_list_pages_21_to_40_metadata_only_and_counts(mail_service):
    _add(mail_service, "a@example.com", "a", 45)
    result = mail_service.list_messages(
        received_from="2026-05-01T00:00:00Z", received_before="2026-06-01T00:00:00Z",
        start_rank=21, end_rank=40,
    )
    assert result.total_matches == 45
    assert result.requested_range == {"start_rank": 21, "end_rank": 40}
    assert result.returned_count == 20
    assert result.has_more is True
    assert result.next_range == {"start_rank": 41, "end_rank": 45}
    assert result.applied_limit == 20
    assert result.listing_id
    assert result.coverage == {"start_rank": 21, "end_rank": 40}
    assert all("body_text" not in card.model_dump() and "snippet" not in card.model_dump() for card in result.messages)


def test_mail_list_half_open_dates_and_account_grants(mail_service):
    a = _add(mail_service, "a@example.com", "a", 1, base="2026-05-01T00:00:00Z")
    b = _add(mail_service, "b@example.com", "b", 1, base="2026-05-02T00:00:00Z")
    tool = ListMailTool(mail_service, _AllMailSourcesMirror(mail_service))
    view = ToolView(
        snapshot_id="snap", child_run_id="child", allowed_packages=("mail",),
        allowed_tools=("mail.list",), allowed_source_ids=(MailKnowledgeMirror.source_id_for_account(a.account_id),),
        allowed_account_ids=(a.account_id,), side_effect_level=SideEffectLevel.READ,
    )
    result = tool.invoke(
        invocation=ToolInvocation(invocation_id="list", tool=tool.spec, session_id="child-session", context_id="ctx", input={
            "received_from": "2026-05-01T00:00:00Z", "received_before": "2026-05-02T00:00:00Z",
        }),
        context=ToolContext(session_id="child-session", tool_view=view),
    )
    assert result.status == "completed"
    assert result.output["total_matches"] == 1
    assert len(result.output["messages"]) == 1
    allowed_ids = {message.message_id for message in mail_service.load_messages(
        [result.output["messages"][0]["message_id"]], account_ids=[a.account_id]
    )}
    assert allowed_ids == {result.output["messages"][0]["message_id"]}
    assert result.output["messages"][0]["message_id"] not in {
        message.message_id for message in mail_service.load_messages(
            [result.output["messages"][0]["message_id"]], account_ids=[b.account_id]
        )
    }


def test_mail_list_rejects_invalid_ranges_and_child_missing_grants(mail_service):
    _add(mail_service, "a@example.com", "a", 1)
    with pytest.raises(ValueError):
        mail_service.list_messages(
            received_from="2026-05-01T00:00:00Z", received_before="2026-06-01T00:00:00Z",
            start_rank=21, end_rank=41,
        )
    with pytest.raises(ValueError):
        mail_service.list_messages(
            received_from="2026-05-01T00:00:00Z", received_before="2026-06-01T00:00:00Z",
            start_rank=0,
        )
    tool = ListMailTool(mail_service, _AllMailSourcesMirror(mail_service))
    view = ToolView(
        snapshot_id="snap", child_run_id="child", allowed_packages=("mail",),
        allowed_tools=("mail.list",), side_effect_level=SideEffectLevel.READ,
    )
    rejected = tool.invoke(
        invocation=ToolInvocation(invocation_id="list", tool=tool.spec, session_id="child-session", context_id="ctx", input={
            "received_from": "2026-05-01T00:00:00Z", "received_before": "2026-06-01T00:00:00Z",
        }),
        context=ToolContext(session_id="child-session", tool_view=view),
    )
    assert rejected.status == "rejected"


def test_mail_list_timezone_boundaries_and_stable_tie_order(mail_service):
    _add(mail_service, "a@example.com", "a", 2, base="2026-05-01T02:00:00+02:00")
    result = mail_service.list_messages(
        received_from="2026-05-01T00:00:00Z", received_before="2026-05-01T00:00:01Z",
    )
    assert result.total_matches == 2
    assert result.requested_range == {"start_rank": 1, "end_rank": 20}
    assert [card.message_id for card in result.messages] == sorted(
        [card.message_id for card in result.messages], reverse=True,
    )


def test_mail_list_snapshot_rejects_insert_between_pages(mail_service):
    _add(mail_service, "a@example.com", "a", 25)
    first = mail_service.list_messages(
        received_from="2026-05-01T00:00:00Z", received_before="2026-06-01T00:00:00Z",
    )
    assert len(first.messages) == 20
    _add(mail_service, "b@example.com", "b", 1, base="2026-05-15T00:00:00Z")
    with pytest.raises(ValueError, match="Stale listing"):
        mail_service.list_messages(
            received_from="2026-05-01T00:00:00Z", received_before="2026-06-01T00:00:00Z",
            start_rank=21, end_rank=25, listing_id=first.listing_id,
        )


def test_mail_list_rejects_tampered_listing_id(mail_service):
    first = mail_service.list_messages(
        received_from="2026-05-01T00:00:00Z", received_before="2026-06-01T00:00:00Z",
    )
    tampered = first.listing_id[:-1] + ("A" if first.listing_id[-1] != "A" else "B")
    with pytest.raises(ValueError, match="listing_id"):
        mail_service.list_messages(
            received_from="2026-05-01T00:00:00Z", received_before="2026-06-01T00:00:00Z",
            listing_id=tampered,
        )


def test_mail_search_reports_capped_non_exhaustive_retrieval_metadata(mail_service):
    class FakeKnowledgeService:
        def search(self, **kwargs):
            assert kwargs["limit"] == 100
            return KnowledgeSearchResult(
                query=kwargs["query"], query_id="q", results=[], filtered_count=0,
            )

    from app.domains.mail_knowledge import MailKnowledgeMirror

    mirror = MailKnowledgeMirror(mail_service=mail_service, knowledge_service=FakeKnowledgeService())
    result = mirror.search(query="anything", limit=150)
    assert result.requested_limit == 150
    assert result.applied_limit == 100
    assert result.returned_count == 0
    assert result.possible_more is False
    assert not hasattr(result, "total_matches")


def test_mail_list_excludes_deactivated_knowledge_source(mail_service):
    imported = _add(mail_service, "inactive@example.com", "inactive", 1)
    knowledge_service = KnowledgeService(mail_service._conn_factory)
    mirror = MailKnowledgeMirror(mail_service=mail_service, knowledge_service=knowledge_service)
    mirror.sync(account_id=imported.account_id)
    conn = mail_service._conn_factory()
    try:
        conn.execute(
            "UPDATE knowledge_sources SET status = 'inactive' WHERE source_id = ?",
            (mirror.source_id_for_account(imported.account_id),),
        )
        conn.commit()
    finally:
        conn.close()
    result = ListMailTool(mail_service, mirror).invoke(
        invocation=ToolInvocation(
            invocation_id="inactive-list", tool=ListMailTool.spec, session_id="session",
            context_id="ctx", input={
                "received_from": "2026-05-01T00:00:00Z",
                "received_before": "2026-06-01T00:00:00Z",
            },
        ),
        context=ToolContext(session_id="session"),
    )
    assert result.status == "completed"
    assert result.output["total_matches"] == 0
    assert result.output["messages"] == []


def test_mail_list_caps_long_card_headers_without_changing_stored_values(mail_service):
    subject = "S" * 240
    sender = "s" * 180
    folder = "F" * 100
    mail_service.import_messages(
        account=MailAccountInput(email_address="long-headers@example.com"),
        messages=[MailMessageInput(
            external_id="long-header", subject=subject, sender=sender, folder=folder,
            received_at="2026-05-10T00:00:00Z", body_text="private body",
        )],
    )
    result = mail_service.list_messages(
        received_from="2026-05-01T00:00:00Z", received_before="2026-06-01T00:00:00Z",
    )
    card = result.messages[0]
    assert len(card.subject) == 180 and card.subject_truncated is True
    assert len(card.sender) == 120 and card.sender_truncated is True
    assert len(card.folder) == 80 and card.folder_truncated is True
    assert "body_text" not in card.model_dump() and "snippet" not in card.model_dump()
    conn = mail_service._conn_factory()
    try:
        stored = conn.execute(
            "SELECT subject, sender, folder FROM mail_messages WHERE message_id = ?", (card.message_id,),
        ).fetchone()
    finally:
        conn.close()
    assert stored["subject"] == subject
    assert stored["sender"] == sender
    assert stored["folder"] == folder
