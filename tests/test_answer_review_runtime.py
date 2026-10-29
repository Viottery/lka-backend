from __future__ import annotations

import json

import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.llm import LLMResponse, LLMResponseMode
from app.core.llm.errors import LLMTimeoutError


class RuntimeScriptedLLM:
    """Synthetic model dispatching by stage and current prompt contents."""

    def __init__(self, *, timeout_review=False, approve_degraded=False):
        self.requests = []
        self.timeout_review = timeout_review
        self.approve_degraded = approve_degraded
        self.tool_written = False
        self.read_calls = 0
        self.answer_calls = 0
        self.review_calls = 0
        self.decision_calls = 0
        self.create_calls = 0

    def complete_text(self, **request):
        self.requests.append(request)
        stage = request["metadata"]["stage"]
        prompt = request["user_prompt"]
        if stage == "route":
            value = {"selected_package": "filesystem", "reason": "Read the supplied source file."}
        elif stage == "decision":
            self.decision_calls += 1
            if self.decision_calls == 1:
                value = {"operation": {"type": "expand_package", "package_name": "filesystem", "reason": "Read the source."}}
            elif self.decision_calls == 2:
                self.create_calls += 1
                value = {"operation": {"type": "tool_call", "tool_name": "filesystem.create_file",
                                         "tool_input": {"path": "audit.txt", "content": "Synthetic audit marker."},
                                         "reason": "Create the requested audit marker."}}
            elif self.decision_calls == 3:
                value = {"operation": {"type": "final_answer", "reason": "Give the current result."}}
            elif self.decision_calls == 4:
                self.read_calls += 1
                value = {"operation": {"type": "tool_call", "tool_name": "filesystem.read_file",
                                         "tool_input": {"path": "facts.txt"}, "reason": "Retrieve missing evidence."}}
            else:
                value = {"operation": {"type": "final_answer", "reason": "The source evidence is available."}}
        elif stage == "tool_result_check":
            value = {"status": "accepted", "message": "The tool result is usable.", "remaining_work": ""}
        elif stage in {"answer", "context_answer"}:
            self.answer_calls += 1
            data = json.loads(prompt)
            if data.get("answer_degradation"):
                content = "Partial status: source details remain unavailable, so the requested finding cannot be confirmed."
            elif self.read_calls == 0:
                content = "The source confirms revenue increased by 17%."
            else:
                content = "The source states revenue increased by 7%."
            return self._response(content, request)
        elif stage == "answer_review":
            self.review_calls += 1
            if self.timeout_review:
                raise LLMTimeoutError("synthetic reviewer timeout")
            data = json.loads(prompt)
            draft = data.get("draft_answer", "")
            if data.get("answer_degradation") and self.approve_degraded:
                value = {"faithful": True, "hallucination_free": True, "request_aligned": True,
                         "decision": "pass", "issues": [], "revision_instructions": "", "evidence_gap": ""}
            elif "17%" in draft:
                value = {"faithful": False, "hallucination_free": False, "request_aligned": True,
                         "decision": "continue", "issues": ["No source observation supports 17%."],
                         "revision_instructions": "Read the requested source before answering.",
                         "evidence_gap": "The source file has not been read."}
            else:
                value = {"faithful": True, "hallucination_free": True, "request_aligned": True,
                         "decision": "pass", "issues": [], "revision_instructions": "", "evidence_gap": ""}
        else:
            if stage == "safety_review":
                value = {"decision": "allow", "reason": "Synthetic low-risk test operation."}
            else:
                raise AssertionError(f"Unexpected synthetic stage: {stage}")
        return self._response(json.dumps(value), request)

    def _response(self, content, request):
        return LLMResponse(provider="synthetic_runtime", status="completed", content=content,
                           prompt_summary=request["prompt_summary"], finish_reason="stop",
                           usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def runtime(tmp_path, monkeypatch, orchestrator, *, timeout_review=False, approve_degraded=False):
    config_path = tmp_path / "local.toml"
    config_path.write_text(f'[agent]\norchestrator = "{orchestrator}"\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    app = create_app()
    script = RuntimeScriptedLLM(timeout_review=timeout_review, approve_degraded=approve_degraded)
    app.state.runtime.agent_turn_loop.llm_client = script
    app.state.runtime.agent_turn_loop.default_workspace_root = str(tmp_path)
    (tmp_path / "facts.txt").write_text("Revenue increased by 7%.", encoding="utf-8")
    return app.state.runtime, script


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_continue_reenters_agent_tools_and_only_publishes_reviewed_answer(tmp_path, monkeypatch, orchestrator):
    runtime_instance, script = runtime(tmp_path, monkeypatch, orchestrator)
    runner = runtime_instance.agent_turn_runner
    run = runner.create_run_for_turn(session_id="answer-review-runtime", user_input="Create audit.txt, read facts.txt, and report the revenue increase.")
    try:
        result = runner.run(session_id=run.session_id, user_input=run.user_input,
                            existing_run_id=run.run_id, llm_response_mode=LLMResponseMode.STREAM)
        events = runtime_instance.agent_run_manager.list_events(run.run_id)
        public = json.dumps([event.payload for event in events if event.type in {"final_answer", "llm_delta", "run_completed"}])
        assert result.answer == "The source states revenue increased by 7%."
        assert "17%" not in public
        assert "7%" in public
        assert sum(event.type == "final_answer" for event in events) == 1
        assert sum(event.type == "answer_review_completed" for event in events) == 2
        assert script.read_calls == 1
        assert script.create_calls == 1
        assert (tmp_path / "audit.txt").exists()
        assert runtime_instance.agent_run_manager.get_run(run.run_id).status.value == "completed"
    finally:
        runtime_instance.stop()
        get_settings.cache_clear()


def test_review_timeout_uses_fact_free_incomplete_notice_without_publishing_draft(tmp_path, monkeypatch):
    runtime_instance, script = runtime(tmp_path, monkeypatch, "langgraph", timeout_review=True)
    runner = runtime_instance.agent_turn_runner
    run = runner.create_run_for_turn(session_id="answer-review-timeout", user_input="Report the result.")
    try:
        result = runner.run(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        events = runtime_instance.agent_run_manager.list_events(run.run_id)
        assert result.answer
        assert "17%" not in result.answer
        assert "17%" not in json.dumps([event.payload for event in events])
        assert any(event.type == "final_answer" for event in events)
        state = runtime_instance.agent_run_manager.get_run(run.run_id)
        assert state.metadata["answer_review_approval_source"] == "system_status_notice"
        assert state.metadata["answer_review_degraded"] is True
        assert result.task_outcome == "incomplete"
        assert script.review_calls == 2
    finally:
        runtime_instance.stop()
        get_settings.cache_clear()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_rework_exhaustion_degraded_partial_completes_without_replaying_write(tmp_path, monkeypatch, orchestrator):
    runtime_instance, script = runtime(tmp_path, monkeypatch, orchestrator, approve_degraded=True)
    runtime_instance.agent_turn_loop.answer_review_max_reworks = 0
    runner = runtime_instance.agent_turn_runner
    run = runner.create_run_for_turn(
        session_id="answer-review-degraded-runtime",
        user_input="Create audit.txt, read facts.txt, and report the revenue increase.",
    )
    try:
        result = runner.run(session_id=run.session_id, user_input=run.user_input,
                            existing_run_id=run.run_id, llm_response_mode=LLMResponseMode.STREAM)
        events = runtime_instance.agent_run_manager.list_events(run.run_id)
        visible = json.dumps([event.payload for event in events if event.type in {
            "final_answer", "llm_delta", "run_completed", "answer_review_completed",
        }])
        state = runtime_instance.agent_run_manager.get_run(run.run_id)
        assert result.task_outcome == "incomplete"
        assert "17%" not in result.answer and "17%" not in visible
        assert result.answer.startswith("Partial status:")
        assert state.metadata["answer_review_degraded"] is True
        assert state.metadata["answer_review_approval_source"] == "llm"
        assert state.metadata["answer_review_reworks"] == 0
        assert script.create_calls == 1
        assert (tmp_path / "audit.txt").exists()
        assert sum(event.type == "final_answer" for event in events) == 1
    finally:
        runtime_instance.stop()
        get_settings.cache_clear()


@pytest.mark.parametrize(("orchestrator", "step_limit"), [("legacy", 3), ("langgraph", 2)])
def test_continue_with_no_decision_budget_delivers_reviewed_partial(tmp_path, monkeypatch, orchestrator, step_limit):
    runtime_instance, script = runtime(tmp_path, monkeypatch, orchestrator, approve_degraded=True)
    # Legacy also counts its initial local package expansion as a decision.
    runtime_instance.agent_turn_loop.max_decision_steps = step_limit
    runner = runtime_instance.agent_turn_runner
    run = runner.create_run_for_turn(
        session_id="answer-review-step-limit",
        user_input="Create audit.txt, read facts.txt, and report the revenue increase.",
    )
    try:
        result = runner.run(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        state = runtime_instance.agent_run_manager.get_run(run.run_id)
        assert state.status.value == "completed"
        assert result.task_outcome == "incomplete"
        assert result.stop_reason == "answer_review_degraded"
        assert result.answer.startswith("Partial status:")
        assert "17%" not in result.answer
        assert script.decision_calls == 2
        assert script.create_calls == 1 and script.read_calls == 0
        assert (tmp_path / "audit.txt").exists()
        assert state.metadata["answer_review_reworks"] == 1
        assert state.metadata["answer_review_approval_source"] == "llm"
    finally:
        runtime_instance.stop()
        get_settings.cache_clear()
