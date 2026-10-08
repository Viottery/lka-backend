from types import SimpleNamespace

import pytest

from app.core.local_config import MessageHistoryConfig
from app.domains.message_reading_rollout import choose_selector_algorithm


def test_rollout_defaults_off_and_boundaries():
    default = MessageHistoryConfig()
    assert choose_selector_algorithm(default, "family") == "current"
    candidate = default.model_copy(update={"selector_algorithm": "multi_lane"})
    assert choose_selector_algorithm(candidate, "family") == "current"
    assert choose_selector_algorithm(candidate.model_copy(update={"selector_rollout_percent": 100}), "family") == "multi_lane"
    for invalid in (-1, 101):
        with pytest.raises(ValueError):
            MessageHistoryConfig(selector_rollout_percent=invalid)


def test_rollout_is_stable_and_monotonic_and_frozen_work_survives_rollback():
    config = SimpleNamespace(selector_algorithm="multi_lane", selector_rollout_percent=10)
    cohort = [choose_selector_algorithm(config, f"family-{index}") for index in range(1000)]
    assert cohort == [choose_selector_algorithm(config, f"family-{index}") for index in range(1000)]
    assert 50 < cohort.count("multi_lane") < 150
    config.selector_rollout_percent = 20
    increased = [choose_selector_algorithm(config, f"family-{index}") for index in range(1000)]
    assert all(old != "multi_lane" or new == "multi_lane" for old, new in zip(cohort, increased, strict=True))
    assert choose_selector_algorithm(config, "old-family", {"input_snapshot_digest": "old"}) == "current"
    config.selector_rollout_percent = 0
    assert choose_selector_algorithm(config, "frozen", {"selector_algorithm": "multi_lane"}) == "multi_lane"
    with pytest.raises(ValueError, match="unsupported_checkpoint_selector"):
        choose_selector_algorithm(config, "frozen", {"selector_algorithm": "unknown"})
