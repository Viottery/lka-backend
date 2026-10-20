"""Audited model retries preserve partial output, cancellation and deadline fences."""

import time
from contextlib import asynccontextmanager, contextmanager

import pytest

from app.core.agent_runs import AgentRunCancelled
from app.core.llm import LLMNetworkError, LLMStreamEvent, LLMTimeoutError
from app.core.llm.errors import LLMResourceError
from app.core.tools import ToolContext
from tests.test_answer_generation_recovery_quality import make_loop, response, scope
from tests.test_control_generation_quality import _decide
from tests.test_control_recovery_budget_quality import FINISH, calibrated


def deadline(manager, run, value):
    manager._update_run(run.run_id, status=run.status,
                        metadata_patch={"execution_deadline_unix_seconds": value})


@pytest.mark.parametrize("stream", [False, True])
def test_network_control_recovery_has_separate_audit_and_no_thinking_downgrade(tmp_path, stream):
    loop, provider, manager = make_loop(tmp_path, [LLMNetworkError("disconnected"), response(FINISH)],
                                        selected_thinking="deepseek")
    with scope(manager, stream=stream) as run:
        events = []
        assert _decide(loop, events)["action"] == "final_answer"
        audit = manager.list_events(run.run_id)
    assert len(provider.requests) == 2
    assert [request.thinking_enabled for request in provider.requests] == [None, None]
    started = [event for event in audit if event.type == "llm_started"]
    assert len(started) == 2 and started[0].payload["llm_call_id"] != started[1].payload["llm_call_id"]
    failure = next(event for event in audit if event.type == "llm_failed")
    assert failure.payload["error_category"] == "network_failed"
    assert failure.payload["audit_record"]["total_token_count"] is None
    assert failure.payload["audit_record"]["is_retriable"] is True
    assert any(event.type == "llm_retry_scheduled" for event in audit)


def test_explicit_single_attempt_does_not_retry_network(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [LLMNetworkError("disconnected"), response(FINISH)])
    loop.llm_max_attempts = 1
    with scope(manager):
        assert _decide(loop, []) is None
    assert len(provider.requests) == 1


@pytest.mark.parametrize("error_type", [LLMNetworkError, LLMTimeoutError])
def test_partial_control_stream_is_preserved_and_never_restarted(tmp_path, error_type):
    loop, provider, manager = make_loop(tmp_path, [])
    error = error_type("stream lost")
    error.headers = {"x-request-id": "partial-request"}
    error.provider_request_id = "partial-request"
    error.partial_content = "unfinished control"
    error.usage = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    error.tool_calls = []

    async def stream(request):
        provider.requests.append(request)
        yield LLMStreamEvent(event_type="llm_delta", stage="decision", client_name="selected",
                             provider="test", model="test-model", delta="unfinished control",
                             content_snapshot="unfinished control")
        raise error

    provider.stream = stream
    with scope(manager, stream=True) as run:
        assert _decide(loop, []) is None
        failure = next(event for event in manager.list_events(run.run_id) if event.type == "llm_failed")
    assert len(provider.requests) == 1
    record = failure.payload["audit_record"]
    assert record["partial"] is True
    assert record["provider_request_id"] == "partial-request"
    assert record["total_token_count"] == 16
    assert record["content_length"] == len("unfinished control")


def test_cancel_after_network_failure_prevents_retry(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [])
    with scope(manager) as run:
        async def cancelled(request):
            provider.requests.append(request)
            manager.request_cancel(run.run_id)
            raise LLMNetworkError("lost connection")
        provider.complete = cancelled
        with pytest.raises(AgentRunCancelled):
            _decide(loop, [])
    assert len(provider.requests) == 1


def test_child_network_failure_retains_delivery_call_and_unknown_usage_reservation(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [LLMNetworkError("lost connection"), response("Partial result.")])
    calibrated(loop, monkeypatch)
    with scope(manager, child_budget={"max_llm_calls": 2, "max_tokens": 100_000}) as run:
        result = _decide(loop, [])
        assert result["action"] == "final_answer"
        assert len(provider.requests) == 1
        reservation = next(event.payload["budget_token_reservation"] for event in manager.list_events(run.run_id)
                           if event.type == "llm_started")
        assert loop._child_budget_for_prompt()["consumed_tokens"] == reservation


