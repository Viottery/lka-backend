import pytest
from pydantic import ValidationError

from app.core.local_config import AgentConfig


def test_answer_review_config_defaults_and_bounds() -> None:
    config = AgentConfig()
    assert config.answer_review_enabled is True
    assert config.answer_review_max_reworks == 2
    assert AgentConfig(answer_review_enabled=False, answer_review_max_reworks=0).answer_review_enabled is False
    assert AgentConfig(answer_review_max_reworks=5).answer_review_max_reworks == 5
    for invalid in (-1, 6):
        with pytest.raises(ValidationError):
            AgentConfig(answer_review_max_reworks=invalid)
