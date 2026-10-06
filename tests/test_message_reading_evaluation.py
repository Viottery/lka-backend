"""Offline local badcase checks with synthetic quotes and temporary SQLite."""

import pytest
from pydantic import ValidationError

from app.core.background_jobs import BackgroundJobStore
from app.domains.message_history import MessageHistoryService
from app.domains.message_matter_proposals import HumanControlPrincipal
from app.domains.message_reading_evaluation import (
    BadcaseConflict,
    BadcaseInput,
    MessageReadingEvaluationService,
)
from app.storage.db import init_db

HUMAN = HumanControlPrincipal("paired-local-ui")


@pytest.fixture
def evaluation(tmp_path):
    path = tmp_path / "badcases.sqlite3"
    init_db(path)
    history = MessageHistoryService(path, BackgroundJobStore(path))
    history.ensure_schema()
    policy = history.set_policy({"platform": "mock", "account_id": "account",
        "conversation_type": "group", "conversation_id": "group", "record_enabled": True})
    history.import_messages([{"platform": "mock", "account_id": "account", "message_id": "synthetic-" + str(index),
        "conversation_type": "group", "conversation_id": "group", "sender_name": "Synthetic",
        "text": "Synthetic deadline correction " + str(index), "sent_at": 100 + index,
        "received_at": 200 + index} for index in range(2)])
    service = MessageReadingEvaluationService(history)
    ids = [row["message_id"] for row in history.recent()["messages"]]
    return history, service, ids, policy


def payload(service, ids, **changes):
    preview = service.preview(ids, principal=HUMAN)
    return {"evidence_message_ids": ids, "expected_evidence_digest": preview["evidence_digest"],
            "label": "deadline_correction", "note": "Human marked a wrong due date",
            "local_copy_consent": True, **changes}


def toggle(history, enabled):
    prior = history.list_policies()[0]
    return history.set_policy({"platform": prior["platform"], "account_id": prior["account_id"],
        "conversation_type": prior["conversation_type"], "conversation_id": prior["conversation_id"],
        "expected_revision": prior["revision"], "record_enabled": enabled})


def test_exact_preview_explicit_copy_and_stable_dedup(evaluation):
    history, service, ids, _ = evaluation
    preview = service.preview(ids, principal=HUMAN)
    assert len(preview["evidence"]) == 2
    assert preview["local_copy_requires_consent"] is True
    data = payload(service, ids)
    saved = service.save(data, principal=HUMAN)
    replay = service.save({**data, "evidence_message_ids": list(reversed(ids)) + [ids[0]]}, principal=HUMAN)
    assert replay == saved
    assert saved["evidence"] == preview["evidence"]
    assert saved["local_only"] is True
    with history._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_badcases").fetchone()[0] == 1


def test_consent_and_fields_are_strict(evaluation):
    _, service, ids, _ = evaluation
    data = payload(service, ids)
    for changes in ({"local_copy_consent": False}, {"local_copy_consent": 1}, {"label": "auto_train"},
                    {"note": "x" * 2001}, {"uploaded": True}, {"evidence_message_ids": ids * 11}):
        with pytest.raises(ValidationError):
            service.save({**data, **changes}, principal=HUMAN)
    without = dict(data)
    del without["local_copy_consent"]
    with pytest.raises(ValidationError):
        BadcaseInput.model_validate(without)
    constructed = BadcaseInput.model_construct(**{**data, "local_copy_consent": False})
    with pytest.raises(ValidationError):
        service.save(constructed, principal=HUMAN)


def test_human_identity_is_required_on_every_operation(evaluation):
    _, service, ids, _ = evaluation
    data = payload(service, ids)
    record = service.save(data, principal=HUMAN)
    for principal in (None, {"kind": "human_control"}, HumanControlPrincipal(""), HumanControlPrincipal("import", "importer")):
        for action in (lambda principal=principal: service.preview(ids, principal=principal),
                       lambda principal=principal: service.save(data, principal=principal),
                       lambda principal=principal: service.get_badcase(record["badcase_id"], principal=principal),
                       lambda principal=principal: service.list_badcases(principal=principal)):
            with pytest.raises(PermissionError, match="human_control_identity_required"):
                action()


