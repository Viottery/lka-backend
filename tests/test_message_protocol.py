"""R1 protocol/permission checks use synthetic messages and temporary SQLite only."""
import json
import sqlite3

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.domains.message_history import MessageHistoryService, PolicyRevisionConflict


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "protocol.sqlite"
    jobs = BackgroundJobStore(path)
    service = MessageHistoryService(path, jobs)
    service.ensure_schema()
    return service, jobs


def policy(service, prior=None, **changes):
    value = {"platform": "mock", "account_id": "a", "conversation_type": "group", "conversation_id": "c",
             "expected_revision": prior["revision"] if prior else 0}
    if prior:
        value.update({k: prior[k] for k in ("record_enabled", "analysis_enabled", "batch_size", "timezone")})
    value.update(changes)
    return service.set_policy(value)


def message(identifier="m", **changes):
    row = {"platform": "mock", "account_id": "a", "conversation_type": "group", "conversation_id": "c",
           "message_id": identifier, "received_at": 100, "text": "synthetic", "capture_epoch": 1,
           "adapter_id": "mock.reader", "adapter_version": "2", "metadata_capabilities": {}}
    row.update(changes)
    return row


def v1(identifier="m", **changes):
    row = message(identifier, **changes)
    for key in ("capture_epoch", "adapter_id", "adapter_version", "metadata_capabilities"):
        row.pop(key, None)
    return row


def test_new_defaults_and_old_partial_update_preserves_record_grant(store):
    service, _ = store
    p = policy(service)
    assert not any(p[k] for k in ("record_enabled", "analysis_enabled", "proposals_enabled"))
    assert p["local_signals_enabled"] and p["minimum_import_version"] == 1
    p = policy(service, p, record_enabled=True)
    p = service.set_policy({"platform": "mock", "account_id": "a", "conversation_type": "group",
                            "conversation_id": "c", "expected_revision": p["revision"], "batch_size": 30})
    assert p["record_enabled"]
    assert service.import_messages([v1()])["acknowledged"]


def test_split_epochs_and_cas(store):
    service, _ = store
    p = policy(service, record_enabled=True)
    q = policy(service, p, batch_size=4)
    assert q["schedule_revision"] == p["schedule_revision"] + 1
    assert q["capture_epoch"] == p["capture_epoch"]
    q = policy(service, q, analysis_enabled=True, proposals_enabled=True, timezone="UTC")
    assert q["analysis_epoch"] == 2 and q["proposals_epoch"] == 2 and q["processing_revision"] == 2
    with pytest.raises(PolicyRevisionConflict):
        policy(service, p)


def test_v2_native_metadata_and_legacy_unknown(store):
    service, _ = store
    p = policy(service, record_enabled=True)
    row = message(mentions=[{"kind": "user", "user_id": "a"}, {"kind": "all"}],
                  thread_id="thread", content_parts=[{"kind": "mention", "mention": {"kind": "all"}}],
                  metadata_capabilities={"mentions": "supported", "thread": "supported", "content_parts": "supported"})
    assert service.import_messages([row], schema_version=2)["acknowledged"]
    assert service.import_messages([v1("old", text="@a")])["acknowledged"]
    result = service.history(p["conversation_key"])["messages"]
    assert result[0]["mentions"] == [] and result[0]["metadata_capabilities"]["mentions"] == "unknown"
    assert result[1]["mentions"][0]["user_id"] == "a"


def test_v2_metadata_is_bounded_typed_and_capabilities_explicit(store):
    service, _ = store
    policy(service, record_enabled=True)
    for row in (message(capture_epoch=None), message(mentions=[{"kind": "user", "user_id": "a"}]),
                message(metadata_capabilities={"payload": {}}), message(content_parts=[{"kind": "text", "text": "x"}] * 101),
                message(adapter_id=""), message(mentions=[{"kind": "all", "user_id": "a"}])):
        assert service.import_messages([row], schema_version=2)["rejected"][0]["reason"] == "invalid_message"
    with pytest.raises(ValueError, match="byte bound"):
        service.import_messages([message(str(i), text="x" * 16384) for i in range(150)], schema_version=2)


def test_upgrade_is_explicit_and_old_entry_cannot_bypass_epoch(store):
    service, _ = store
    p = policy(service, record_enabled=True)
    assert service.import_messages([message()], schema_version=2)["acknowledged"]
    assert service.list_policies()[0]["minimum_import_version"] == 1
    assert service.import_messages([v1()])["acknowledged"]
    p = policy(service, p, minimum_import_version=2)
    assert service.import_messages([v1("old")])["rejected"][0]["reason"] == "import_version_required"
    with pytest.raises(ValueError, match="downgraded"):
        policy(service, p, minimum_import_version=1)


def test_reenable_does_not_admit_old_offline_epoch(store):
    service, _ = store
    p = policy(service, record_enabled=True, minimum_import_version=2)
    p = policy(service, p, record_enabled=False)
    p = policy(service, p, record_enabled=True)
    assert p["capture_epoch"] == 3
    assert service.import_messages([message()], schema_version=2)["rejected"][0]["reason"] == "capture_epoch_conflict"
    assert service.import_messages([message(capture_epoch=3)], schema_version=2)["acknowledged"]


def test_metadata_identity_immutable_and_v1_retry_does_not_erase(store):
    service, _ = store
    policy(service, record_enabled=True)
    row = message(reply_to_message_id="missing", metadata_capabilities={"reply": "supported"})
    assert service.import_messages([row], schema_version=2)["acknowledged"]
    assert service.import_messages([v1()])["acknowledged"]
    assert service.import_messages([{**row, "reply_to_message_id": "changed"}], schema_version=2)["rejected"][0]["reason"] == "identity_conflict"
    assert service.recent()["messages"][0]["reply_to_message_id"] == "missing"


