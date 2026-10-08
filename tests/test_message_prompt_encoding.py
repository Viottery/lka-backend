"""Offline codec transport and durable production resume contracts."""
import copy
import json

import pytest
from test_message_reading_production_v3 import Model, finish, insert, pipeline

from app.core.message_analysis import MessageAnalysisCoordinator
from app.domains.message_reading_codec import (
    decode_messages,
    decode_shared_defaults,
    encode_shared_defaults,
)
from app.tool_packages import message_reading_analysis as reading


class CodecModel(Model):
    def __init__(self, *, evidence=False, recover=False):
        super().__init__(evidence=evidence)
        self.recover = recover
        self.raw_requests = []
        self.systems = []

    async def complete_text(self, **kwargs):
        request = json.loads(kwargs["user_prompt"])
        self.raw_requests.append(copy.deepcopy(request))
        self.systems.append(kwargs["system_prompt"])
        if isinstance(request["messages"], dict):
            request["messages"] = decode_messages(request["messages"])
        elif "message_defaults" in request:
            request["messages"] = decode_shared_defaults(request)
        response = await super().complete_text(**{**kwargs, "user_prompt": json.dumps(request)})
        if self.recover and len(self.requests) == 1:
            response.content = "invalid JSON"
        return response


def checkpoint(service):
    with service._connection() as conn:
        row = conn.execute("SELECT * FROM message_reading_checkpoints").fetchone()
    return dict(row), json.loads(row["checkpoint_json"])


def test_codec_prompt_roundtrip_and_record_contract_unchanged():
    row = {"id": "m1f2", "sender": "u1", "seq": 5, "sent_at": None, "received_at": 1720000000,
           "text": '完整🙂\n"\\quote', "kind": "mixed", "mentions": ["all", "u2"],
           "reply": "unresolved", "capabilities": {"reply": "unknown"},
           "parts": [{"kind": "text", "text": "part text"}, {"kind": "image", "state": "unknown"}],
           "timestamp_quality": "unknown", "thread": None, "fragment_index": 2, "fragment_count": 3}
    context = {"known_topics": [{"topic_id": "q1", "title": "Known"}]}
    codec = json.loads(reading.prompt(context, [row], ["m1"], "codec_v2"))
    records = json.loads(reading.prompt(context, [row], ["m1"]))
    assert decode_messages(codec["messages"]) == records["messages"] == [row]
    assert {k: v for k, v in codec.items() if k != "messages"} == {
        k: v for k, v in records.items() if k != "messages"}
    assert reading.system_for_encoding() == reading.SYSTEM
    assert reading.versions_for_encoding() == reading.VERSIONS
    assert reading.versions_for_encoding("codec_v2")["prompt"] != reading.PROMPT_VERSION
    assert reading.versions_for_encoding("codec_v2")["codec"] == 2
    assert "slots 7,9,10,11" in reading.system_for_encoding("codec_v2")
    assert "part's text is t[text_index]" in reading.system_for_encoding("codec_v2")


@pytest.mark.parametrize("encoding", ["codec_v2", "compact_records"])
def test_codec_fitting_counts_exact_dispatched_payload_without_text_loss(tmp_path, encoding):
    service, store, policy, coordinator, model = pipeline(tmp_path, model=CodecModel(), message_encoding=encoding,
                                                         max_input_tokens=16384)
    text = "".join(f"line {i} 不同文字{i} abcdefghijklmnop\n" for i in range(400))
    insert(service, 1, text)
    counted = []
    counter = coordinator.counter.count_request

    def count(system, prompt):
        counted.append((system, prompt))
        return counter(system, prompt)

    coordinator.counter.count_request = count
    finish(service, policy, coordinator)
    assert store.list(status="succeeded"), store.list()
    assert len(model.raw_requests) > 1
    assert "".join(row["text"] for request in model.requests for row in request["messages"]) == text
    for system, request in zip(model.systems, model.raw_requests, strict=True):
        prompt = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert (system, prompt) in counted
        assert counter(system, prompt).count <= coordinator.config.max_input_tokens
        assert isinstance(request["messages"], dict if encoding == "codec_v2" else list)
        if encoding == "compact_records":
            assert all({"id", "sender", "seq", "text"} <= row.keys() for row in request["messages"])
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1
    serialized = json.dumps(model.raw_requests)
    assert "private-user" not in serialized and "private-message" not in serialized
    with service._connection() as conn:
        output = json.loads(conn.execute("SELECT output_json FROM message_reading_batch_outputs").fetchone()[0])
    assert output["prompt_version"] == reading.versions_for_encoding(encoding)["prompt"]
    assert output["reading_manifest"]["versions"] == reading.versions_for_encoding(encoding)
    assert output["reading_manifest"]["coverage_mode"] == "full_text"


