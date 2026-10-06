from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.core.background_jobs import BackgroundJobStore
from app.domains.matters import (
    MatterCreateInput,
    MatterService,
    MatterSourceLinkInput,
    MatterUpdateInput,
)
from app.domains.message_history import MessageHistoryService
from app.domains.message_matter_proposals import (
    DECISION_ADAPTER,
    HumanControlPrincipal,
    MessageMatterProposalService,
    ProposalConflict,
)
from app.storage.db import connect, init_db

HUMAN = HumanControlPrincipal("paired-ui")


@pytest.fixture
def services(tmp_path):
    path = tmp_path / "r4.sqlite3"
    init_db(path)
    messages = MessageHistoryService(path, BackgroundJobStore(path))
    messages.ensure_schema()
    messages.set_policy(
        {
            "platform": "mock",
            "account_id": "account",
            "conversation_type": "group",
            "conversation_id": "group",
            "record_enabled": True,
            "analysis_enabled": True,
            "proposals_enabled": True,
        }
    )
    messages.import_messages(
        [
            {
                "platform": "mock",
                "account_id": "account",
                "message_id": "one",
                "conversation_type": "group",
                "conversation_id": "group",
                "sender_name": "Author",
                "text": "Submit the report tomorrow",
                "received_at": 200,
                "sent_at": 100,
            }
        ]
    )
    matters = MatterService(lambda: connect(path))
    proposals = MessageMatterProposalService(messages, matters)
    return messages, matters, proposals


def candidate(services, **extra):
    messages, _, proposals = services
    message_id = messages.recent()["messages"][0]["message_id"]
    return proposals.publish_proposal(
        {
            "title": "Submit report",
            "summary": "Prepare the report",
            "action_key": "submit-report",
            "evidence_message_ids": [message_id],
            **extra,
        }
    )


def decision(preview, **extra):
    return {
        "action": "create",
        "expected_revision": preview["revision"],
        "preview_digest": preview["preview_digest"],
        "evidence_digest": preview["evidence_digest"],
        "decision_key": "ui-decision",
        "reviewed_fields": preview["proposed_fields"],
        **extra,
    }


def policy_change(messages, **changes):
    policy = messages.list_policies()[0]
    fields = {
        name: policy[name]
        for name in (
            "platform",
            "account_id",
            "conversation_type",
            "conversation_id",
            "record_enabled",
            "analysis_enabled",
            "proposals_enabled",
        )
    }
    return messages.set_policy({**fields, "expected_revision": policy["revision"], **changes})


def test_create_is_exact_atomic_and_replay_safe(services):
    _, matters, proposals = services
    preview = candidate(services)
    result = proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    assert result == proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    record = matters.get_matter(matter_id=result["matter_id"])
    assert record.revision == 1
    assert record.source_links[0].source_id == preview["evidence"][0]["message_id"]
    assert matters.search_matters(query="report").matters[0].matter_id == record.matter_id
    assert candidate(services)["state"] == "accepted"
    with proposals._transaction() as conn:
        assert conn.execute("SELECT count(*) FROM message_proposal_decisions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM message_proposal_applications").fetchone()[0] == 1


def test_generic_paths_fail_closed_without_private_receipt(services):
    messages, matters, proposals = services
    message_id = messages.recent()["messages"][0]["message_id"]
    link = MatterSourceLinkInput(source_type="message_history_message", source_id=message_id)
    with pytest.raises(PermissionError, match="receipt_required"):
        matters.create_matter(
            MatterCreateInput(
                title="forged",
                source_links=[link],
                metadata={"approved": True, "human_receipt": "anything"},
            )
        )
    plain = matters.create_matter(MatterCreateInput(title="manual"))
    with pytest.raises(PermissionError, match="receipt_required"):
        matters.link_source(matter_id=plain.matter_id, source_link=link)
    preview = candidate(services)
    accepted = proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    with pytest.raises(PermissionError, match="receipt_required"):
        matters.update_matter(
            matter_id=accepted["matter_id"], payload=MatterUpdateInput(status="done")
        )
    with pytest.raises(PermissionError, match="receipt_required"):
        matters.link_source(
            matter_id=accepted["matter_id"],
            source_link=MatterSourceLinkInput(source_type="trace", source_id="x"),
        )


