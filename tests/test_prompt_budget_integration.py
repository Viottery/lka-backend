"""Agent-turn request preflight without a provider or user data."""

from __future__ import annotations

import json
import threading

import pytest

from app.core.agent_turn import AgentTurnLoop, _turn_llm_model
from app.core.llm import build_llm_service
from app.core.local_config import LLMClientConfig, LLMModelConfigOverride, LLMProviderConfig
from app.core.prompt_budget import PromptBudgetExceeded


def _loop(*, context_window_tokens: int | None) -> AgentTurnLoop:
    service = build_llm_service(LLMProviderConfig(
        default_client="local",
        clients=[LLMClientConfig(
            name="local", provider="mock", default_model="demo",
            context_window_tokens=context_window_tokens,
            output_reserve_tokens=8192,
        )],
    ))
    assert service is not None
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.llm_client = service
    loop.prompt_input_target_tokens = 131_072
    loop.prompt_safety_margin_tokens = 4096
    loop.session_context_token_budget = 65_536
    loop._prompt_counter_lock = threading.Lock()
    loop._prompt_counters = {}
    return loop


def test_configured_model_budget_preserves_current_task_and_history_contract():
    loop = _loop(context_window_tokens=148_000)
    payload = {
        "user_input": "must remain visible",
        "session_context_window": {
            "summary": "past decisions",
            "recent_messages": [{"content": "old"}, {"content": "latest"}],
        },
        "observations": [{"result": "x" * 140_000}],
    }
    result = loop._budget_llm_prompt(
        system_prompt="System instructions", user_prompt=json.dumps(payload),
        max_output_tokens=None, tools=None,
    )
    visible = json.loads(result.user_prompt)
    assert result.input_limit == 131_072
    assert result.output_reserve_tokens == 8192
    assert result.input_tokens <= result.input_limit
    assert visible["user_input"] == "must remain visible"
    assert visible["session_context_window"]["recent_messages"][-1]["content"] == "latest"
    assert visible["_prompt_budget"]["older_observations"] == 1
    assert loop.session_context_token_budget == 65_536


def test_configured_model_budget_rejects_oversize_mandatory_input():
    loop = _loop(context_window_tokens=148_000)
    with pytest.raises(PromptBudgetExceeded, match="mandatory"):
        loop._budget_llm_prompt(
            system_prompt="System instructions",
            user_prompt=json.dumps({"user_input": "x" * 140_000}),
            max_output_tokens=None, tools=None,
        )


def test_provider_level_capacity_does_not_leak_to_named_clients():
    config = LLMProviderConfig(
        context_window_tokens=148_000,
        clients=[LLMClientConfig(name="other", provider="mock", default_model="different")],
    )
    assert config.resolve_context_capacity("other", "different") is None


def test_unknown_model_capacity_fails_before_provider_dispatch():
    loop = _loop(context_window_tokens=None)
    with pytest.raises(PromptBudgetExceeded, match="no configured context_window_tokens"):
        loop._budget_llm_prompt(
            system_prompt="system", user_prompt='{"user_input":"hi"}',
            max_output_tokens=None, tools=None,
        )


def test_explicit_output_cap_is_reserved_in_full():
    loop = _loop(context_window_tokens=148_000)
    result = loop._budget_llm_prompt(
        system_prompt="system", user_prompt='{"user_input":"hi"}',
        max_output_tokens=16_000, tools=None,
    )
    assert result.output_reserve_tokens == 16_000
    assert result.input_limit == 127_904


def test_session_counter_follows_request_model_override(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.WordLevel(vocab={"[UNK]": 0, "你好": 1}, unk_token="[UNK]")
    )
    path = tmp_path / "alternate.json"
    tokenizer.save(str(path))
    service = build_llm_service(LLMProviderConfig(
        default_client="local",
        clients=[LLMClientConfig(
            name="local", provider="mock", default_model="demo",
            context_window_tokens=148_000,
            model_overrides={"alternate": LLMModelConfigOverride(
                context_window_tokens=148_000, tokenizer_json_path=path,
            )},
        )],
    ))
    loop = _loop(context_window_tokens=148_000)
    loop.llm_client = service
    assert loop._selected_session_counter().count_text("你好").method == "utf8_byte_upper_bound"
    token = _turn_llm_model.set("alternate")
    try:
        assert loop._selected_session_counter().count_text("你好").method == "local_tokenizers_json"
    finally:
        _turn_llm_model.reset(token)
