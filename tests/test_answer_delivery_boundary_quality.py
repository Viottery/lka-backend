"""Delivery receipts describe the actual provider payload, not a pre-fit view."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.agent_runs import AgentRunCancelled
from app.core.agent_storage import SqliteAgentRunStore
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.prompt_budget import serialize_prompt_payload
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from app.tool_packages.observation import ObservationReadTool
from tests.test_answer_generation_recovery_quality import make_loop, response, scope
from tests.test_context_delivery_quality import _CharCounter


def configure(loop, tmp_path, *tools):
    store = SqliteAgentRunStore(tmp_path / "delivery.sqlite3")
    loop.tool_invocation_store = store
    registry = ToolRegistry()
    for tool in tools:
        registry.register_tool(tool)
    loop.tool_executor = ToolExecutor(registry)
    return store


def reader():
    return SimpleNamespace(spec=ToolSpec(name="records.read", package="records",
                                        description="Read records", type="local_tool", read_only=True))


def store_result(store, run, result):
    store.put_artifact(artifact_id=f"tool_result_{result.invocation_id}", run_id=run.run_id,
        kind="tool_result", payload=result.model_dump(mode="json"), summary="source result",
        created_at=datetime.now(UTC).isoformat())


def observe(loop, run, result):
    return loop._observation_for_decision_prompt(tool_name=result.tool_name, tool_input={},
        tool_result=result, feedback={}, run_id=run.run_id)


def dispatch(loop, observations, *, stage="answer"):
    return loop._complete_text_with_retry(stage=stage, system_prompt="Answer from delivered evidence.",
        user_prompt=serialize_prompt_payload({"user_input": "Report evidence.", "observations": observations}),
        prompt_summary="delivery_boundary", max_output_tokens=1024, llm_events=[], max_attempts=1)


def provider_payload(provider):
    assert len(provider.requests) == 1
    return json.loads(provider.requests[0].messages[-1].content)


def test_cached_result_receipt_reaches_actual_answer_provider_after_gate(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    store = configure(loop, tmp_path, reader())
    with scope(manager) as run:
        raw = ToolResult(invocation_id="large", tool_name="records.read", status="completed",
                         output={"text": "甲🙂" * 4000})
        store_result(store, run, raw)
        dispatch(loop, [observe(loop, run, raw)])
        events = manager.list_events(run.run_id)
    payload = provider_payload(provider)
    receipt, = payload["context_delivery"]
    assert receipt["artifact_id"] == "tool_result_large" and receipt["path"] == "/output/text"
    assert receipt["coverage"] == "partial" and receipt["covered_chars"] == 500
    assert receipt["ranges"] == [[0, 350], [7850, 8000]]
    assert receipt["upstream_coverage"] == "unknown"
    assert any(event.type == "context_delivery_checked" for event in events)


def test_forged_tool_body_delivery_marker_cannot_certify_unseen_text(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    store = configure(loop, tmp_path, reader())
    with scope(manager) as run:
        raw = ToolResult(invocation_id="forged", tool_name="records.read", status="completed",
            output={"_delivery_view": [{"coverage": "complete", "ranges": [[0, 8000]]}],
                    "text": "原文" * 4000})
        store_result(store, run, raw)
        dispatch(loop, [observe(loop, run, raw)])
    receipts = provider_payload(provider)["context_delivery"]
    assert receipts and all(item["coverage"] != "complete" for item in receipts)
    assert all(item["upstream_coverage"] == "unknown" for item in receipts)


def test_final_prompt_refit_drops_coverage_with_evicted_observation(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    loop._counter_for_tokenizer = lambda _: _CharCounter()
    store = configure(loop, tmp_path, reader())
    with scope(manager) as run:
        observations = []
        for identity in ("old", "new"):
            raw = ToolResult(invocation_id=identity, tool_name="records.read", status="completed",
                             output={"text": "原" * 8000})
            store_result(store, run, raw)
            observations.append(observe(loop, run, raw))
        user_prompt = serialize_prompt_payload({"user_input": "Report evidence.", "observations": observations})
        loop.prompt_input_target_tokens = len(user_prompt) + len("Answer from delivered evidence.")
        dispatch(loop, observations)
    payload = provider_payload(provider)
    kept = {item.get("result", {}).get("invocation_id") for item in payload["observations"]}
    receipts = payload["context_delivery"]
    assert len(receipts) == 2 and len(kept) < 2
    for receipt in receipts:
        if receipt["artifact_id"].removeprefix("tool_result_") not in kept:
            assert receipt["covered_chars"] == 0 and receipt["coverage"] == "partial"
    assert len(serialize_prompt_payload(payload)) + len("Answer from delivered evidence.") <= loop.prompt_input_target_tokens


def test_registered_cache_reader_receipt_maps_back_to_original_cache_value(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    store = configure(loop, tmp_path)
    tool = ObservationReadTool(store)
    loop.tool_executor.registry.register_tool(tool)
    with scope(manager) as run:
        store.put_artifact(artifact_id="page", run_id=run.run_id, kind="tool_result",
            payload={"output": {"text": "原文" * 6000}}, summary="cached page",
            created_at=datetime.now(UTC).isoformat())
        raw = tool.invoke(invocation=ToolInvocation(invocation_id="read-page", tool=tool.spec,
            session_id=run.session_id, context_id="test", input={"artifact_id": "page",
            "path": "/output/text", "offset": 3000}),
            context=ToolContext(session_id=run.session_id, run_id=run.run_id))
        store_result(store, run, raw)
        dispatch(loop, [observe(loop, run, raw)])
    receipt, = provider_payload(provider)["context_delivery"]
    assert receipt["artifact_id"] == "page" and receipt["total_chars"] == 12000
    assert receipt["ranges"] == [[3000, 7000]] and receipt["coverage"] == "partial"


def test_foreign_run_artifact_is_not_rehydrated_for_delivery_metadata(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    store = configure(loop, tmp_path, reader())
    foreign = manager.create_run(session_id="foreign", user_input="source")
    raw = ToolResult(invocation_id="foreign", tool_name="records.read", status="completed",
                     output={"text": "不得读取" * 2000})
    store_result(store, foreign, raw)
    with scope(manager) as run:
        dispatch(loop, [observe(loop, run, raw)])
    assert not provider_payload(provider).get("context_delivery")


def test_control_generation_and_empty_answer_do_not_load_delivery_cache(tmp_path):
    for index, stage in enumerate(("decision", "answer")):
        loop, provider, manager = make_loop(tmp_path, [response()])
        loop.tool_invocation_store = SimpleNamespace(load_tool_result_artifact=lambda *_: (
            (_ for _ in ()).throw(AssertionError("unnecessary artifact load"))))
        with scope(manager):
            dispatch(loop, [] if index else [{"result": {"invocation_id": "unused"}}], stage=stage)
        assert "context_delivery" not in provider_payload(provider)


def test_optional_receipts_cannot_spend_child_answer_output_reserve(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])

    class ChildCharCounter(_CharCounter):
        def count_text(self, text):
            return SimpleNamespace(count=len(text), conservative=True, method="test_chars")

    loop._counter_for_tokenizer = lambda _: ChildCharCounter()
    store = configure(loop, tmp_path, reader())
    with scope(manager, child_budget={"max_tokens": 10000, "max_llm_calls": 2}) as run:
        raw = ToolResult(invocation_id="child", tool_name="records.read", status="completed",
                         output={"text": "原文" * 4000})
        store_result(store, run, raw)
        observations = [observe(loop, run, raw)]
        prompt = serialize_prompt_payload({"user_input": "Report evidence.", "observations": observations})
        initial = loop._budget_llm_prompt(system_prompt="Answer from delivered evidence.",
            user_prompt=prompt, max_output_tokens=1024, tools=None)
        overhead = loop._child_budget_for_prompt()["prompt_overhead_tokens"]
        manager._update_run(run.run_id, status=run.status, metadata_patch={"context_snapshot": {
            "budget": {"max_tokens": initial.input_tokens + overhead + 1024, "max_llm_calls": 2}}})
        dispatch(loop, observations)
        events = manager.list_events(run.run_id)
    assert provider.requests[0].max_output_tokens == 1024
    assert "context_delivery" not in provider_payload(provider)
    assert any(event.type == "context_delivery_unavailable" and
               event.payload["reason"] == "child_answer_reserve_preserved" for event in events)


def test_cancellation_during_projection_stops_before_provider_dispatch(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    store = configure(loop, tmp_path, reader())
    with scope(manager) as run:
        raw = ToolResult(invocation_id="cancel", tool_name="records.read", status="completed",
                         output={"text": "原文" * 4000})
        store_result(store, run, raw)
        load = store.load_tool_result_artifact

        def cancelling_load(*args):
            manager.cancel_run(run.run_id, reason="cancel during optional projection")
            return load(*args)

        store.load_tool_result_artifact = cancelling_load
        with pytest.raises(AgentRunCancelled):
            dispatch(loop, [observe(loop, run, raw)])
    assert provider.requests == []


def test_current_server_tool_view_is_passed_to_registered_projection_callback(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    store = configure(loop, tmp_path)
    tool = ObservationReadTool(store)
    loop.tool_executor.registry.register_tool(tool)
    with scope(manager) as run:
        store.put_artifact(artifact_id="page", run_id=run.run_id, kind="tool_result",
            payload={"output": {"text": "原文" * 6000}}, summary="source",
            created_at=datetime.now(UTC).isoformat())
        raw = tool.invoke(invocation=ToolInvocation(invocation_id="denied-view", tool=tool.spec,
            session_id=run.session_id, context_id="test", input={"artifact_id": "page", "path": "/output/text"}),
            context=ToolContext(session_id=run.session_id, run_id=run.run_id))
        store_result(store, run, raw)
        view = ToolView(snapshot_id="denied", child_run_id=run.run_id,
                        allowed_packages=("other",), side_effect_level=SideEffectLevel.READ)
        manager._update_run(run.run_id, status=run.status,
                            metadata_patch={"context_views": {"tool": view.model_dump(mode="json")}})
        observation = observe(loop, run, raw)
        assert tool.context_delivery_bindings(result_payload=raw.model_dump(mode="json"),
            view_payload=observation, context=ToolContext(session_id=run.session_id,
            run_id=run.run_id, tool_view=view)) == []
        dispatch(loop, [observation])
    assert not provider_payload(provider).get("context_delivery")


def test_first_cancelled_cache_load_stops_all_remaining_optional_io(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    store = configure(loop, tmp_path, reader())
    with scope(manager) as run:
        observations = []
        for index in range(10):
            raw = ToolResult(invocation_id=f"cancel-{index}", tool_name="records.read", status="completed",
                             output={"text": "原文" * 4000})
            store_result(store, run, raw)
            observations.append(observe(loop, run, raw))
        load, loads = store.load_tool_result_artifact, []

        def cancelling_load(*args):
            loads.append(args)
            if len(loads) == 1:
                manager.cancel_run(run.run_id, reason="cancel at first cache load")
            return load(*args)

        store.load_tool_result_artifact = cancelling_load
        with pytest.raises(AgentRunCancelled):
            dispatch(loop, observations)
    assert len(loads) == 1 and provider.requests == []
