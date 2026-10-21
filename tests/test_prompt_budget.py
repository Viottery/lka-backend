import copy
import json
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


def test_recent_source_survives_control_feedback_and_old_context_eviction():
    record = {
        "tool_name": "records.read", "input": {"path": "record.txt"},
        "result": {"status": "completed", "output": {
            "content": "The previous check failed. Recovery is not verified.",
        }},
        "_cache": {"historical_only": True, "as_of": "2025-04-03"},
    }
    payload = {
        "user_input": "Summarize the existing result.",
        "observations": [record, *[{"action": "control_feedback", "attempt": i} for i in range(3)]],
        "session_context_window": {
            "recalled_memories": {"items": [{"content": "old memory " * 200}]},
            "recent_messages": [{"content": "old dialogue " * 200},
                                {"content": "recent question"}, {"content": "recent reply"}],
        },
    }
    original = copy.deepcopy(payload)
    result = PromptBudgeter(CharCounter()).fit(
        system_prompt="system", user_prompt=json.dumps(payload), input_limit=1000,
    )
    visible = json.loads(result.user_prompt)
    assert record in visible["observations"]
    assert visible["_prompt_budget"]["lower_ranked_memories"] == 1
    assert visible["_prompt_budget"]["older_context_messages"] == 1
    assert result.input_tokens <= 1000
    assert payload == original


def test_unfittable_recent_result_keeps_omission_handles_across_refits():
    resource = {"identity": "record-v1", "read_package": "records", "read_tool": "records.read",
                "read_input": {"path": "record.txt", "version": "v1"}}
    payload = {
        "user_input": "Summarize the existing result.",
        "observations": [{
            "tool_name": "records.read", "result": {"output": {"content": "x" * 6000}},
            "_resource": resource, "_result_cache": {"artifact_id": "artifact-record"},
        }],
    }
    original = copy.deepcopy(payload)
    budgeter = PromptBudgeter(CharCounter())
    first = budgeter.fit(system_prompt="system", user_prompt=json.dumps(payload), input_limit=1000)
    visible = json.loads(first.user_prompt)
    assert visible["observations"] == []
    assert first.omitted["older_observations"] == 1
    assert first.omitted["observation_artifact_ids"] == ["artifact-record"]
    assert first.omitted["observation_resources"] == [resource]
    visible["session_context_window"] = {"recalled_memories": {"items": [{"content": "y" * 6000}]}}
    second = budgeter.fit(system_prompt="system", user_prompt=json.dumps(visible), input_limit=1000)
    for key, value in first.omitted.items():
        assert second.omitted[key] == value
    assert second.omitted["lower_ranked_memories"] == 1
    assert first.input_tokens <= 1000 and second.input_tokens <= 1000
    assert payload == original


def test_oversize_recent_result_does_not_evict_fitting_guidance():
    instruction = {"kind": "global", "path": "/tmp/AGENTS.md", "content": "Keep this rule."}
    result = PromptBudgeter(CharCounter()).fit(
        system_prompt="system", user_prompt=json.dumps({
            "user_input": "continue", "observations": [{"result": "x" * 6000}],
            "session_context_window": {"agent_instructions": [instruction]},
        }), input_limit=1000,
    )
    visible = json.loads(result.user_prompt)
    assert visible["observations"] == []
    assert visible["session_context_window"]["agent_instructions"] == [instruction]
    assert "instruction_previews" not in result.omitted


def test_recent_result_omission_is_explicit_when_mandatory_tail_leaves_no_room():
    result = PromptBudgeter(CharCounter()).fit(
        system_prompt="system", user_prompt=json.dumps({
            "user_input": "x" * 750,
            "observations": [{"result": "y" * 200, "_result_cache": {"artifact_id": "raw-result"}}],
        }), input_limit=1000,
    )
    assert json.loads(result.user_prompt)["observations"] == []
    assert result.omitted["recent_observations"] == 1
    assert result.omitted["observation_artifact_ids"] == ["raw-result"]
    assert result.input_tokens <= 1000


def test_fork_compaction_retains_exact_verification_constraints():
    verification = {
        "status": "inconclusive", "summary": "Only the historical record was checked; current state is unknown.",
        "missing_requirements": ["current_health"],
        "checks": [{"requirement": "current_health", "passed": False, "required": True}],
    }
    payload = {"user_input": "continue", "observations": [{
        "action": "fork_subtasks", "status": "partial", "execution_status": "failed",
        "verification": verification,
        "task_results": [{"step_id": "check", "status": "partial", "summary": "x" * 6000,
                          "missing_requirements": ["current_health"]}],
    }], "session_context_window": {"agent_instructions": [{
        "kind": "global", "content": "Keep this instruction.",
    }]}}
    original = copy.deepcopy(payload)
    result = PromptBudgeter(CharCounter()).fit(
        system_prompt="system", user_prompt=json.dumps(payload), input_limit=1400,
    )
    view = json.loads(result.user_prompt)["observations"][0]
    assert view["verification"] == verification
    assert view["task_results"][0]["missing_requirements"] == ["current_health"]
    assert json.loads(result.user_prompt)["session_context_window"] == original["session_context_window"]
    assert result.omitted["compacted_fork_results"] == 1
    assert result.input_tokens <= 1400
    assert payload == original


def test_irreducible_fork_verdict_fails_closed_without_dropping_constraints():
    payload = {"user_input": "continue", "observations": [{
        "action": "fork_subtasks", "status": "partial",
        "verification": {"status": "inconclusive", "missing_requirements": ["x" * 1000]},
    }]}
    original = copy.deepcopy(payload)
    with pytest.raises(PromptBudgetExceeded, match="mandatory"):
        PromptBudgeter(CharCounter()).fit(
            system_prompt="system", user_prompt=json.dumps(payload), input_limit=500,
        )
    assert payload == original