def test_failure_rolls_back_matter_fts_decision_and_application(services, monkeypatch):
    _, matters, proposals = services
    preview = candidate(services)
    original = matters._upsert_fts

    def failing(*args):
        original(*args)
        raise RuntimeError("fail after FTS")

    monkeypatch.setattr(matters, "_upsert_fts", failing)
    with pytest.raises(RuntimeError, match="after FTS"):
        proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    with proposals._transaction() as conn:
        for table in (
            "matters",
            "matters_fts",
            "matter_source_links",
            "message_proposal_decisions",
            "message_proposal_applications",
        ):
            assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    assert proposals.get_proposal(preview["proposal_id"])["state"] == "pending"


def test_real_message_cannot_be_disguised_as_local_evidence(services):
    messages, matters, _ = services
    message_id = messages.recent()["messages"][0]["message_id"]
    from app.domains.matters import MatterCreateInput
    with pytest.raises(PermissionError, match="canonical_type"):
        matters.create_matter(MatterCreateInput(title="forged alias", summary="",
            source_links=[{"source_type": "local_evidence", "source_id": message_id, "reason": "alias"}]))


def test_strict_discriminated_decisions_and_server_principal(services):
    _, _, proposals = services
    preview = candidate(services)
    with pytest.raises(ValidationError):
        DECISION_ADAPTER.validate_python(decision(preview, target_matter_id="ignored"))
    with pytest.raises(ValidationError):
        DECISION_ADAPTER.validate_python(decision(preview, decided_by="user"))
    for principal in (
        {"principal_id": "user", "kind": "human_control"},
        HumanControlPrincipal("worker", "reading_worker"),
    ):
        with pytest.raises(PermissionError, match="identity_required"):
            proposals.decide(preview["proposal_id"], decision(preview), principal=principal)
    rejected = {
        key: value
        for key, value in decision(preview, action="reject").items()
        if key != "reviewed_fields"
    }
    assert (
        proposals.decide(preview["proposal_id"], rejected, principal=HUMAN)["state"] == "rejected"
    )
    assert candidate(services)["state"] == "rejected"


def test_preview_revision_evidence_and_decision_key_are_bound(services):
    _, _, proposals = services
    preview = candidate(services)
    replacement = candidate(services, summary="Explicit corrected instruction")
    assert replacement["revision"] == preview["revision"] + 1
    with pytest.raises(ProposalConflict, match="preview_changed"):
        proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    with pytest.raises(ProposalConflict, match="evidence_changed"):
        proposals.decide(
            replacement["proposal_id"],
            decision(replacement, evidence_digest="0" * 64),
            principal=HUMAN,
        )
    proposals.decide(replacement["proposal_id"], decision(replacement), principal=HUMAN)
    with pytest.raises(ProposalConflict, match="key_reused"):
        proposals.decide(
            replacement["proposal_id"],
            decision(
                replacement,
                reviewed_fields={**replacement["proposed_fields"], "title": "Different"},
            ),
            principal=HUMAN,
        )


def test_permission_generations_freeze_and_revalidation_invalidates_old_preview(services):
    messages, _, proposals = services
    preview = candidate(services)
    policy_change(messages, proposals_enabled=False)
    assert proposals.get_proposal(preview["proposal_id"])["frozen_reason"] == "proposals_disabled"
    with pytest.raises(PermissionError, match="frozen"):
        proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    policy_change(messages, proposals_enabled=True)
    with pytest.raises(PermissionError, match="frozen"):
        proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    updated = proposals.revalidate(
        preview["proposal_id"], expected_revision=preview["revision"], principal=HUMAN
    )
    assert updated["revision"] == preview["revision"] + 1
    assert updated["preview_digest"] != preview["preview_digest"]
    with pytest.raises(ProposalConflict, match="preview_changed"):
        proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    assert (
        proposals.decide(updated["proposal_id"], decision(updated), principal=HUMAN)["state"]
        == "accepted"
    )