@pytest.mark.parametrize("mode", ["evidence", "recover"])
@pytest.mark.parametrize("encoding", ["codec_v2", "compact_records"])
def test_codec_evidence_and_recovery_pin_encoding_family_usage_on_restart(tmp_path, mode, encoding):
    model = CodecModel(**{mode: True})
    service, store, policy, coordinator, _ = pipeline(tmp_path, model=model, message_encoding=encoding)
    insert(service, 1)
    service.schedule_pending(policy["conversation_key"], force=True)
    assert coordinator.worker.run_one()
    old_row, state = checkpoint(service)
    assert state["message_encoding"] == encoding
    assert state["versions"] == reading.versions_for_encoding(encoding)
    assert state["evidence_pending" if mode == "evidence" else "recovery_pending"]
    config = coordinator.config.model_copy(update={"message_encoding": "records"})
    restarted = MessageAnalysisCoordinator(service=service, store=store, config=config, llm_client=model)
    finish(service, policy, restarted)
    assert store.list(status="succeeded"), store.list()
    assert len(model.raw_requests) == 2
    assert all(isinstance(request["messages"], dict if encoding == "codec_v2" else list)
               for request in model.raw_requests)
    assert model.systems == [reading.system_for_encoding(encoding)] * 2
    assert restarted.controller.quota_usage(old_row["family_id"])["total_calls"] == 2
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0] == 1
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1
    if mode == "recover":
        assert model.raw_requests[1]["validation_feedback"] == [{"code": "invalid_json"}]
    else:
        assert model.requests[1]["messages"][0]["text"] == model.requests[0]["messages"][0]["text"]


@pytest.mark.parametrize("encoding", ["codec_v2", "compact_records"])
def test_old_checkpoint_without_encoding_resumes_identical_records_after_rollout(tmp_path, encoding):
    model = CodecModel()
    service, store, policy, coordinator, _ = pipeline(tmp_path, model=model)
    text = "".join(f"line {i} 不同文字{i} abcdefghijklmnop\n" for i in range(250))
    insert(service, 1, text)
    service.schedule_pending(policy["conversation_key"], force=True)
    assert coordinator.worker.run_one()
    old_row, state = checkpoint(service)
    assert json.loads(old_row["cursor_json"]) == 1
    state.pop("message_encoding")
    with service._connection() as conn:
        conn.execute("UPDATE message_reading_checkpoints SET checkpoint_json=? WHERE family_id=?",
                     (json.dumps(state), old_row["family_id"]))
    config = coordinator.config.model_copy(update={"message_encoding": encoding})
    restarted = MessageAnalysisCoordinator(service=service, store=store, config=config, llm_client=model)
    fingerprints = []
    save = service.save_reading_progress

    def capture(job, cursor, digest, checkpoint, next_cursor, epoch):
        fingerprints.append(checkpoint["input_snapshot_digest"])
        assert checkpoint["message_encoding"] == "records"
        return save(job, cursor, digest, checkpoint, next_cursor, epoch)

    service.save_reading_progress = capture
    finish(service, policy, restarted)
    assert store.list(status="succeeded"), store.list()
    assert all(isinstance(request["messages"], list) for request in model.raw_requests)
    assert model.systems == [reading.SYSTEM] * len(model.requests)
    assert "".join(row["text"] for request in model.requests for row in request["messages"]) == text
    assert all(fingerprint == state["input_snapshot_digest"] for fingerprint in fingerprints)
    assert restarted.controller.quota_usage(old_row["family_id"])["total_calls"] == len(model.requests)
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0] == 1
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1


