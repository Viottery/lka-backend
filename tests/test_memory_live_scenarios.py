"""Full root-turn wiring with synthetic conversations and deterministic inference.

These tests verify what is sent to the model, not real-model answer intelligence.
"""

import json
import threading

import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.llm import LLMResponse


class PromptClient:
    def __init__(self):
        self.answer_contexts = []

    def complete_text(self, *, system_prompt, user_prompt, **kwargs):
        payload = json.loads(user_prompt)
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps({"selected_package": None, "reason": "Answer without tools"})
        elif "Choose the next single action" in system_prompt:
            content = json.dumps({"operation": {"type": "final_answer", "final_answer": None,
                                                "reason": "Enough context", "confidence": "high"}})
        elif "Final Answer Writer" in system_prompt or "Answer the current user turn directly" in system_prompt:
            self.answer_contexts.append(payload["session_context_window"])
            content = "已经处理"
        else:
            raise AssertionError(system_prompt[:100])
        return LLMResponse(provider="scenario", model="deterministic", status="completed",
                           prompt_summary=kwargs.get("prompt_summary", "scenario"), content=content)


def _runtime(tmp_path, monkeypatch, orchestrator):
    config = tmp_path / "config.toml"
    config.write_text(f'[agent]\norchestrator="{orchestrator}"\n[memory]\nextraction_debounce_seconds=0\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    client = PromptClient()
    runtime.agent_turn_loop.llm_client = client
    return runtime, client


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_cross_session_memory_reaches_real_answer_prompt_and_forget_is_immediate(tmp_path, monkeypatch, orchestrator):
    runtime, client = _runtime(tmp_path, monkeypatch, orchestrator)
    runtime.run_agent_turn(session_id="learn", user_input="我希望以后回答先给结论")
    assert runtime.memory_background.worker.run_one()
    memory = runtime.memory_service.list(scope="global")[0]
    runtime.run_agent_turn(session_id="new-session", user_input="解释一下接口的设计")
    context = client.answer_contexts[-1]
    assert context["recent_messages"] == []
    assert context["recalled_memories"]["items"][0]["memory_id"] == memory.memory_id
    # No worker runs after this turn. Retraction must precede prompt assembly.
    runtime.run_agent_turn(session_id="new-session", user_input=f"忘记 {memory.memory_id}")
    assert client.answer_contexts[-1]["recalled_memories"]["items"] == []
    assert runtime.memory_service.get(memory.memory_id).status == "retracted"


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_foreground_turn_finishes_while_background_model_is_still_blocked(tmp_path, monkeypatch, orchestrator):
    runtime, client = _runtime(tmp_path, monkeypatch, orchestrator)
    runtime.run_agent_turn(session_id="learn", user_input="我倾向使用有条理的解释")
    entered, release = threading.Event(), threading.Event()

    class SlowBackground:
        def complete_text(self, **kwargs):
            entered.set()
            assert release.wait(timeout=10)
            return LLMResponse(provider="background", model="deterministic", status="completed",
                               prompt_summary="background", content='{"candidates":[]}')

    runtime.memory_background.llm_client = SlowBackground()
    runtime.memory_background.allow_remote_extraction = True
    worker = threading.Thread(target=runtime.memory_background.worker.run_one)
    worker.start()
    try:
        assert entered.wait(timeout=2)
        result = runtime.run_agent_turn(session_id="other", user_input="解释一下代码结构")
        assert result.answer == "已经处理"
        assert not release.is_set() and worker.is_alive()
        assert client.answer_contexts[-1]["recalled_memories"]["items"] == []
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