def test_reply_resolution_never_crosses_conversation_or_account(store):
    service, _ = store
    p = policy(service, record_enabled=True)
    policy(service, record_enabled=True, conversation_id="other")
    policy(service, record_enabled=True, account_id="other")
    service.import_messages([message("target", conversation_id="other"), message("own", account_id="other"),
                             message("reply", reply_to_message_id="target", metadata_capabilities={"reply": "supported"}),
                             message("reply2", reply_to_message_id="own", metadata_capabilities={"reply": "supported"})], schema_version=2)
    assert all(r["reply_resolution"] == "unresolved" for r in service.history(p["conversation_key"])["messages"])
    service.import_messages([message("own")], schema_version=2)
    reply = next(r for r in service.history(p["conversation_key"])["messages"] if r["provider_message_id"] == "reply2")
    assert reply["reply_resolution"] == "resolved"


def test_source_resolver_requires_real_internal_id_and_both_grants(store):
    service, _ = store
    p = policy(service, record_enabled=True)
    service.import_messages([v1()])
    internal = service.recent()["messages"][0]["message_id"]
    assert service.resolve_message_source(internal, allowed_sources=[p["source_id"]], allowed_accounts=[p["account_scope_id"]])["source_type"] == "message_history_message"
    for ident, scopes in (("m", {}), (internal, {"allowed_sources": []}), (internal, {"allowed_accounts": []})):
        with pytest.raises(PermissionError):
            service.resolve_message_source(ident, **scopes)
    for scopes in ({"allowed_sources": []}, {"allowed_accounts": []}):
        with pytest.raises(PermissionError):
            service.coverage(p["conversation_key"], **scopes)
    policy(service, p, record_enabled=False)
    with pytest.raises(PermissionError):
        service.resolve_message_source(internal)


def test_schedule_change_keeps_running_batch_but_permissions_fence(store):
    service, jobs = store
    p = policy(service, record_enabled=True, analysis_enabled=True, batch_size=1)
    service.import_messages([v1()])
    job = jobs.claim("worker", 30, kinds=("message_analysis",))
    q = policy(service, p, batch_size=3)
    assert service.load_analysis_batch(job)
    assert service.list_conversations()["conversations"][0]["analysis_job"]["job_id"] == job["job_id"]
    assert service.summary(q["conversation_key"])["analysis_job"]["job_id"] == job["job_id"]
    assert service.publish_analysis(job, {"batch_summary": "mock", "summary": "mock"})
    service.import_messages([v1("m2")])
    service.schedule_pending(q["conversation_key"], force=True)
    job2 = jobs.claim("worker", 30, kinds=("message_analysis",))
    q = policy(service, q, analysis_enabled=False)
    q = policy(service, q, analysis_enabled=True)
    assert service.load_analysis_batch(job2) is None
    assert not service.publish_analysis(job2, {"batch_summary": "stale", "summary": "stale"})


def test_additive_migration_retains_rows_grants_and_legacy_job(store):
    service, jobs = store
    p = policy(service, record_enabled=True, analysis_enabled=True, batch_size=1)
    service.import_messages([v1()])
    job = jobs.list(kind="message_analysis")[0]
    with sqlite3.connect(service.db_path) as conn:
        payload = json.loads(conn.execute("SELECT payload_json FROM background_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0])
        for name in ("capture_epoch", "analysis_epoch", "processing_revision"):
            payload.pop(name)
        conn.execute("UPDATE background_jobs SET payload_json=? WHERE job_id=?", (json.dumps(payload), job["job_id"]))
        for name in ("capture_epoch", "analysis_epoch", "proposals_epoch", "processing_revision", "schedule_revision", "proposals_enabled", "local_signals_enabled", "minimum_import_version", "processing_schema_version"):
            conn.execute(f"ALTER TABLE message_history_policies DROP COLUMN {name}")
    service.ensure_schema()
    p = service.list_policies()[0]
    assert p["record_enabled"] and p["minimum_import_version"] == 1
    assert service.recent()["messages"][0]["provider_message_id"] == "m"
    p = policy(service, p, batch_size=8)
    leased = jobs.claim("worker", 30, kinds=("message_analysis",))
    assert service.load_analysis_batch(leased)
    coverage = service.coverage(p["conversation_key"])
    assert coverage["pipeline_version"] == "legacy-v1" and coverage["legacy_only"]
    assert coverage["analysis_covered_seq"] is None and coverage["baseline_start_seq"] is None
    assert not coverage["complete_for_platform"]


def test_v2_media_uses_capture_epoch_across_schedule_changes(store):
    service, _ = store
    p = policy(service, record_enabled=True, media_enabled=True, minimum_import_version=2)
    service.import_messages([message(attachments=[{"ordinal": 0, "kind": "image"}])], schema_version=2)
    row = {"platform": "mock", "account_id": "a", "message_id": "m", "conversation_type": "group",
           "conversation_id": "c", "ordinal": 0, "state": "pending", "policy_revision": p["revision"],
           "capture_epoch": p["capture_epoch"]}
    p = policy(service, p, batch_size=7)
    assert service.update_media([row], schema_version=2)["acknowledged"]
    assert service.update_media([row])["rejected"][0]["reason"] == "import_version_required"
    p = policy(service, p, record_enabled=False)
    p = policy(service, p, record_enabled=True)
    assert service.update_media([row], schema_version=2)["rejected"][0]["reason"] == "policy_fenced"
