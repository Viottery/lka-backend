"""Small isolated replay/privacy checks, no real messages or provider calls."""

import json
import sqlite3

import pytest

from app.domains.message_reading_replay import (
    build_windows,
    create_snapshot,
    digest,
    load_snapshot,
    redact_text,
    validate_grant,
    validate_windows,
)
from app.tool_packages.message_replay_analysis import ReplayAnalysis, validate_evidence


def test_local_audit_roundtrip_duplicate_and_simulated_cold_cleanup():
    from app.core.prompt_tokens import PromptTokenCounter
    from app.domains.message_reading_codec import FIELDS
    from scripts.replay_message_reading import audit_history

    defaults = dict(zip(FIELDS, ["m1", "p1", 1, 100, 100, "hello", "text", [], None, {}, []]))
    rows = [{**defaults, "id": f"m{i}", "seq": i + 1,
             "sent_at": 100 + i * 10800, "received_at": 100 + i * 10800} for i in range(3)]
    result = audit_history({"c1": rows}, PromptTokenCounter())
    group = result["groups"]["c1"]
    assert group["idempotent"] and group["codec_roundtrip"]
    assert group["hot_people"] == 1
    assert group["simulated_days_without_new_input"]["14"]["hot_people"] == 0
    assert group["simulated_days_without_new_input"]["31"]["remaining_activity_people"] == 0
    assert result["remote_calls"] == 0 and result["production_approval"] is False


