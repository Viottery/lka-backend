"""The decision boundary must expose durable Planner feedback, not hide it."""

from __future__ import annotations

import json

from app.core.agent_turn import AgentTurnLoop
from app.core.llm import LLMResponse
from app.core.multi_agent import ForkPolicy, ScopeGrant


def test_failed_child_feedback_reaches_structured_replan_decision() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.fork_policy = ForkPolicy(
        max_depth=1,
        max_children=2,
        max_fork_size=2,
        allowed_scope=ScopeGrant(),
    )
    loop.run_manager = None
    loop.llm_client = None
    loop.llm_generation_token_budget = 256
    loop.decision_format_max_attempts = 1
    loop._context_window_for_llm = lambda context: context
    loop._route_context = lambda route: route
    loop._completed_tool_call_summaries = lambda observations: []
    prompts: list[dict[str, str]] = []

    def scripted_decision(**kwargs):
        prompts.append({"system": kwargs["system_prompt"], "user": kwargs["user_prompt"]})
        return LLMResponse(
            provider="scripted",
            status="completed",
            content=json.dumps({
                "operation": {
                    "type": "plan_patch",
                    "patch_id": "retry_research",
                    "plan_id": "plan_1",
                    "expected_revision": 0,
                    "operation": "retry_step",
                    "target_step_id": "research",
                    "reason": "The provider error was retryable.",
                }
            }),
            prompt_summary="planner feedback test",
        )

    loop._complete_text_with_retry = scripted_decision
    observations = [{
        "action": "fork_subtasks",
        "execution_status": "failed",
        "failed_step_ids": ["research"],
        "task_results": [{
            "step_id": "research",
            "status": "failed",
            "failure": {"category": "provider", "code": "temporary", "retryable": True},
        }],
        "aggregate": {"status": "failed"},
        "replan_required": True,
    }]

    decision = loop._decide_next_action(
        user_input="Research and summarize.",
        route={},
        context_window={},
        package_catalog=[],
        expanded_package_names=[],
        expanded_tools=[],
        observations=observations,
        llm_events=[],
    )

    assert decision["action"] == "plan_patch"
    assert decision["operation"]["operation"] == "retry_step"
    assert decision["operation"]["target_step_id"] == "research"
    assert "When prior results require re-planning" in prompts[0]["system"]
    assert json.loads(prompts[0]["user"])["observations"] == observations


def test_large_child_results_keep_actionable_replan_feedback_in_prompt() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    observation = {
        "action": "fork_subtasks",
        "execution_status": "failed",
        "failed_step_ids": ["step_0"],
        "replan_required": True,
        "task_results": [
            {
                "step_id": f"step_{index}",
                "status": "failed" if index == 0 else "completed",
                "summary": "large result " * 500,
                "failure": {
                    "category": "provider", "code": "rate_limited",
                    "message": "Retry after the provider recovers.", "retryable": True,
                } if index == 0 else None,
            }
            for index in range(6)
        ],
        "aggregate": {"status": "failed", "failed_step_ids": ["step_0"]},
        "verification": {
            "status": "failed",
            "summary": "Required evidence is missing.",
            "missing_requirements": ["source citation"],
            "recommended_actions": ["Collect an authorized source."],
        },
    }

    prompt_observations = loop._observations_within_prompt_budget([observation])

    assert len(prompt_observations) == 1
    kept = prompt_observations[0]
    assert kept["action"] == "fork_subtasks"
    assert kept["replan_required"] is True
    assert kept["task_results"][0]["failure"]["code"] == "rate_limited"
    assert kept["aggregate"]["failed_step_ids"] == ["step_0"]
    assert kept["verification"]["missing_requirements"] == ["source citation"]
    assert kept["_prompt_compacted"] is True