def shared_rows():
    common = {"sent_at": None, "received_at": 1720000000, "kind": "text", "mentions": [],
              "reply": None, "capabilities": {"marker": 1}, "parts": [], "timestamp_quality": "unknown"}
    return [{**copy.deepcopy(common), "id": f"m{i}", "sender": f"u{i}", "seq": i,
             "text": f"distinct exact text {i}🙂"} for i in range(1, 4)]


def test_shared_defaults_roundtrip_preserves_optional_null_and_json_types():
    rows = shared_rows()
    rows[0]["thread"] = None
    rows[2]["thread"] = "t1"
    rows[0]["fragment_index"] = rows[1]["fragment_index"] = 1
    rows[2]["capabilities"] = {"marker": True}
    rows[2]["sent_at"] = 1719999900
    rows[2]["parts"] = [{"kind": "image", "state": "unknown", "size_bytes": None}]
    original = copy.deepcopy(rows)
    payload = encode_shared_defaults(rows)
    restored = decode_shared_defaults(payload)
    assert rows == original
    assert json.dumps(restored, sort_keys=True) == json.dumps(rows, sort_keys=True)
    assert "thread" not in payload["message_defaults"]
    assert "fragment_index" not in payload["message_defaults"]
    assert restored[0]["thread"] is None and "thread" not in restored[1]
    assert type(restored[0]["capabilities"]["marker"]) is int
    assert type(restored[2]["capabilities"]["marker"]) is bool
    assert payload["message_defaults"]["sent_at"] is None
    assert payload["messages"][2]["sent_at"] == 1719999900
    assert all({"id", "sender", "seq", "text"} <= row.keys() for row in payload["messages"])
    restored[0]["mentions"].append("u-other")
    assert restored[1]["mentions"] == [] and payload["message_defaults"]["mentions"] == []
    for invalid in (
        {"messages": payload["messages"], "message_defaults": {"text": "inherited"}},
        {**payload, "messages": [{k: v for k, v in payload["messages"][0].items() if k != "id"}]},
    ):
        with pytest.raises(ValueError):
            decode_shared_defaults(invalid)


def test_shared_defaults_prompt_keeps_current_source_text_binding():
    rows = shared_rows()
    payload = json.loads(reading.prompt({}, rows, [row["id"] for row in rows], "compact_records"))
    assert decode_shared_defaults(payload) == rows
    assert all(row["text"] == original["text"] and row["id"] == original["id"]
               and row["sender"] == original["sender"]
               for row, original in zip(payload["messages"], rows, strict=True))
    assert reading.system_for_encoding("compact_records").startswith(reading.SYSTEM)
    assert "including null, always overrides" in reading.system_for_encoding("compact_records")
    assert reading.versions_for_encoding("compact_records")["prompt"] != reading.CODEC_PROMPT_VERSION
    assert reading.versions_for_encoding()["prompt"] == reading.PROMPT_VERSION
    projection = reading.ScopedProjection("scope", [])
    projection.reverse = {row["id"]: f"original-{row['id']}" for row in rows}
    projection.senders = {row["sender"]: f"original-{row['sender']}" for row in rows}
    claims = [{"sender": rows[0]["sender"], "source_ids": [rows[0]["id"]],
               "quote": row["text"], "text": row["text"], "kind": "preference",
               "basis": "explicit"} for row in rows[:2]]
    verified = reading.verify_intelligence({"participant_claim_candidates": claims},
                                           decode_shared_defaults(payload), projection)
    assert len(verified["participant_claim_candidates"]) == 1
    assert verified["participant_claim_candidates"][0]["source_ids"] == ["original-m1"]
    assert verified["_rejected_candidate_counts"]["participant_claim_candidates"] == 1
