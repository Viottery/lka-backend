from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.api.routes.agent as agent_routes
from app.api.main import create_app
from app.api.routes.agent import (
    decide_agent_safety_review,
    get_agent_safety_review,
    list_agent_safety_review_queue,
    list_agent_run_safety_reviews,
    resume_agent_run,
)
from app.api.schemas import SafetyReviewDecisionRequest
from app.core.agent_graph import AgentTurnWaitingForConfirmation
from app.core.config import get_settings
from app.core.llm import LLMResponse
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewRequest
from app.storage.db import connect
from app.core.tools import ToolContext


def _app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    return create_app()


def test_tool_executor_rejects_non_read_only_without_safety_review(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path, monkeypatch)

    result = app.state.runtime.tool_executor.execute(
        invocation_id="matter_create_without_review",
        tool_name="matter.create",
        tool_input={
            "title": "Needs review",
            "summary": "Direct write should be rejected.",
        },
        context=ToolContext(session_id="session_safety_direct"),
    )

    assert result.status == "rejected"
    assert result.output["safety_review_required"] is True
    assert "approved safety review" in (result.error or "")


def test_registered_tools_declare_read_only(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    assert all(
        tool.read_only is not None
        for tool in app.state.runtime.tool_registry.list_tools()
    )


def test_agent_skip_review_records_and_executes_write_tool(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.runtime.agent_turn_loop.llm_client = _CreateMatterLLM()
    request = SimpleNamespace(app=app)

    response = app.state.runtime.run_agent_turn(
        session_id="session_safety_skip",
        user_input="创建一个本地事务",
    )

    assert response.answer == "已创建事务。"
    reviews = app.state.runtime.agent_run_manager.list_safety_reviews(response.run_id)
    assert len(reviews) == 1
    assert reviews[0].tool_name == "matter.create"
    assert reviews[0].mode == SafetyReviewMode.SKIP
    assert reviews[0].status == "approved"
    assert reviews[0].decided_by == "system.skip"
    assert app.state.runtime.search_matters(query="Safety review matter", limit=5).matters
    listed = asyncio.run(
        list_agent_run_safety_reviews(response.run_id, request)
    )
    assert listed.reviews[0].review_id == reviews[0].review_id


def test_request_cannot_lower_configured_safety_review_mode(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    loop = app.state.runtime.agent_turn_loop
    loop.safety_review_mode = SafetyReviewMode.MANUAL
    from app.core.agent_turn import _turn_safety_review_mode

    token = _turn_safety_review_mode.set(SafetyReviewMode.SKIP)
    try:
        assert loop._effective_safety_review_mode() == SafetyReviewMode.MANUAL
    finally:
        _turn_safety_review_mode.reset(token)


def test_manual_review_api_approves_waiting_agent_run(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.runtime.agent_turn_loop.llm_client = _CreateMatterLLM()
    request = SimpleNamespace(app=app)
    result_holder: dict[str, object] = {}

    def target() -> None:
        result_holder["response"] = app.state.runtime.run_agent_turn(
            session_id="session_safety_manual",
            user_input="创建一个需要人工确认的本地事务",
            safety_review_mode=SafetyReviewMode.MANUAL,
        )

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    review = _wait_for_review(app.state.runtime.agent_run_manager)

    run = app.state.runtime.agent_run_manager.get_run(review.run_id)
    assert run is not None
    assert run.status == "waiting_confirmation"
    assert review.mode == SafetyReviewMode.MANUAL

    fetched = asyncio.run(get_agent_safety_review(review.review_id, request))
    assert fetched.review_id == review.review_id
    assert "tool_input" not in fetched.model_dump()
    assert "llm_output" not in fetched.model_dump()

    queue = asyncio.run(list_agent_safety_review_queue(request))
    assert [item.review_id for item in queue.reviews] == [review.review_id]
    assert queue.reviews[0].parent_run_id is None
    assert queue.reviews[0].child_run_id is None
    assert queue.reviews[0].input_fields == ["priority", "status", "summary", "title"]
    assert len(queue.reviews[0].invocation_fingerprint) == 20
    assert "tool_input" not in queue.reviews[0].model_dump()

    decided = asyncio.run(
        decide_agent_safety_review(
            review.review_id,
            SafetyReviewDecisionRequest(
                decision=SafetyReviewDecision.APPROVE,
                reason="User approved the local matter write.",
            ),
            request,
        )
    )
    assert decided.status == "approved"

    thread.join(timeout=5)
    assert not thread.is_alive()
    response = result_holder["response"]
    assert response.answer == "已创建事务。"
    run = app.state.runtime.agent_run_manager.get_run(review.run_id)
    assert run is not None
    assert run.status == "completed"


def test_langgraph_manual_review_api_resumes_waiting_run(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text('[agent]\norchestrator = "langgraph"\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.MANUAL
    runtime.agent_turn_loop.llm_client = _CreateMatterLLM()
    run = runtime.create_agent_run(
        session_id="session_langgraph_safety_manual",
        user_input="创建一个需要人工确认的本地事务",
    )
    request = SimpleNamespace(app=app)

    async def run_review_flow() -> None:
        with pytest.raises(AgentTurnWaitingForConfirmation):
            await runtime.run_agent_turn_async(
                session_id=run.session_id,
                user_input=run.user_input,
                existing_run_id=run.run_id,
            )

        [review] = runtime.agent_run_manager.list_safety_reviews(run.run_id)
        assert review.status.value == "pending"
        runner = runtime.agent_turn_runner
        state = runner.get_state(run.run_id)
        runtime_data = runtime.agent_run_store.load_artifact(
            state.values["runtime_artifact_ref"]["artifact_id"]
        )
        assert runtime_data is not None
        assert any(
            event["type"] == "safety_review" and event["status"] == "required"
            for event in runtime_data["progress_events"]
        )
        decided = await decide_agent_safety_review(
            review.review_id,
            SafetyReviewDecisionRequest(
                decision=SafetyReviewDecision.APPROVE,
                reason="User approved the LangGraph tool call.",
            ),
            request,
        )
        assert decided.status == "approved"

        for _ in range(100):
            resumed = runtime.agent_run_manager.get_run(run.run_id)
            if resumed is not None and resumed.status.value == "completed":
                return
            await asyncio.sleep(0.01)
        raise AssertionError("LangGraph run did not resume after manual approval.")

    try:
        asyncio.run(run_review_flow())
        completed = runtime.agent_run_manager.get_run(run.run_id)
        assert completed is not None
        assert completed.status.value == "completed"
        assert [event.type for event in runtime.agent_run_manager.list_events(run.run_id)].count(
            "run_started"
        ) == 1
        assert len(runtime.agent_run_manager.list_safety_reviews(run.run_id)) == 1
    finally:
        runtime.stop()


def test_langgraph_resume_endpoint_recovers_running_checkpoint_after_restart(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        '[agent]\norchestrator = "langgraph"\ncheckpoint_backend = "sqlite"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()

    first_app = create_app()
    first_runner = first_app.state.runtime.agent_turn_runner
    first_runner.interrupt_after = ["initialize_run"]
    run = first_runner.create_run_for_turn(
        session_id="session_resume_endpoint", user_input="Resume from HTTP control plane."
    )
    asyncio.run(
        first_runner._invoke(
            run_id=run.run_id,
            session_id=run.session_id,
            user_input=run.user_input,
        )
    )
    first_app.state.runtime.stop()

    get_settings.cache_clear()
    second_app = create_app()
    request = SimpleNamespace(app=second_app)
    try:
        response = asyncio.run(resume_agent_run(run.run_id, request))
        assert response.status == "running"
        for _ in range(100):
            restored = second_app.state.runtime.agent_run_manager.get_run(run.run_id)
            if restored is not None and restored.status.value == "completed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("HTTP resume did not complete the checkpointed run.")
        assert second_app.state.runtime.agent_run_manager.get_run(run.run_id).status.value == (
            "completed"
        )
    finally:
        second_app.state.runtime.stop()


def test_safety_review_decision_api_rehydrates_run_after_runtime_restart(tmp_path, monkeypatch):
    first_app = _app(tmp_path, monkeypatch)
    first_manager = first_app.state.runtime.agent_run_manager
    run = first_manager.create_run(
        session_id="session_review_restart",
        user_input="approve after restart",
        trace_id="trace_review_restart",
    )
    review = first_manager.create_safety_review(
        SafetyReviewRequest(
            review_id="review_restart_api",
            run_id=run.run_id,
            session_id=run.session_id,
            trace_id=run.trace_id,
            invocation_id="invocation_restart_api",
            tool_name="filesystem.edit_file",
            mode=SafetyReviewMode.MANUAL,
            reason="Write requires confirmation.",
            created_at=run.created_at,
        )
    )
    first_app.state.runtime.stop()

    get_settings.cache_clear()
    second_app = create_app()
    request = SimpleNamespace(app=second_app)
    response = asyncio.run(
        decide_agent_safety_review(
            review.review_id,
            SafetyReviewDecisionRequest(
                decision=SafetyReviewDecision.APPROVE,
                reason="Approved after restart.",
            ),
            request,
        )
    )

    assert response.status == "approved"
    assert second_app.state.runtime.agent_run_manager.get_run(run.run_id) is not None
    second_app.state.runtime.stop()


def test_replaying_committed_safety_decision_recovers_run_after_restart(tmp_path, monkeypatch):
    first_app = _app(tmp_path, monkeypatch)
    first_manager = first_app.state.runtime.agent_run_manager
    run = first_manager.create_run(
        session_id="session_review_replay",
        user_input="recover an approved review",
        trace_id="trace_review_replay",
    )
    review = first_manager.create_safety_review(
        SafetyReviewRequest(
            review_id="review_replay_api",
            run_id=run.run_id,
            session_id=run.session_id,
            trace_id=run.trace_id,
            invocation_id="invocation_replay_api",
            tool_name="filesystem.edit_file",
            mode=SafetyReviewMode.MANUAL,
            reason="Write requires confirmation.",
            created_at=run.created_at,
        )
    )

    def crash_after_decision_transaction(*_args, **_kwargs):
        raise RuntimeError("simulated process loss after decision transaction")

    monkeypatch.setattr(first_manager, "_cache_persisted_event", crash_after_decision_transaction)
    request = SimpleNamespace(app=first_app)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        asyncio.run(
            decide_agent_safety_review(
                review.review_id,
                SafetyReviewDecisionRequest(
                    decision=SafetyReviewDecision.APPROVE,
                    reason="Approved before simulated crash.",
                ),
                request,
            )
        )
    durable_store = first_manager.durable_store
    assert durable_store is not None
    assert [
        event["type"] for event in durable_store.list_events(run.run_id)
    ].count("safety_review_decided") == 1
    first_app.state.runtime.stop()

    # Model a legacy crash from before decision events were transactional.
    with connect(durable_store.db_path) as conn:
        conn.execute(
            """
            DELETE FROM agent_run_events
            WHERE run_id = ?
              AND json_extract(event_payload, '$.type') = 'safety_review_decided'
              AND json_extract(event_payload, '$.payload.review.review_id') = ?
            """,
            (run.run_id, review.review_id),
        )

    get_settings.cache_clear()
    second_app = create_app()
    request = SimpleNamespace(app=second_app)
    scheduled: list[str] = []
    monkeypatch.setattr(
        agent_routes,
        "_start_agent_resume_task",
        lambda *, runtime, run_id: scheduled.append(run_id),
    )
    try:
        replayed = asyncio.run(
            decide_agent_safety_review(
                review.review_id,
                SafetyReviewDecisionRequest(
                    decision=SafetyReviewDecision.APPROVE,
                    reason="Replay the already committed approval.",
                ),
                request,
            )
        )
        restored = second_app.state.runtime.agent_run_manager.get_run(run.run_id)
        assert replayed.status == "approved"
        assert restored is not None and restored.status.value == "running"
        assert scheduled == [run.run_id]
        events = second_app.state.runtime.agent_run_manager.list_events(run.run_id)
        assert [event.type for event in events].count("safety_review_decided") == 1

        # A further idempotent replay must not append a second decision event.
        asyncio.run(
            decide_agent_safety_review(
                review.review_id,
                SafetyReviewDecisionRequest(
                    decision=SafetyReviewDecision.APPROVE,
                    reason="Repeated approval replay.",
                ),
                request,
            )
        )
        assert [
            event.type
            for event in second_app.state.runtime.agent_run_manager.list_events(run.run_id)
        ].count("safety_review_decided") == 1
    finally:
        second_app.state.runtime.stop()


def _wait_for_review(run_manager):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for run in run_manager._runs.values():
            reviews = run_manager.list_safety_reviews(run.run_id)
            if reviews:
                return reviews[0]
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for safety review.")


class _CreateMatterLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "matter",
                    "reason": "User asks to create a local matter.",
                    "search_query": "Safety review matter",
                }
            )
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload.get("observations", [])
            if not any(
                observation.get("tool_name") == "matter.create"
                for observation in observations
            ):
                content = json.dumps(
                    {
                        "assistant_message": "准备创建本地事务。",
                        "operation": {
                            "type": "tool_call",
                            "package_name": None,
                            "tool_name": "matter.create",
                            "tool_input": {
                                "title": "Safety review matter",
                                "summary": "Created after safety review.",
                                "status": "open",
                                "priority": "normal",
                            },
                            "final_answer": None,
                            "reason": "Persist the user requested matter.",
                            "confidence": "high",
                        },
                    }
                )
            else:
                content = json.dumps(
                    {
                        "assistant_message": "事务已经创建。",
                        "operation": {
                            "type": "final_answer",
                            "package_name": None,
                            "tool_name": None,
                            "tool_input": {},
                            "final_answer": None,
                            "reason": "matter.create completed.",
                            "confidence": "high",
                        },
                    }
                )
        elif "Final Answer Writer" in system_prompt:
            content = "已创建事务。"
        else:
            content = json.dumps({"status": "accepted", "message": "ok"})
        return LLMResponse(
            provider="fake_safety_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )
