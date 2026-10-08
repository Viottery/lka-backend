import json
import stat
from types import SimpleNamespace

import pytest

from app.core.local_config import MessageHistoryConfig
from app.core.prompt_tokens import PromptTokenCounter
from scripts.compare_message_reading import (
    Ledger,
    prepare_arms,
    private_write,
    synthetic_window,
    validate_output,
)


def empty_output():
    return {"schema_version": 3, "topic_updates": [], "highlights": [], "importance_findings": [],
            "facts": [], "warnings": [], "participant_claim_candidates": [], "focus_candidates": [], "evidence_requests": []}


def test_private_artifacts_and_durable_budget(tmp_path):
    path = tmp_path / "artifact.json"
    private_write(path, {"ok": True})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        private_write(path, {})
    ledger = Ledger(tmp_path / "usage.sqlite3", max_calls=2, max_tokens=30)
    key = ledger.reserve(10)
    ledger.finish(key, "failed", {})
    ledger.reserve(10)
    with pytest.raises(ValueError, match="budget"):
        Ledger(tmp_path / "usage.sqlite3", max_calls=2, max_tokens=30).reserve(1)


def test_native_production_arms_and_schema_provenance():
    arms = prepare_arms(synthetic_window(), SimpleNamespace(message_history=MessageHistoryConfig(max_input_tokens=30000)), PromptTokenCounter())
    assert [arm["arm"] for arm in arms] == ["current_records", "current_codec_v2", "candidate_codec_v2"]
    assert all(not arm["metrics"]["missed_critical_evidence"] for arm in arms)
    assert isinstance(json.loads(arms[0]["requests"][0]["prompt"])["messages"], list)
    assert json.loads(arms[1]["requests"][0]["prompt"])["messages"]["v"] == 2
    arm = arms[0]
    request = arm["requests"][0]
    assert validate_output(empty_output(), request["fragments"], arm["projection"], arm["context"])["source_valid"]
    output = empty_output()
    output["facts"] = [{"kind": "fact", "text": "fabricated", "source_message_ids": ["outside"], "certainty": "explicit"}]
    with pytest.raises(ValueError, match="source_not"):
        validate_output(output, request["fragments"], arm["projection"], arm["context"])
    with pytest.raises(ValueError, match="envelope"):
        validate_output({}, request["fragments"], arm["projection"], arm["context"])


def test_readonly_and_capture_consent_revalidation(tmp_path):
    import sqlite3

    from scripts.compare_message_reading import read_connection, validate_consent

    path = tmp_path / "production.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE message_reading_control(service TEXT, paused INTEGER); "
                           "INSERT INTO message_reading_control VALUES ('message_reading',0); "
                           "CREATE TABLE message_history_policies(conversation_key TEXT, record_enabled INTEGER, "
                           "analysis_enabled INTEGER,capture_epoch INTEGER,analysis_epoch INTEGER, "
                           "processing_revision INTEGER,revision INTEGER); "
                           "INSERT INTO message_history_policies VALUES('scope',1,1,1,2,3,4);")
    policy = {"conversation_key": "scope", "capture_epoch": 1, "analysis_epoch": 2,
              "processing_revision": 3, "revision": 4}
    validate_consent(path, [policy])
    with read_connection(path) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE message_history_policies SET capture_epoch=2")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE message_history_policies SET capture_epoch=2")
    with pytest.raises(PermissionError, match="consent_or_capture"):
        validate_consent(path, [policy])


def test_provider_usage_above_reservation_still_charged(tmp_path):
    ledger = Ledger(tmp_path / "usage.sqlite3", max_calls=3, max_tokens=30)
    key = ledger.reserve(10)
    ledger.finish(key, "completed", {"prompt_tokens": 15, "completion_tokens": 10})
    with pytest.raises(ValueError, match="budget"):
        ledger.reserve(6)