def test_deadline_expiry_prevents_model_dispatch(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(FINISH)])
    with scope(manager) as run:
        deadline(manager, run, time.time() - 1)
        with pytest.raises(LLMResourceError, match="deadline"):
            _decide(loop, [])
    assert not provider.requests


def test_deadline_caps_provider_timeout_and_remaining_time_is_visible(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(FINISH)])
    with scope(manager) as run:
        deadline(manager, run, time.time() + 30)
        assert _decide(loop, [])["action"] == "final_answer"
        assert 0 < provider.requests[0].metadata["network_timeout_seconds"] <= 30
        assert loop._remaining_execution_seconds() <= 30


def test_expiry_during_admission_does_not_dispatch(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [response(FINISH)])
    with scope(manager) as run:
        deadline(manager, run, time.time() + 30)

        @contextmanager
        def admit(_provider):
            deadline(manager, run, time.time() - 1)
            yield

        monkeypatch.setattr(loop, "_admit_llm", admit)
        with pytest.raises(LLMResourceError, match="deadline"):
            _decide(loop, [])
    assert not provider.requests


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_service_admission_rechecks_deadline_and_cancellation(tmp_path, monkeypatch, stream, cancel):
    from app.core.llm_workloads import UsageTicket, current_workload

    loop, provider, manager = make_loop(tmp_path, [response(FINISH)])
    tickets = []
    with scope(manager, stream=stream) as run:
        deadline(manager, run, time.time() + 30)

        @asynccontextmanager
        async def admission(_request):
            ticket = UsageTicket("queued", current_workload(), 10, 10)
            tickets.append(ticket)
            if cancel:
                manager.request_cancel(run.run_id)
            else:
                deadline(manager, run, time.time() - 1)
            yield ticket

        monkeypatch.setattr(loop.llm_client, "_admission", admission)
        with pytest.raises(AgentRunCancelled if cancel else LLMResourceError):
            _decide(loop, [])
    assert not provider.requests
    assert len(tickets) == 1 and not tickets[0].dispatched


@pytest.mark.parametrize("stream", [False, True])
def test_provider_dispatch_guard_preserves_existing_workload_fence(tmp_path, stream):
    from app.core.llm_workloads import current_workload, workload_scope

    loop, provider, manager = make_loop(tmp_path, [response(FINISH)])
    checked = []

    async def existing_fence():
        checked.append(current_workload().task_id)

    with scope(manager, stream=stream), workload_scope("interactive", task_id="owned-task",
                                                     max_tokens=10_000, before_dispatch=existing_fence):
        assert _decide(loop, [])["action"] == "final_answer"
        assert current_workload().before_dispatch is existing_fence
    assert checked == ["owned-task"]
    assert len(provider.requests) == 1


@pytest.mark.parametrize("graph", [False, True])
def test_expired_pending_tool_cannot_cross_executor_boundary(tmp_path, graph):
    loop, _provider, manager = make_loop(tmp_path, [])
    with scope(manager) as run:
        deadline(manager, run, time.time() - 1)
        context = ToolContext(session_id=run.session_id, trace_id=run.trace_id)
        # A pre-approved graph operation and a legacy operation use the same
        # deadline guard immediately before executing the actual tool.
        loop.tool_executor = type("NeverExecutor", (), {"execute": lambda *_args, **_kwargs: pytest.fail("expired tool executed")})()
        if graph:
            with pytest.raises(LLMResourceError, match="deadline"):
                loop.execute_graph_tool(invocation_id="pending", tool_name="example.write", tool_input={},
                                        context=context, approved_review=None, tool_events=[], progress_events=[])
        else:
            # Standalone safety review would need a registry; intercept only
            # that completed phase so the actual dispatch fence is exercised.
            loop._review_tool_call_before_execute = lambda **_kwargs: (None, None)
            with pytest.raises(LLMResourceError, match="deadline"):
                loop._execute_tool(tool_name="example.write", tool_input={}, context=context,
                                   tool_events=[], progress_events=[])