def test_real_ids_and_both_scope_grants_are_required(evaluation):
    _, service, ids, policy = evaluation
    for ids_value, scope in ((["synthetic-0"], {}), (ids, {"allowed_sources": []}),
                            (ids, {"allowed_accounts": []}), (ids, {"allowed_sources": ["other"]}),
                            (ids, {"allowed_accounts": ["other"]})):
        with pytest.raises(PermissionError):
            service.preview(ids_value, principal=HUMAN, **scope)
    valid_scope = {"allowed_sources": [policy["source_id"]], "allowed_accounts": [policy["account_scope_id"]]}
    data = payload(service, ids)
    saved = service.save(data, principal=HUMAN, **valid_scope)
    with pytest.raises(PermissionError):
        service.get_badcase(saved["badcase_id"], principal=HUMAN, allowed_sources=[])
    assert service.list_badcases(principal=HUMAN, allowed_accounts=[])["badcases"] == []


def test_stale_digest_does_not_copy_any_quote(evaluation):
    history, service, ids, _ = evaluation
    with pytest.raises(BadcaseConflict):
        service.save(payload(service, ids, expected_evidence_digest="0" * 64), principal=HUMAN)
    with history._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_badcases").fetchone()[0] == 0


def test_revocation_is_opaque_and_regrant_invalidates_old_preview(evaluation):
    history, service, ids, _ = evaluation
    data = payload(service, ids)
    saved = service.save(data, principal=HUMAN)
    toggle(history, False)
    with pytest.raises(PermissionError):
        service.save(data, principal=HUMAN)
    with pytest.raises(PermissionError):
        service.get_badcase(saved["badcase_id"], principal=HUMAN)
    assert service.list_badcases(principal=HUMAN)["badcases"] == []
    toggle(history, True)
    with pytest.raises(BadcaseConflict):
        service.save(data, principal=HUMAN)
    with pytest.raises(PermissionError):
        service.get_badcase(saved["badcase_id"], principal=HUMAN)


def test_saved_copy_is_immutable_and_caller_transaction_owns_commit(evaluation):
    history, service, ids, _ = evaluation
    saved = service.save(payload(service, ids), principal=HUMAN)
    original = saved["evidence"][0]["text"]
    # Simulate repair of a source store; the explicitly saved copy stays unchanged.
    with history._connection() as conn:
        conn.execute("UPDATE message_history_messages SET text='changed source' WHERE internal_message_id=?", (saved["evidence"][0]["message_id"],))
    assert service.get_badcase(saved["badcase_id"], principal=HUMAN)["evidence"][0]["text"] == original
    data = payload(service, ids, note="Caller transaction")
    conn = history._connect()
    try:
        with pytest.raises(ValueError, match="caller_transaction"):
            service.save(data, principal=HUMAN, conn=conn)
        conn.execute("BEGIN IMMEDIATE")
        rolled_back = service.save(data, principal=HUMAN, conn=conn)
        assert conn.in_transaction
        conn.rollback()
    finally:
        conn.close()
    with pytest.raises(PermissionError):
        service.get_badcase(rolled_back["badcase_id"], principal=HUMAN)


def test_bounded_keyset_pages_and_cursor_scope_binding(evaluation):
    _, service, ids, _ = evaluation
    for note in ("First", "Second", "Third"):
        service.save(payload(service, ids, note=note), principal=HUMAN)
    cursor, seen = None, []
    for _ in range(3):
        page = service.list_badcases(principal=HUMAN, limit=1, cursor=cursor)
        seen.extend(item["badcase_id"] for item in page["badcases"])
        cursor = page["next_cursor"]
    assert len(set(seen)) == 3
    assert cursor is None
    first = service.list_badcases(principal=HUMAN, limit=1)
    for kwargs in ({"allowed_sources": []}, {"principal": HumanControlPrincipal("other-ui")}):
        with pytest.raises(ValueError, match="cursor"):
            service.list_badcases(**{"principal": HUMAN, "cursor": first["next_cursor"], **kwargs})
    for limit in (True, 0, 51):
        with pytest.raises(ValueError):
            service.list_badcases(principal=HUMAN, limit=limit)
