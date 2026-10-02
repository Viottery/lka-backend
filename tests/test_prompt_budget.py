from dataclasses import dataclass

import pytest

from app.core.prompt_budget import PromptBudgeter, PromptBudgetExceeded


@dataclass
class Count:
    count: int
    method: str = "test_chars"
    conservative: bool = True


class CharCounter:
    def count_request(self, system_prompt, user_prompt, tools=None):
        return Count(len(system_prompt) + len(user_prompt) + len(str(tools or [])))


def test_budget_keeps_current_input_and_drops_older_views():
    import json

    payload = {
        "user_input": "current task",
        "observations": [{"old": "x" * 800}, {"new": "y" * 40}],
        "session_context_window": {
            "summary": "current summary",
            "recent_messages": [
                {"content": "a" * 800}, {"content": "b" * 40},
                {"content": "c" * 40}, {"content": "d" * 40},
            ],
            "agent_instructions": [{
                "kind": "global", "path": "/tmp/AGENTS.md", "content": "rule" * 200,
                "sha256": "version", "next_offset": None,
            }],
        },
    }
    fitted = PromptBudgeter(CharCounter()).fit(
        system_prompt="system", user_prompt=json.dumps(payload), input_limit=800,
    )
    visible = json.loads(fitted.user_prompt)
    assert visible["user_input"] == "current task"
    assert visible["session_context_window"]["summary"] == "current summary"
    assert visible["_prompt_budget"]["older_observations"] >= 1
    assert len(visible["session_context_window"]["recent_messages"]) >= 2
    assert visible["session_context_window"]["agent_instructions"][0]["sha256"] == "version"
    assert fitted.input_tokens <= fitted.input_limit


def test_budget_fails_closed_when_mandatory_content_exceeds_limit():
    import json

    with pytest.raises(PromptBudgetExceeded, match="mandatory"):
        PromptBudgeter(CharCounter()).fit(
            system_prompt="system", user_prompt=json.dumps({"user_input": "x" * 1000}),
            input_limit=200,
        )


def test_fork_replan_signal_survives_budget_compaction():
    import json

    payload = {
        "user_input": "continue plan",
        "observations": [{
            "action": "fork_subtasks", "status": "failed", "replan_required": True,
            "failed_step_ids": ["step-a"],
            "task_results": [{
                "step_id": "step-a", "status": "failed", "summary": "x" * 10_000,
                "failure": {"category": "tool_error", "code": "retryable"},
            }],
        }],
    }
    result = PromptBudgeter(CharCounter()).fit(
        system_prompt="system", user_prompt=json.dumps(payload), input_limit=800,
    )
    visible = json.loads(result.user_prompt)
    assert visible["observations"][0]["replan_required"] is True
    assert visible["observations"][0]["failed_step_ids"] == ["step-a"]
    assert visible["observations"][0]["task_results"][0]["failure"]["category"] == "tool_error"