def source_database(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE message_history_policies(conversation_key TEXT,platform TEXT,
                account_id TEXT,conversation_type TEXT,conversation_id TEXT,
                record_enabled INTEGER,capture_epoch INTEGER);
            CREATE TABLE message_history_messages(internal_message_id TEXT,provider_message_id TEXT,
                conversation_key TEXT,seq INTEGER,sender_id TEXT,sender_name TEXT,text TEXT,
                content_kind TEXT,sent_at INTEGER,received_at INTEGER,timestamp_quality TEXT,
                metadata_json TEXT);
        """)
        conn.executemany(
            "INSERT INTO message_history_policies VALUES(?,?,?,?,?,?,?)",
            [
                ("group-key", "qq", "12345678", "group", "87654321", 1, 2),
                ("private-key", "qq", "12345678", "private", "99887766", 1, 1),
            ],
        )
        conn.executemany(
            "INSERT INTO message_history_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "internal-1",
                    "provider-1",
                    "group-key",
                    1,
                    "11223344",
                    "name",
                    "我喜欢Python",
                    "text",
                    90,
                    100,
                    "valid",
                    "{}",
                ),
                (
                    "internal-2",
                    "provider-2",
                    "group-key",
                    2,
                    "55667788",
                    "name",
                    "明天回复11223344",
                    "text",
                    100,
                    110,
                    "valid",
                    json.dumps(
                        {
                            "reply_to_message_id": "provider-1",
                            "metadata_capabilities": {"reply": "supported"},
                        }
                    ),
                ),
                (
                    "internal-private",
                    "private-provider",
                    "private-key",
                    1,
                    "99887766",
                    "name",
                    "PRIVATE SECRET",
                    "text",
                    90,
                    100,
                    "valid",
                    "{}",
                ),
                (
                    "internal-late",
                    "provider-late",
                    "group-key",
                    3,
                    "11223344",
                    "name",
                    "FUTURE MESSAGE",
                    "text",
                    200,
                    200,
                    "valid",
                    "{}",
                ),
            ],
        )


def test_snapshot_is_filtered_fixed_and_aliased(tmp_path):
    source = tmp_path / "production.sqlite"
    source_database(source)
    output = tmp_path / "snapshot"
    manifest = create_snapshot(
        source,
        output,
        platform="qq",
        account_id="12345678",
        group_ids=["87654321"],
        until=110,
        consent_at="test",
    )
    assert manifest["count"] == 2
    loaded, rows = load_snapshot(output)
    text = (output / "messages.json").read_text()
    assert "PRIVATE SECRET" not in text and "FUTURE MESSAGE" not in text
    assert "11223344" not in text and "internal-1" not in text
    assert rows["c1"][1]["reply"] == rows["c1"][0]["id"]
    assert rows["c1"][0]["timestamp_quality"] == "valid"
    assert loaded == manifest
    mapping = json.loads((output / "identity_mapping.json").read_text())
    assert mapping["c1"]["display_names"][rows["c1"][0]["sender"]] == "name"
    assert "display_names" not in (output / "messages.json").read_text()
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_history_messages").fetchone()[0] == 4
    with pytest.raises(ValueError, match="destination"):
        create_snapshot(
            source,
            output,
            platform="qq",
            account_id="12345678",
            group_ids=["87654321"],
            until=110,
            consent_at="test",
        )


def test_snapshot_permission_and_digest(tmp_path):
    source = tmp_path / "production.sqlite"
    source_database(source)
    with pytest.raises(ValueError, match="authorized"):
        create_snapshot(
            source,
            tmp_path / "denied",
            platform="qq",
            account_id="12345678",
            group_ids=["no-such-group"],
            until=110,
            consent_at="test",
        )
    output = tmp_path / "snapshot"
    create_snapshot(
        source,
        output,
        platform="qq",
        account_id="12345678",
        group_ids=["87654321"],
        until=110,
        consent_at="test",
    )
    (output / "messages.json").write_text("{}")
    with pytest.raises(ValueError, match="digest"):
        load_snapshot(output)


def test_redaction_preserves_dates_and_negation():
    value = redact_text(
        "11223344说不是2026-10-05，是明天；13812345678 a@example.com https://example.com/private?token=secret",
        {"11223344": "p1"},
    )
    assert value.startswith("p1说不是2026-10-05，是明天")
    assert "secret" not in value and "13812345678" not in value and "example.com" not in value
    assert "[phone]" in value and "[email]" in value and "[link]" in value


def test_reply_connected_windows_do_not_leak_between_splits():
    rows = [{"id": f"m{i}", "seq": i, "reply": "m3" if i == 5 else None} for i in range(1, 25)]
    windows = build_windows({"c1": rows}, size=4)
    owners = {row["id"]: window["window_id"] for window in windows for row in window["messages"]}
    assert owners["m3"] == owners["m5"]
    ids = [row["id"] for window in windows for row in window["messages"]]
    assert len(ids) == len(set(ids))
    assert {window["split"] for window in windows} == {"development", "holdout"}


def test_grant_checks_provider_scope_epoch_and_revocation():
    manifest = {"snapshot_id": "snapshot"}
    mapping = {"c1": {"source": {"conversation_key": "key", "capture_epoch": 2}}}
    grant = {
        "snapshot_id": "snapshot",
        "active": True,
        "provider_hash": "provider",
        "expires_at": 200,
        "conversation_keys": ["key"],
        "manifest_digest": digest(manifest),
        "mapping_digest": digest(mapping),
    }
    policies = [{"conversation_key": "key", "capture_epoch": 2, "record_enabled": True}]
    validate_grant(grant, manifest, "provider", policies, mapping, now=100)
    for patch in (
        {"active": False},
        {"provider_hash": "changed"},
        {"expires_at": 100},
        {"conversation_keys": []},
        {"manifest_digest": "changed"},
        {"mapping_digest": "changed"},
    ):
        with pytest.raises(ValueError):
            validate_grant({**grant, **patch}, manifest, "provider", policies, mapping, now=100)
    for patch in ({"record_enabled": False}, {"capture_epoch": 3}):
        with pytest.raises(ValueError, match="permission"):
            validate_grant(
                grant, manifest, "provider", [{**policies[0], **patch}], mapping, now=100
            )


def test_report_does_not_hide_cancelled_usage_or_claim_semantic_scores():
    from scripts.replay_message_reading import _json_fence, _report_summary

    ledger = [
        {
            "count_method": "provider_usage",
            "input_tokens": 100,
            "output_tokens": 20,
            "cost": 0,
            "cost_known": 0,
        },
        {
            "count_method": "conservative_estimate",
            "input_tokens": 110,
            "output_tokens": 40,
            "cost": 0,
            "cost_known": 0,
        },
    ]
    result = _report_summary([{"state": "completed"}], ledger)
    assert result["admitted_calls"] == 2 and result["recorded_responses"] == 1
    assert result["provider_reported_tokens"] == 120
    assert result["ledger_accounted_tokens"] == 270
    assert result["cost"] is None and result["critical_event_recall"] is None
    assert result["semantic_quality_verified"] is False
    assert _json_fence({"text": "```fake instructions"}).startswith("````json\n")


def test_failed_output_rechecks_permission_before_retaining_body():
    from types import SimpleNamespace

    from scripts.replay_message_reading import _authorized_failure_response

    response = SimpleNamespace(content="PRIVATE MODEL OUTPUT")

    def revoked():
        raise ValueError("PRIVATE ERROR BODY")

    assert _authorized_failure_response(response, revoked) == (None, False)
    assert _authorized_failure_response(response, lambda: None) == (response.content, True)


def test_window_metadata_and_future_range_cannot_be_changed():
    rows = [{"id": f"m{i}", "seq": i, "reply": None} for i in range(1, 17)]
    windows = build_windows({"c1": rows}, size=4)
    fixed = digest(windows)
    validate_windows(windows, {"c1": rows}, fixed)
    changed = json.loads(json.dumps(windows))
    changed[0]["range"][1] = 999
    with pytest.raises(ValueError):
        validate_windows(changed, {"c1": rows}, fixed)
    with pytest.raises(ValueError):
        validate_windows(changed, {"c1": rows}, digest(changed))
    changed[0]["range"] = windows[0]["range"]
    changed[0]["split"] = "holdout" if windows[0]["split"] == "development" else "development"
    with pytest.raises(ValueError):
        validate_windows(changed, {"c1": rows}, fixed)


def test_schema_and_author_evidence_are_checked():
    result = ReplayAnalysis.model_validate(
        {
            "schema_version": 3,
            "topics": [],
            "highlights": [],
            "importance": [],
            "participant_claim_candidates": [
                {
                    "sender": "p2",
                    "kind": "preference",
                    "text": "我喜欢Python",
                    "source_ids": ["m1"],
                    "quote": "我喜欢Python",
                    "basis": "explicit",
                    "valid_until": None,
                }
            ],
            "focus_candidates": [],
            "warnings": [],
        }
    )
    assert validate_evidence(result, [{"id": "m1", "sender": "p1", "text": "我喜欢Python"}]) == [
        "participant_author_mismatch"
    ]
    assert validate_evidence(result, []) == ["unknown_evidence"]
    with pytest.raises(ValueError):
        ReplayAnalysis.model_validate({**result.model_dump(), "approved": True})


def test_replay_output_cannot_point_at_production(monkeypatch, tmp_path):
    from scripts import replay_message_reading as runner

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="private"):
        runner._safe_destination(str(tmp_path / "data/runtime/lka.sqlite3"))
    assert (
        runner._safe_destination(str(tmp_path / "data/message_replay/test"))
        == tmp_path / "data/message_replay/test"
    )
