"""Regression for source-fenced sessions and supported conversational stops."""

import json

import httpx
import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.tools import ToolContext, ToolResult
from app.integrations.web_search import BraveSearchAdapter
from app.tool_packages.messages import MESSAGE_ORIGIN_CONSTRAINT
from tests.test_answer_generation_recovery_quality import make_loop


def runtime_for(tmp_path, monkeypatch, orchestrator):
    config = tmp_path / "local.toml"
    config.write_text(f'[agent]\norchestrator = "{orchestrator}"\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    # Enter the production decision loop without real model or provider calls.
    runtime.agent_turn_loop.llm_client = make_loop(tmp_path, [])[0].llm_client
    return runtime


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_public_search_after_message_read_uses_per_call_review(tmp_path, monkeypatch, orchestrator):
    runtime = runtime_for(tmp_path, monkeypatch, orchestrator)
    loop = runtime.agent_turn_loop
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={"web": {"results": [
            {"title": "Public guide", "url": "https://example.org/guide", "description": "Public excerpt"},
        ]}})

    runtime.tool_registry.get_tool("web.search").adapter = BraveSearchAdapter(
        "synthetic-key", transport=httpx.MockTransport(provider)
    )
    runtime.tool_executor.constraint_store.add([("session", "sample")], {MESSAGE_ORIGIN_CONSTRAINT})
    decisions = iter([
        {"action": "call_tool", "tool_name": "web.search", "tool_input": {"query": "public guide"}},
        {"action": "final_answer"},
    ])
    monkeypatch.setattr(loop, "_route", lambda **kwargs: {"selected_package": "web"})
    monkeypatch.setattr(loop, "_decide_next_action", lambda **kwargs: next(decisions))
    monkeypatch.setattr(loop, "_answer_with_llm", lambda **kwargs: "Contract test answer.")
    result = runtime.run_agent_turn(session_id="sample", user_input="Search public information.")
    assert result.answer == "Contract test answer."
    assert len(requests) == 1
    assert result.tool_events[0].result["status"] == "completed"
    reviews = runtime.agent_run_manager.list_safety_reviews(result.run_id)
    assert len(reviews) == 1 and reviews[0].status == "approved"
    assert reviews[0].read_only is True and reviews[0].user_request == "Search public information."
    assert runtime.tool_executor.constraint_store.get([("session", "sample")]) == {MESSAGE_ORIGIN_CONSTRAINT}


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
@pytest.mark.parametrize("action", ["request_confirmation", "no_op"])
def test_control_stop_preserves_message_without_fallback_or_extra_model_call(
    tmp_path, monkeypatch, orchestrator, action,
):
    runtime = runtime_for(tmp_path, monkeypatch, orchestrator)
    loop = runtime.agent_turn_loop
    monkeypatch.setattr(loop, "_route", lambda **kwargs: {"selected_package": "web"})
    monkeypatch.setattr(loop, "_decide_next_action", lambda **kwargs: {
        "action": action, "answer": "请指定需要查询的日期。", "operation": {"type": action},
    })
    monkeypatch.setattr(loop, "_answer_with_llm", lambda **kwargs: pytest.fail("Control stop reran answer model"))
    result = runtime.run_agent_turn(session_id="sample", user_input="Find information.")
    assert result.answer == "请指定需要查询的日期。"
    assert result.tool_events == [] and result.llm_events == []
    assert not any(e.source == "local" and e.action == "answer" for e in result.decision_events)


def test_source_policy_refusal_uses_local_feedback_not_llm(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch, "legacy")
    runtime.tool_registry.register_effect_constraint(MESSAGE_ORIGIN_CONSTRAINT,
                                                    blocked_domains=(), block_unrestricted=True)
    runtime.tool_executor.constraint_store.add([("session", "sample")], {MESSAGE_ORIGIN_CONSTRAINT})
    result = runtime.tool_executor.execute(
        invocation_id="denied", tool_name="bash.run", tool_input={"command": "pwd"},
        context=ToolContext(session_id="sample", safety_review_approved=True),
    )
    assert result.status == "rejected" and result.execution_started is False
    assert "review_path" not in result.output
    loop = runtime.agent_turn_loop
    monkeypatch.setattr(loop, "_complete_text_with_retry", lambda **kwargs: pytest.fail("Deterministic denial called LLM"))
    events = []
    feedback = loop._check_tool_result(user_input="Inspect workspace", tool_package="bash",
                                      decision={}, tool_result=result, llm_events=events)
    assert feedback["source"] == "local" and feedback["status"] == "failed" and events == []
    assert "invent an approval" in feedback["remaining_work"]
    assert "/messages/matter-proposals" not in json.dumps(feedback)


def test_domain_refusal_retains_real_dedicated_review_path(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch, "legacy")
    result = ToolResult(
        invocation_id="denied", tool_name="matter.create_many", status="rejected", execution_started=False,
        error="Source-derived actions require the dedicated human review workflow.",
        output={"source_constraint_denial": {"reason": "effect_domain_blocked", "retryable": False},
                "human_review_required": True, "review_path": "/messages/matter-proposals"},
    )
    loop = runtime.agent_turn_loop
    monkeypatch.setattr(loop, "_complete_text_with_retry", lambda **kwargs: pytest.fail("Deterministic denial called LLM"))
    feedback = loop._check_tool_result(user_input="Create matter", tool_package="matter",
                                      decision={}, tool_result=result, llm_events=[])
    assert feedback["source"] == "local" and "/messages/matter-proposals" in feedback["remaining_work"]