def test_record_revocation_hides_content_and_rejects_replay(services):
    messages, _, proposals = services
    preview = candidate(services)
    proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    policy_change(messages, record_enabled=False, analysis_enabled=False, proposals_enabled=False)
    with pytest.raises(PermissionError, match="revoked"):
        proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    revoked = proposals.get_proposal(preview["proposal_id"])
    assert revoked["state"] == "accepted"
    assert revoked["frozen_reason"] == "record_revoked"
    assert "evidence" not in revoked and "proposed_fields" not in revoked
    assert proposals.list_proposals(state="accepted")["proposals"][0] == revoked


def test_pending_revocation_is_irreversible_and_pause_allows_human_decision(services):
    messages, _, proposals = services
    preview = candidate(services)
    policy_change(messages, record_enabled=False, analysis_enabled=False, proposals_enabled=False)
    assert proposals.get_proposal(preview["proposal_id"])["state"] == "revoked"
    policy_change(messages, record_enabled=True, analysis_enabled=True, proposals_enabled=True)
    assert proposals.get_proposal(preview["proposal_id"])["state"] == "revoked"
    with pytest.raises(ProposalConflict):
        proposals.revalidate(
            preview["proposal_id"], expected_revision=preview["revision"], principal=HUMAN
        )
    second = candidate(services, action_key="new-action")
    with proposals._transaction() as conn:
        conn.execute("UPDATE message_reading_control SET paused=1,service_epoch=service_epoch+1")
    assert (
        proposals.decide(second["proposal_id"], decision(second), principal=HUMAN)["state"]
        == "accepted"
    )


def test_link_preserves_reviewed_matter_fields_and_checks_revision_before_replay(services):
    _, matters, proposals = services
    target = matters.create_matter(
        MatterCreateInput(title="Original title", status="waiting", priority="high")
    )
    preview = candidate(services)
    link = {
        key: value
        for key, value in decision(preview, action="link_existing").items()
        if key != "reviewed_fields"
    }
    link.update(
        target_matter_id=target.matter_id,
        expected_target_revision=target.revision,
        source_reason="Reviewed quote",
    )
    stale = {**link, "expected_target_revision": 999}
    with pytest.raises(ProposalConflict, match="target_revision_changed"):
        proposals.decide(preview["proposal_id"], stale, principal=HUMAN)
    result = proposals.decide(preview["proposal_id"], link, principal=HUMAN)
    updated = matters.get_matter(matter_id=target.matter_id)
    assert (updated.title, updated.status, updated.priority) == (
        target.title,
        target.status,
        target.priority,
    )
    assert updated.revision == target.revision + 1
    assert proposals.decide(preview["proposal_id"], link, principal=HUMAN) == result
    with proposals._transaction() as conn:
        conn.execute(
            "UPDATE matters SET revision=revision+1 WHERE matter_id=?", (target.matter_id,)
        )
    with pytest.raises(ProposalConflict, match="target_revision_changed"):
        proposals.decide(preview["proposal_id"], link, principal=HUMAN)


def test_all_matter_mutations_revision_and_caller_transaction(services):
    _, matters, proposals = services
    created = matters.create_matter(MatterCreateInput(title="Local task"))
    updated = matters.update_matter(
        matter_id=created.matter_id, payload=MatterUpdateInput(status="waiting")
    )
    linked = matters.link_source(
        matter_id=created.matter_id,
        source_link=MatterSourceLinkInput(source_type="trace", source_id="trace"),
    )
    assert (created.revision, updated.revision, linked.revision) == (1, 2, 3)
    conn = proposals._conn_factory()
    conn.execute("BEGIN IMMEDIATE")
    transient = matters.create_matter(MatterCreateInput(title="Roll back"), conn=conn)
    assert conn.in_transaction
    conn.rollback()
    conn.close()
    with pytest.raises(KeyError):
        matters.get_matter(matter_id=transient.matter_id)


