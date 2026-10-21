"""Advisory progress review must never bypass a tool's normal safety gate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.api.routes.agent import decide_agent_safety_review
from app.api.schemas import SafetyReviewDecisionRequest
from app.core.agent_graph import AgentTurnWaitingForConfirmation
from app.core.safety import SafetyReviewDecision, SafetyReviewMode
from tests.test_unified_entry_quality import operation, runtime_for


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_progress_review_does_not_skip_manual_safety_gate_for_write(
    tmp_path, monkeypatch, orchestrator,
):
    read_paths = [tmp_path / f"evidence-{index}.txt" for index in range(3)]
    for index, path in enumerate(read_paths):
        path.write_text(f"evidence {index}", encoding="utf-8")
    target = tmp_path / "target.txt"
    original = "BEFORE_WRITE"
    target.write_text(original, encoding="utf-8")
    expected_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()

    outputs = [
        operation("expand_package", package_name="filesystem"),
        *[
            operation("tool_call", tool_name="filesystem.read_file",
                      tool_input={"path": str(path)})
            for path in read_paths
        ],
        operation("tool_call", tool_name="filesystem.edit_file", tool_input={
            "path": str(target), "expected_sha256": expected_sha256,
            "edits": [{"old_text": original, "new_text": "WRITE_ONCE"}],
        }),
        operation("final_answer", reason="The proposed write was reviewed and denied."),
        "The file was left unchanged because the write was denied.",
    ]
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, outputs)
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.SKIP
    tool_invocations = {"filesystem.read_file": 0, "filesystem.edit_file": 0}
    for tool_name in tool_invocations:
        tool = runtime.tool_registry.get_tool(tool_name)
        original_invoke = tool.invoke

        def counted_invoke(*, invocation, context, _original=original_invoke, _name=tool_name):
            tool_invocations[_name] += 1
            result = _original(invocation=invocation, context=context)
            if _name == "filesystem.read_file" and tool_invocations[_name] == 3:
                # Keep the first three reads lightweight, then require ordinary
                # manual review for the following proposed write.
                runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.MANUAL
            return result

        monkeypatch.setattr(tool, "invoke", counted_invoke)
    run = runtime.create_agent_run(session_id="progress-review-safety", user_input="Inspect and update the file.")
    result_holder = {}

    def assert_write_is_waiting(review):
        assert review.tool_name == "filesystem.edit_file"
        assert review.mode == SafetyReviewMode.MANUAL
        assert review.status.value == "pending"
        assert runtime.agent_run_manager.get_run(run.run_id).status.value == "waiting_confirmation"
        assert target.read_text(encoding="utf-8") == original
        assert tool_invocations == {"filesystem.read_file": 3, "filesystem.edit_file": 0}
        assert len(client.calls) == 5  # expand, three reads, then the write; no review-only retry
        assert json.loads(client.calls[-1]["user_prompt"])["task_working_state"]["review_required"] is True

    async def deny_and_wait(review):
        await decide_agent_safety_review(
            review.review_id,
            SafetyReviewDecisionRequest(
                decision=SafetyReviewDecision.REJECT,
                reason="Regression test denies the proposed write.",
            ),
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime))),
        )
        end = time.monotonic() + 6
        while time.monotonic() < end:
            current = runtime.agent_run_manager.get_run(run.run_id)
            if current is not None and current.status.value == "completed":
                return
            if current is not None and current.status.value in {"failed", "cancelled"}:
                raise AssertionError(f"run did not recover after denial: {current.status.value}")
            await asyncio.sleep(0.01)
        raise AssertionError("run did not finish after the safety denial")

    if orchestrator == "langgraph":
        async def graph_flow():
            with pytest.raises(AgentTurnWaitingForConfirmation):
                await runtime.run_agent_turn_async(
                    session_id=run.session_id, user_input=run.user_input,
                    existing_run_id=run.run_id,
                )
            [review] = runtime.agent_run_manager.list_pending_safety_reviews()
            assert review.run_id == run.run_id
            assert_write_is_waiting(review)
            await deny_and_wait(review)

        try:
            asyncio.run(graph_flow())
        finally:
            runtime.stop()
        assert target.read_text(encoding="utf-8") == original
        review = next(
            item for item in runtime.agent_run_manager.list_safety_reviews(run.run_id)
            if item.tool_name == "filesystem.edit_file"
        )
        assert review.status.value == "rejected"
        assert tool_invocations == {"filesystem.read_file": 3, "filesystem.edit_file": 0}
        return

    def execute_turn():
        try:
            result_holder["result"] = runtime.run_agent_turn(
                session_id=run.session_id, user_input=run.user_input,
                existing_run_id=run.run_id,
            )
        except AgentTurnWaitingForConfirmation as exc:
            result_holder["waiting"] = exc
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the test thread
            result_holder["error"] = exc

    worker = threading.Thread(target=execute_turn, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 8
        review = None
        while time.monotonic() < deadline:
            pending = runtime.agent_run_manager.list_pending_safety_reviews()
            review = next((item for item in pending if item.run_id == run.run_id), None)
            if review is not None:
                break
            if not worker.is_alive():
                break
            time.sleep(0.01)

        assert review is not None, f"no pending review; worker={result_holder!r}"
        assert_write_is_waiting(review)
        asyncio.run(deny_and_wait(review))
        worker.join(timeout=8)
        assert not worker.is_alive(), "agent did not finish after the safety denial"
        assert "error" not in result_holder, repr(result_holder.get("error"))
        assert target.read_text(encoding="utf-8") == original
        assert runtime.agent_run_manager.get_safety_review(review.review_id).status.value == "rejected"
        assert tool_invocations == {"filesystem.read_file": 3, "filesystem.edit_file": 0}
    finally:
        runtime.stop()
