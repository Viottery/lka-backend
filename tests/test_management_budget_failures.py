"""Local budget refusals stop real Harness turns before any further dispatch."""
import json
from contextlib import closing

import pytest

from app.core.config import Settings
from app.core.llm import LLMRateLimitError, LLMResponse, LLMService
from app.core.llm.audit import classify_openai_sdk_exception
from app.core.llm.errors import LLMResourceError
from app.core.llm.registry import LLMClientRegistry
from app.core.llm_workloads import BudgetQuota, WorkBudgetExceeded, workload_scope
from app.core.local_config import AgentConfig, LLMClientConfig, LLMProviderConfig, LocalAppConfig
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewMode
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded, MeteredClient
from tests.test_unified_entry_quality import operation


class OfflineProvider:
    name = "offline"
    default_model = "test-model"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        content = next(self.responses)
        if isinstance(content, BaseException):
            raise content
        return LLMResponse(provider="offline", client_name=self.name, model=self.default_model,
                           status="completed", content=content, prompt_summary=request.prompt_summary,
                           usage={"prompt_tokens": 10, "completion_tokens": 3})


def make_runtime(tmp_path, monkeypatch, orchestrator, responses, *, max_attempts=2, metered=True, blocked=False):
    config = LocalAppConfig(agent=AgentConfig(orchestrator=orchestrator, unified_entry_enabled=True))
    config.memory.enabled = False
    config.message_history.enabled = False
    config.llm.max_attempts = max_attempts
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    runtime = LocalKnowledgeAgentRuntime(Settings(LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path)))
    provider = OfflineProvider(responses)
    registry = LLMClientRegistry()
    budget = LiveBudget(tmp_path / "evaluation.sqlite3", usd_limit=1e-12 if blocked else 1, call_limit=1)
    registry.register_client(MeteredClient(provider, budget, allowed_model="test-model", protected_values=())
                             if metered else provider)
    service = LLMService(config=LLMProviderConfig(default_client="offline", clients=[
        LLMClientConfig(name="offline", default_model="test-model", context_window_tokens=100_000,
                        output_reserve_tokens=1024),
    ]), registry=registry)
    runtime.agent_turn_loop.llm_client = service
    return runtime, provider, budget, service


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
@pytest.mark.parametrize("refusal_stage", ["decision", "answer", "decision_repair"])
def test_budget_refusal_preserves_category_and_never_falls_back(tmp_path, monkeypatch, orchestrator, refusal_stage):
    first = operation("final_answer", reason="Answer the request")
    if refusal_stage == "decision_repair":
        fragment = operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(tmp_path / "fact.txt")})
        first = fragment + "\n" + fragment
    runtime, provider, budget, _ = make_runtime(tmp_path, monkeypatch, orchestrator, [first, "Never dispatched"],
                                               blocked=refusal_stage == "decision")
    run = runtime.create_agent_run(session_id="budget", user_input="Give a concise answer")
    try:
        with pytest.raises(LiveBudgetExceeded) as error:
            runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        assert error.value.error_category == "live_budget_exceeded"
        assert error.value.is_retriable is False
        assert len(provider.requests) == (0 if refusal_stage == "decision" else 1)
        events = runtime.agent_run_manager.list_events(run.run_id)
        failures = [event for event in events if event.type == "llm_failed"]
        assert len(failures) == 1
        expected_stage = "context_answer" if refusal_stage == "answer" and orchestrator == "langgraph" else refusal_stage
        assert failures[0].stage == expected_stage
        assert failures[0].payload["error_category"] == "live_budget_exceeded"
        assert failures[0].payload["is_retriable"] is False
        assert not any(event.type in {"run_completed", "llm_retry_scheduled"} for event in events)
        assert runtime.agent_run_manager.get_run(run.run_id).error_type == "live_budget_exceeded"
        assert runtime.agent_run_manager.get_run(run.run_id).status.value == "failed"
        assert [event for event in events if event.type == "run_failed"][-1].payload["error_category"] == "live_budget_exceeded"
        assert budget.snapshot()["groups"].get("llm", {}).get("calls", 0) == len(provider.requests)
    finally:
        runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_shared_work_quota_refusal_is_not_an_unknown_provider_failure(tmp_path, monkeypatch, orchestrator):
    runtime, provider, _, service = make_runtime(tmp_path, monkeypatch, orchestrator, [], metered=False)
    service.workloads = runtime.llm_workloads
    run = runtime.create_agent_run(session_id="quota", user_input="Answer briefly")
    try:
        with workload_scope("interactive", quotas=(BudgetQuota("blocked-work", max_calls=0),)), pytest.raises(WorkBudgetExceeded) as error:
            runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        assert error.value.is_retriable is False
        assert provider.requests == []
        failure = next(event for event in runtime.agent_run_manager.list_events(run.run_id) if event.type == "llm_failed")
        assert failure.payload["error_category"] == "work_budget_exhausted"
        assert failure.payload["is_retriable"] is False
        assert runtime.agent_run_manager.get_run(run.run_id).error_type == "work_budget_exhausted"
    finally:
        runtime.stop()


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_configured_ordinary_retry_count_reaches_harness(tmp_path, monkeypatch, max_attempts):
    errors = [LLMRateLimitError(status_code=429, message="Fixture rate limit", retry_after="0") for _ in range(max_attempts)]
    runtime, provider, _, _ = make_runtime(tmp_path, monkeypatch, "legacy", errors, max_attempts=max_attempts, metered=False)
    try:
        assert runtime.agent_turn_loop.llm_max_attempts == max_attempts
        events = []
        assert runtime.agent_turn_loop._complete_text_with_retry(stage="test", system_prompt="Test", user_prompt="Test",
                                                                 prompt_summary="retry config", max_output_tokens=100, llm_events=events) is None
        assert len(provider.requests) == max_attempts
        assert len(events) == max_attempts
    finally:
        runtime.stop()


def test_local_resource_exception_classification_is_explicit():
    error = LLMResourceError("Resource unavailable", error_category="resource_unavailable")
    assert classify_openai_sdk_exception(error) == ("resource_unavailable", False)


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_budget_failure_leaves_a_non_model_rejection_review_and_no_tool_effect(tmp_path, monkeypatch, orchestrator):
    target = tmp_path / "never-modified.txt"
    target.write_text("Original", encoding="utf-8")
    outputs = [operation("expand_package", package_name="filesystem"),
               operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(target)})]
    runtime, provider, _, _ = make_runtime(tmp_path, monkeypatch, orchestrator, outputs, metered=False)
    complete = provider.complete

    async def refuse_review(request):
        if request.metadata.get("stage") == "safety_review":
            raise LLMResourceError("No review quota", error_category="review_budget_exhausted")
        return await complete(request)

    monkeypatch.setattr(provider, "complete", refuse_review)
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.LLM
    run = runtime.create_agent_run(session_id="review", user_input="Read the requested local file")
    try:
        with pytest.raises(LLMResourceError, match="No review quota"):
            runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input, existing_run_id=run.run_id)
        assert len(provider.requests) == 2
        with closing(runtime._conn()) as conn:
            rows = conn.execute("SELECT review_payload FROM agent_safety_reviews WHERE run_id=?", (run.run_id,)).fetchall()
        assert len(rows) == 1
        review = json.loads(rows[0][0])
        assert review["decided_by"] == "system.llm_resource_unavailable"
        assert "not an explicit model rejection" in review["decision_reason"]
        assert target.read_text() == "Original"
        assert not any(event.type == "tool_started" for event in runtime.agent_run_manager.list_events(run.run_id))
    finally:
        runtime.stop()