def test_schema_and_intelligence_reject_bad_quote():
    arm = prepare_arms(synthetic_window(), SimpleNamespace(message_history=MessageHistoryConfig(max_input_tokens=30000)), PromptTokenCounter())[0]
    fragment = arm["requests"][0]["fragments"][0]
    output = empty_output()
    output["participant_claim_candidates"] = [{"sender": fragment["sender"], "source_ids": [fragment["id"]],
        "quote": "words never authored", "kind": "preference", "text": "words never authored", "basis": "explicit"}]
    result = validate_output(output, arm["requests"][0]["fragments"], arm["projection"], arm["context"])
    assert result["rejected_candidate_counts"]["participant_claim_candidates"] == 1
    assert result["source_valid"]
    output = empty_output()
    output["facts"] = [{"kind": "invented_kind", "text": "claim", "source_message_ids": [fragment["id"]], "certainty": "explicit"}]
    with pytest.raises(ValueError):
        validate_output(output, arm["requests"][0]["fragments"], arm["projection"], arm["context"])


def test_resume_reuses_failed_call_and_checks_snapshot_route_and_request(tmp_path):
    from app.domains.message_reading_replay import digest
    from scripts.compare_message_reading import load_resume

    window = synthetic_window()
    arms = prepare_arms(window, SimpleNamespace(message_history=MessageHistoryConfig(max_input_tokens=30000)), PromptTokenCounter())
    arm = arms[0]
    usage = {"prompt_tokens": 20, "completion_tokens": 10}
    ledger = Ledger(tmp_path / "usage.sqlite3")
    key = ledger.reserve(100)
    ledger.finish(key, "failed", usage)
    artifact = {"request_digest": digest(arm["requests"][0]["prompt"]), "client": "route", "model": "configured",
                "state": "failed", "usage": usage}
    private_write(tmp_path / "synthetic-contract-current_records-0.json", artifact)
    report = {"snapshot_digest": "snapshot", "route": {"client": "route", "model": "configured"},
              "config_digest": "configuration", "arms": [a["metrics"] for a in arms]}
    report["arms"][0]["calls"] = [{"state": "failed", "input_estimate": arm["requests"][0]["input_estimate"],
                                   "provider_usage": usage, "ledger_call_id": key, "artifact_digest": digest(artifact)}]
    private_write(tmp_path / "report.json", report)
    current = {"snapshot_digest": "snapshot", "route": report["route"]}
    reused = load_resume(tmp_path / "report.json", current, [(window, arms)], ledger, "configuration")
    assert reused[("synthetic-contract", "current_records", 0)]["state"] == "failed"
    assert ledger.conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
    with pytest.raises(ValueError, match="configuration"):
        load_resume(tmp_path / "report.json", current, [(window, arms)], ledger, "changed")
    artifact["request_digest"] = "tampered"
    (tmp_path / "synthetic-contract-current_records-0.json").write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="artifact_or_request"):
        load_resume(tmp_path / "report.json", current, [(window, arms)], ledger, "configuration")


def test_compact_variant_forces_current_selection_even_with_gray_config():
    from app.domains.message_reading_codec import decode_shared_defaults

    config = SimpleNamespace(message_history=MessageHistoryConfig(
        max_input_tokens=30000, selector_algorithm="multi_lane", selector_rollout_percent=100))
    window = synthetic_window()
    current = prepare_arms(window, config, PromptTokenCounter(), ["current_records"])[0]
    compact = prepare_arms(window, config, PromptTokenCounter(), ["current_compact_records"])[0]
    assert compact["selection"] == current["selection"]
    assert len(compact["selection"]["protected_ids"]) == 5
    assert any("native_reply" in decision["reasons"] for decision in compact["selection"]["decisions"])
    assert compact["arm"] == "current_compact_records"
    payload = json.loads(compact["requests"][0]["prompt"])
    assert all({"id", "sender", "text"} <= row.keys() for row in payload["messages"])
    assert decode_shared_defaults(payload) == compact["requests"][0]["fragments"]


def test_explicit_subset_rejects_unknown_windows_and_arms_before_provider_setup():
    from scripts.compare_message_reading import choose_windows

    window = synthetic_window()
    assert choose_windows([window], ["synthetic-contract"]) == [window]
    with pytest.raises(ValueError, match="unknown_window"):
        choose_windows([window], ["not_authorized"])
    config = SimpleNamespace(message_history=MessageHistoryConfig())
    with pytest.raises(ValueError, match="invalid_arm"):
        prepare_arms(window, config, PromptTokenCounter(), ["unknown_arm"])
