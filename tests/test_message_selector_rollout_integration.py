import json

import pytest
from test_message_prompt_encoding import CodecModel, checkpoint
from test_message_reading_production_v3 import IDENTITY, finish, insert, pipeline

from app.core.local_config import MessageHistoryConfig
from app.core.message_analysis import MessageAnalysisCoordinator
from app.domains.message_reading_selection import SELECTOR_VERSION
from app.tool_packages import message_reading_analysis as reading
from scripts.compare_message_reading import synthetic_window


def test_self_identity_does_not_cross_platform_with_colliding_ids():
    coordinator = MessageAnalysisCoordinator.__new__(MessageAnalysisCoordinator)
    coordinator.config = MessageHistoryConfig(reading_algorithm="selected", selector_algorithm="multi_lane",
                                             selector_rollout_percent=100)
    batch = synthetic_window()["batch"]
    batch["reading_profile"]["self_ids"] = {"qq": ["qq-self"], "telegram": ["owner"]}
    for row in batch["messages"]:
        row["text"] = "ordinary"
    projection = reading.ScopedProjection("qq-group", batch["messages"])
    context = coordinator._v3_context(batch, projection)
    wrong_platform = coordinator._v3_selection(projection, context, "family", "multi_lane", platform="qq")
    assert not any(reason in ("direct_mention", "reply_to_self") for row in wrong_platform["decisions"]
                   for reason in row["reasons"])
    matching = coordinator._v3_selection(projection, context, "family", "multi_lane", platform="telegram")
    assert any("direct_mention" in row["reasons"] for row in matching["decisions"])


@pytest.mark.parametrize("initial", ["current", "multi_lane"])
def test_frozen_selector_survives_rollout_and_new_family_uses_new_choice(tmp_path, initial):
    model = CodecModel(evidence=True)
    service, store, policy, coordinator, _ = pipeline(
        tmp_path, "selected", model=model, message_encoding="codec_v2",
        selector_algorithm=initial, selector_rollout_percent=100)
    insert(service, 30)
    service.schedule_pending(policy["conversation_key"], force=True)
    assert coordinator.worker.run_one()
    old_row, frozen = checkpoint(service)
    assert frozen["selector_algorithm"] == initial
    opposite = "current" if initial == "multi_lane" else "multi_lane"
    settings = coordinator.config.model_copy(update={"selector_algorithm": opposite})
    restarted = MessageAnalysisCoordinator(service=service, store=store, config=settings, llm_client=model)
    finish(service, policy, restarted)
    assert store.list(status="succeeded"), store.list()
    assert restarted.controller.quota_usage(old_row["family_id"])["total_calls"] == 2
    with service._connection() as conn:
        outputs = [json.loads(row[0]) for row in conn.execute("SELECT output_json FROM message_reading_batch_outputs")]
        assert conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0] == 1
    expected = SELECTOR_VERSION if initial == "multi_lane" else reading.VERSIONS["selector"]
    assert outputs[0]["reading_manifest"]["versions"]["selector"] == expected
    assert outputs[0]["reading_manifest"]["versions"]["prompt"] == reading.CODEC_PROMPT_VERSION
    service.import_messages([{**IDENTITY, "message_id": "new-family-message", "sender_id": "peer",
                              "text": "ordinary", "sent_at": 1790920100, "received_at": 1790920100}])
    finish(service, policy, restarted)
    with service._connection() as conn:
        rows = conn.execute("SELECT checkpoint_json FROM message_reading_checkpoints WHERE family_id!=?",
                            (old_row["family_id"],)).fetchall()
    assert rows and all(json.loads(row[0])["selector_algorithm"] == opposite for row in rows)
