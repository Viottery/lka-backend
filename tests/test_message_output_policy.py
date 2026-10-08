"""Offline output-style, reservation and frozen-checkpoint contracts."""
import asyncio
import json

import pytest
from test_message_prompt_encoding import CodecModel, checkpoint
from test_message_reading_production_v3 import finish, insert, pipeline

from app.core.local_config import MessageHistoryConfig
from app.core.message_analysis import MessageAnalysisCoordinator
from app.tool_packages import message_reading_analysis as reading


def direct_dispatch(coordinator, calls):
    # Isolate the reading/checkpoint contract from shared resource admission.
    # This adapter does not test provider semantics or usage-ledger accounting.
    def call(client, kwargs, incoming):
        calls.append({"system": kwargs["system_prompt"], "output": kwargs["max_output_tokens"],
                      "incoming": incoming})
        return asyncio.run(client.complete_text(**kwargs))
    coordinator._call_once = call


@pytest.mark.parametrize("initial_style", ["standard", "concise"])
def test_recovery_keeps_frozen_style_cap_and_fingerprint_after_rollout(tmp_path, initial_style):
    model = CodecModel(recover=True)
    service, store, policy, coordinator, _ = pipeline(tmp_path, model=model,
        message_encoding="compact_records", reading_output_style=initial_style,
        max_input_tokens=16384,
        generation_output_tokens=6500, recovery_output_tokens=6500, concise_output_tokens=8192)
    calls = []
    direct_dispatch(coordinator, calls)
    insert(service, 1)
    service.schedule_pending(policy["conversation_key"], force=True)
    assert coordinator.worker.run_one()
    old_row, state = checkpoint(service)
    assert state["recovery_pending"]
    if initial_style == "standard":
        # A genuine pre-rollout checkpoint has neither output-style nor cap markers.
        state.pop("output_token_limits")
        assert "output_style" not in state["versions"]
        with service._connection() as conn:
            conn.execute("UPDATE message_reading_checkpoints SET checkpoint_json=? WHERE family_id=?",
                         (json.dumps(state), old_row["family_id"]))
        config = coordinator.config.model_copy(update={"reading_output_style": "concise"})
    else:
        config = coordinator.config.model_copy(update={"reading_output_style": "standard",
            "concise_output_tokens": 4096, "generation_output_tokens": 4096,
            "recovery_output_tokens": 4096})
    restarted = MessageAnalysisCoordinator(service=service, store=store, config=config, llm_client=model)
    direct_dispatch(restarted, calls)
    finish(service, policy, restarted)
    assert store.list(status="succeeded"), store.list()
    _, saved = checkpoint(service)
    assert saved["input_snapshot_digest"] == state["input_snapshot_digest"]
    assert saved["versions"] == reading.versions_for_encoding("compact_records", initial_style)
    expected_cap = 8192 if initial_style == "concise" else 6500
    assert [call["output"] for call in calls] == [expected_cap, expected_cap]
    assert model.systems == [reading.system_for_encoding("compact_records", initial_style)] * 2
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1


def test_planner_reserves_concise_cap_for_generation_and_recovery(tmp_path):
    _, _, _, coordinator, _ = pipeline(tmp_path, reading_output_style="concise",
        concise_output_tokens=8192, generation_output_tokens=6500, recovery_output_tokens=6500)
    # An exact 100-token input, one chunk, and the existing two recovery reservations.
    coordinator.counter.count_request = lambda *args: type("Count", (), {"count": 100})()
    coordinator._v3_context = lambda *args: {}
    coordinator._v3_fragments = lambda *args: [[{"id": "m", "text": "test"}]]
    batch = {"conversation_key": "test", "family_id": "family", "policy": {},
             "messages": [{"message_id": "m", "received_at": 1, "seq": 1, "text": "test"}],
             "work_call_limit": 3}
    reserved = 100 + 8192 + 2 * (coordinator.config.max_input_tokens + 8192)
    assert coordinator._plan_v3_range({**batch, "work_token_limit": reserved - 1}) == 0
    assert coordinator._plan_v3_range({**batch, "work_token_limit": reserved}) == 1
    assert coordinator._plan_v3_range({**batch, "work_token_limit": reserved,
                                      "work_call_limit": 2}) == 0


def test_concise_output_still_rejects_unseen_evidence_and_bounds_repair(tmp_path):
    class UnseenEvidenceModel(CodecModel):
        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            value = json.loads(response.content)
            value["facts"] = [{"kind": "fact", "text": "Unsupported statement",
                               "source_message_ids": ["unseen-message"], "certainty": "explicit"}]
            response.content = json.dumps(value)
            return response

    service, store, policy, coordinator, _ = pipeline(tmp_path, model=UnseenEvidenceModel(),
        message_encoding="compact_records", reading_output_style="concise", max_input_tokens=16384)
    calls = []
    direct_dispatch(coordinator, calls)
    insert(service, 1)
    finish(service, policy, coordinator)
    assert len(calls) == 2
    assert store.list(status="failed")[0]["error_class"] == "model_recovery_exhausted"
    assert not store.list(status="succeeded")
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0


def test_concise_profile_preserves_historical_system_and_source_contracts():
    for encoding in ("records", "codec_v2", "compact_records"):
        assert reading.system_for_encoding(encoding, "concise") == (
            reading.system_for_encoding(encoding) + reading.CONCISE_INSTRUCTIONS)
        standard = reading.versions_for_encoding(encoding)
        concise = reading.versions_for_encoding(encoding, "concise")
        assert concise["prompt"] != standard["prompt"]
        assert "output_style" not in standard
    with pytest.raises(ValueError, match="unsupported_reading_output_style"):
        reading.system_for_encoding("records", "unknown")
    with pytest.raises(ValueError):
        MessageHistoryConfig(concise_output_tokens=0)