def test_scope_checked_before_replay_and_deterministic_suggestions(services):
    messages, matters, proposals = services
    match = matters.create_matter(MatterCreateInput(title="Submit report"))
    preview = candidate(services)
    assert preview["suggested_matters"][0]["matter_id"] == match.matter_id
    proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    for scope in ({"allowed_sources": []}, {"allowed_accounts": []}):
        with pytest.raises(PermissionError):
            proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN, **scope)
        assert proposals.list_proposals(**scope)["proposals"] == []
    policy = messages.list_policies()[0]
    assert (
        proposals.get_proposal(
            preview["proposal_id"],
            allowed_sources=[policy["source_id"]],
            allowed_accounts=[policy["account_scope_id"]],
        )["state"]
        == "accepted"
    )


def test_concurrent_distinct_decisions_apply_once(services):
    _, matters, proposals = services
    preview = candidate(services)

    def approve(key):
        try:
            return proposals.decide(
                preview["proposal_id"], decision(preview, decision_key=key), principal=HUMAN
            )
        except ProposalConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, ("human-click-1", "human-click-2")))
    assert sum(result is not None for result in outcomes) == 1
    assert len(matters.list_matters().matters) == 1


def test_generation_fence_and_ambiguous_due_date(services):
    _, _, proposals = services
    preview = candidate(services, due_at="2026-10-05T10:00:00+08:00", due_provenance="inferred")
    assert preview["proposed_fields"]["due_at"] is None
    with pytest.raises(ProposalConflict, match="generation_fenced"):
        proposals.publish_proposal(
            {
                "title": "A",
                "action_key": "a",
                "evidence_message_ids": [preview["evidence"][0]["message_id"]],
            },
            expected_fences={"capture_epoch": 0},
        )


def test_one_source_can_have_distinct_actions_without_title_dedup(services):
    _, _, proposals = services
    first = candidate(services)
    same = candidate(services)
    assert (same["proposal_id"], same["revision"]) == (first["proposal_id"], first["revision"])
    other = candidate(services, action_key="review-report")
    assert other["proposal_id"] != first["proposal_id"]
    assert len(proposals.list_proposals()["proposals"]) == 2
    assert proposals.list_proposals(limit=1)["has_more"]
    page = proposals.list_proposals(limit=1)
    next_page = proposals.list_proposals(limit=1, cursor=page["next_cursor"])
    assert len(next_page["proposals"]) == 1
    assert next_page["proposals"][0]["proposal_id"] != page["proposals"][0]["proposal_id"]
    with pytest.raises(ValueError, match="scope_changed"):
        proposals.list_proposals(limit=1, cursor=page["next_cursor"], allowed_sources=[])
    for malformed in ("[]", "{}", "not-json"):
        with pytest.raises(ValueError):
            proposals.list_proposals(cursor=malformed)


def test_publication_uses_caller_transaction_and_invalid_evidence_fails_closed(services):
    messages, _, proposals = services
    message_id = messages.recent()["messages"][0]["message_id"]
    payload = {
        "title": "Only after commit",
        "action_key": "transaction",
        "evidence_message_ids": [message_id],
    }
    conn = proposals._conn_factory()
    conn.execute("BEGIN IMMEDIATE")
    result = proposals.publish_proposal(payload, conn=conn)
    assert conn.in_transaction
    conn.rollback()
    conn.close()
    with pytest.raises(KeyError):
        proposals.get_proposal(result["proposal_id"])
    for bad_id in ("unknown-internal-id", "one"):
        with pytest.raises(PermissionError):
            proposals.publish_proposal({**payload, "evidence_message_ids": [bad_id]})


