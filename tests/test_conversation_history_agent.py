"""Offline Agent integration coverage for retrieval from original conversation history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.llm import LLMResponse


class _HistoryRetrievalLLM:
    def __init__(self):
        self.route_payloads = []
        self.decision_payloads = []
        self.prompts = []
        self.completed_tool_calls = []

    def complete_text(self, *, system_prompt, user_prompt, prompt_summary,
                      temperature=0.0, max_output_tokens=None):
        self.prompts.append((system_prompt, user_prompt, prompt_summary))
        if "Choose at most one tool package" in system_prompt:
            payload = json.loads(user_prompt)
            self.route_payloads.append(payload)
            content = json.dumps({"selected_package": None,
                "reason": "Requested fact is outside the fitted context.",
                "context_need": "retrieve", "search_query": "HISTORY_FACT_7F2"})
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            self.decision_payloads.append(payload)
            expanded = set(payload.get("expanded_package_names", []))
            observations = payload.get("observations", [])
            completed_tools = {item.get("tool_name") for item in observations if isinstance(item, dict)}
            if "history" not in expanded:
                operation = {"type": "expand_package", "package_name": "history"}
            elif "history.search" not in completed_tools or (
                observations[-1].get("tool_name") == "history.search"
                and not observations[-1]["result"]["output"]["messages"]
                and observations[-1]["result"]["output"].get("next_index_cursor")
            ):
                operation = {"type": "tool_call", "tool_name": "history.search",
                             "tool_input": {"query": "HISTORY_FACT_7F2", "limit": 5}}
                if "history.search" in completed_tools:
                    operation["tool_input"]["index_cursor"] = observations[-1]["result"]["output"]["next_index_cursor"]
            elif "history.read" not in completed_tools:
                search = next(o for o in reversed(observations) if o.get("tool_name") == "history.search")
                operation = {"type": "tool_call", "tool_name": "history.read",
                             "tool_input": {"seq": search["result"]["output"]["messages"][0]["seq"], "max_chars": 2000}}
            else:
                operation = {"type": "final_answer", "final_answer": None,
                             "reason": "The source read contains the requested fact."}
            content = json.dumps({"operation": operation, "assistant_message": "Checking the original conversation."})
        elif "Tool Result Checker" in system_prompt:
            content = json.dumps({"status": "accepted", "message": "Read completed."})
        elif "Final Answer Writer" in system_prompt:
            content = "The earlier conversation recorded HISTORY_FACT_7F2 as the requested value."
        else:
            content = "Unexpected stage."
        return LLMResponse(provider="offline-history-test", status="completed",
                           content=content, prompt_summary=prompt_summary)


class _SufficientContextLLM:
    def __init__(self):
        self.context_answer_payloads = []

    def complete_text(self, *, system_prompt, user_prompt, prompt_summary,
                      temperature=0.0, max_output_tokens=None):
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps({"selected_package": None,
                "reason": "The exact fact is in the visible context.",
                "context_need": "sufficient"})
        elif "using only the provided session context window" in system_prompt:
            self.context_answer_payloads.append(json.loads(user_prompt))
            content = "The current context records DIRECT_FACT_92."
        else:
            content = "Unexpected stage."
        return LLMResponse(provider="offline-history-test", status="completed",
                           content=content, prompt_summary=prompt_summary)


def _app(tmp_path: Path, monkeypatch, orchestrator: str):
    config_path = tmp_path / f"{orchestrator}.toml"
    config_path.write_text(
        f'[agent]\norchestrator = "{orchestrator}"\nmax_decision_steps = 8\n'
        '[safety]\ntool_review_mode = "skip"\n'
        '[memory]\nenabled = false\nbackground_enabled = false\n'
        '[embedding]\nenabled = false\n'
        '[reranker]\nenabled = false\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / f"data-{orchestrator}"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    return create_app()


def test_semantic_runtime_uses_separate_local_only_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "semantic.toml"
    config_path.write_text(
        '[embedding]\nenabled = false\nlocal_files_only = false\n'
        '[conversation_history]\nsemantic_enabled = true\n'
        '[memory]\nbackground_enabled = false\n'
        '[reranker]\nenabled = false\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "runtime-semantic"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    indexer = runtime.conversation_history_indexer
    assert indexer is not None and indexer.enabled is False
    assert indexer.index._provider._local_files_only is True
    assert indexer.index._provider._model is None
    assert indexer.kind in runtime.memory_background.worker.handlers


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_cold_index_continuation_runs_without_repeated_call_cache(orchestrator, tmp_path, monkeypatch):
    runtime = _app(tmp_path, monkeypatch, orchestrator).state.runtime
    sid = runtime.session_service.create_session(title="cold history").session.session_id
    runtime.session_service.record_context_exchange(session_id=sid, user_input="irrelevant",
        agent_answer="ack", trace_id="seed")
    with runtime._conn() as conn:
        conn.executemany("INSERT INTO agent_session_context_messages VALUES(?,?,?,?,?,?)",
            [(sid, seq, "tool", "noise", "now", f"bg{seq}") for seq in range(3, 301)]
            + [(sid, 301, "user", "The reference is HISTORY_FACT_7F2.", "now", "fact"),
               (sid, 302, "agent", "ack", "now", "fact")])
        conn.execute("UPDATE agent_session_context_state SET next_seq=303 WHERE session_id=?", (sid,))
    runtime.agent_turn_loop.session_context_token_budget = 10
    runtime.agent_turn_loop.llm_client = _HistoryRetrievalLLM()
    result = runtime.run_agent_turn(session_id=sid, user_input="What exact reference did I give earlier?")
    assert [e.tool_name for e in result.tool_events] == ["history.search", "history.search", "history.read"]
    assert all(e.result["status"] == "completed" for e in result.tool_events)
    assert "HISTORY_FACT_7F2" in result.answer


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_missing_window_fact_is_retrieved_from_original_history(orchestrator, tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, orchestrator)
    runtime = app.state.runtime
    seed = runtime.session_service.create_session(title=f"history-{orchestrator}")
    session_id = seed.session.session_id
    old_user = "Please remember this exact reference: HISTORY_FACT_7F2."
    runtime.session_service.record_context_exchange(session_id=session_id, user_input=old_user,
        agent_answer="Acknowledged.", trace_id="seed-turn")
    runtime.agent_turn_loop.session_context_token_budget = 10
    llm = _HistoryRetrievalLLM()
    runtime.agent_turn_loop.llm_client = llm

    response = runtime.run_agent_turn(session_id=session_id,
        user_input="What exact reference did I give earlier?")

    assert response.answer == "The earlier conversation recorded HISTORY_FACT_7F2 as the requested value."
    assert [event.tool_name for event in response.tool_events] == ["history.search", "history.read"], (
        response.decision_events, response.progress_events, llm.prompts, llm.route_payloads,
    )
    assert all(event.result.get("status") == "completed" for event in response.tool_events)
    assert "HISTORY_FACT_7F2" in json.dumps(response.tool_events[-1].result)
    assert "history" in response.expanded_packages
    assert not any(name.startswith("management.") for tool in response.expanded_tools
                   if isinstance(tool, dict) for name in [tool.get("name", "")])
    route_window = llm.route_payloads[0]["session_context_window"]
    assert "HISTORY_FACT_7F2" not in json.dumps(route_window)
    assert any("HISTORY_FACT_7F2" in json.dumps(payload) for payload in llm.decision_payloads)
    assert runtime.agent_run_manager.get_run(response.run_id).status.value == "completed"
    started = [event for event in runtime.agent_run_manager.list_events(response.run_id)
               if event.type == "llm_started"]
    manifests = [event.payload.get("context_manifest") for event in started]
    assert manifests and all(isinstance(manifest, dict) for manifest in manifests)
    assert "HISTORY_FACT_7F2" not in json.dumps(manifests)


def test_sufficient_null_route_answers_from_visible_context_without_history_tools(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "legacy")
    runtime = app.state.runtime
    session_id = runtime.session_service.create_session(title="sufficient-context").session.session_id
    runtime.session_service.record_context_exchange(session_id=session_id,
        user_input="The current reference is DIRECT_FACT_92.",
        agent_answer="I have recorded DIRECT_FACT_92.", trace_id="direct-seed")
    llm = _SufficientContextLLM()
    runtime.agent_turn_loop.llm_client = llm

    response = runtime.run_agent_turn(session_id=session_id,
        user_input="What reference is in the current context?")

    assert "DIRECT_FACT_92" in response.answer
    assert llm.context_answer_payloads
    assert response.tool_events == []
    assert response.expanded_packages == []
