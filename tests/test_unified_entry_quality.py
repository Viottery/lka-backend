"""Unified entry exercises real ReAct/graph execution, not keyword routing."""

import json

import pytest

from app.core.config import Settings
from app.core.llm import LLMResponse
from app.core.local_config import AgentConfig, LocalAppConfig
from app.core.runtime import LocalKnowledgeAgentRuntime


class ScriptedClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def complete_text(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(provider="isolated", status="completed", content=next(self.outputs),
                           prompt_summary=kwargs["prompt_summary"])


def operation(kind, **fields):
    return json.dumps({"operation": {"type": kind, **fields}})


def runtime_for(tmp_path, monkeypatch, orchestrator, outputs):
    config = LocalAppConfig(agent=AgentConfig(orchestrator=orchestrator,
                                            unified_entry_enabled=True))
    config.memory.enabled = False
    if hasattr(config, "message_history"):
        config.message_history.enabled = False
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    runtime = LocalKnowledgeAgentRuntime(Settings(LKA_DATA_DIR=tmp_path / "data",
                                                  LKA_WORKSPACE_ROOTS=str(tmp_path)))
    client = ScriptedClient(outputs)
    runtime.agent_turn_loop.llm_client = client
    return runtime, client


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_unified_entry_expands_then_reads_without_route_dispatch(tmp_path, monkeypatch, orchestrator):
    path = tmp_path / "renamed-evidence.md"
    path.write_text("CURRENT_FLAG_934\n", encoding="utf-8")
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(path)}),
        operation("final_answer", reason="The requested identifier was read."),
        "CURRENT_FLAG_934",
    ])
    result = runtime.run_agent_turn(session_id="simple", user_input="Read the identifier in the file.")
    assert result.answer == "CURRENT_FLAG_934"
    assert result.initial_package == "filesystem"
    assert [event.tool_name for event in result.tool_events] == ["filesystem.read_file"]
    assert len(client.calls) == 4
    stages = [call["metadata"]["stage"] for call in client.calls]
    assert stages == ["decision", "decision", "decision", "answer"]
    assert any(event.type == "entry_ready" for event in result.progress_events)
    first = json.loads(client.calls[0]["user_prompt"])
    assert first["expanded_tools"] == []
    assert "CURRENT_FLAG_934" in client.calls[-1]["user_prompt"]
    assert runtime.agent_run_manager.get_run(result.run_id).status.value == "completed"


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_unified_context_answer_keeps_separate_writer(tmp_path, monkeypatch, orchestrator):
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("final_answer", reason="No external evidence needed."), "Short reply.",
    ])
    result = runtime.run_agent_turn(session_id="context", user_input="Just say hello.")
    assert result.answer == "Short reply."
    assert len(client.calls) == 2 and not result.tool_events
    assert not result.initial_package and not result.expanded_packages
    assert client.calls[0]["metadata"]["stage"] == "decision"
    assert client.calls[-1]["metadata"]["stage"] in {"answer", "context_answer"}
    assert any(event.type == "entry_ready" for event in result.progress_events)


def test_unified_entry_is_explicit_opt_in():
    assert AgentConfig().unified_entry_enabled is False