def test_new_evidence_after_acceptance_creates_related_proposal(services):
    messages, matters, proposals = services
    first = candidate(services)
    result = proposals.decide(first["proposal_id"], decision(first), principal=HUMAN)
    messages.import_messages(
        [
            {
                "platform": "mock",
                "account_id": "account",
                "message_id": "correction",
                "conversation_type": "group",
                "conversation_id": "group",
                "text": "Deadline explicitly changed",
                "received_at": 300,
                "sent_at": 250,
            }
        ]
    )
    evidence_id = messages.recent()["messages"][0]["message_id"]
    changed = candidate(services, evidence_message_ids=[evidence_id])
    assert changed["proposal_id"] != first["proposal_id"]
    assert changed["related_proposal_id"] == first["proposal_id"]
    assert changed["state"] == "pending"
    assert matters.get_matter(matter_id=result["matter_id"]).revision == 1


def test_reviewed_deadline_bounds_and_analysis_toggle_fence(services):
    messages, _, proposals = services
    preview = candidate(services)
    with pytest.raises(ValidationError):
        DECISION_ADAPTER.validate_python(
            decision(
                preview,
                reviewed_fields={**preview["proposed_fields"], "due_at": "2026-10-05T10:00:00"},
            )
        )
    with pytest.raises(ValidationError):
        DECISION_ADAPTER.validate_python(
            decision(preview, reviewed_fields={**preview["proposed_fields"], "summary": "x" * 8001})
        )
    policy_change(messages, analysis_enabled=False)
    policy_change(messages, analysis_enabled=True)
    with pytest.raises(PermissionError, match="frozen"):
        proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)


def test_reviewed_fields_match_saved_normalization_and_canonical_replay(services):
    _, matters, proposals = services
    preview = candidate(services, summary="", tags=["Report", "report"])
    assert preview["proposed_fields"]["summary"] == preview["proposed_fields"]["title"]
    assert preview["proposed_fields"]["tags"] == ["Report"]
    payload = decision(
        preview,
        reviewed_fields={
            **preview["proposed_fields"],
            "summary": "   ",
            "tags": [" Report ", "report"],
        },
    )
    saved = proposals.decide(preview["proposal_id"], payload, principal=HUMAN)
    assert proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN) == saved
    record = matters.get_matter(matter_id=saved["matter_id"])
    assert record.summary == preview["proposed_fields"]["summary"]
    assert record.tags == preview["proposed_fields"]["tags"]


def test_proposal_feed_conversation_and_time_filters_bind_cursor(services):
    messages, _, proposals = services
    first = candidate(services)
    candidate(services, action_key="second-action")
    other = messages.set_policy(
        {
            "platform": "mock",
            "account_id": "account",
            "conversation_type": "group",
            "conversation_id": "other",
            "record_enabled": True,
            "analysis_enabled": True,
            "proposals_enabled": True,
        }
    )
    messages.import_messages(
        [
            {
                "platform": "mock",
                "account_id": "account",
                "message_id": "other-message",
                "conversation_type": "group",
                "conversation_id": "other",
                "text": "Other group task",
                "received_at": 400,
            }
        ]
    )
    message_id = messages.recent(conversation_key=other["conversation_key"])["messages"][0][
        "message_id"
    ]
    proposals.publish_proposal(
        {"title": "Other group", "action_key": "other", "evidence_message_ids": [message_id]}
    )
    assert (
        len(proposals.list_proposals(conversation_key=first["conversation_key"])["proposals"]) == 2
    )
    assert (
        len(proposals.list_proposals(conversation_key=other["conversation_key"])["proposals"]) == 1
    )
    assert not proposals.list_proposals(until="2000-01-01T00:00:00Z")["proposals"]
    assert (
        len(
            proposals.list_proposals(
                since="2000-01-01T00:00:00+08:00", until="2100-01-01T00:00:00Z"
            )["proposals"]
        )
        == 3
    )
    for filters in (
        {"since": "2000-01-01T00:00:00"},
        {"since": "2100-01-01T00:00:00Z", "until": "2000-01-01T00:00:00Z"},
    ):
        with pytest.raises(ValueError):
            proposals.list_proposals(**filters)
    page = proposals.list_proposals(conversation_key=first["conversation_key"], limit=1)
    with pytest.raises(ValueError, match="scope_changed"):
        proposals.list_proposals(
            conversation_key=other["conversation_key"], cursor=page["next_cursor"]
        )
