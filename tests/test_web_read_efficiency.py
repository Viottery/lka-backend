"""Actual query/page dispatch and cached middle-text delivery, not LLM gold."""

import json
from datetime import UTC, datetime

from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import AgentTurnLoop
from app.core.tool_result_gate import bounded_preview, contiguous_preview_fragments, needs_gate
from app.core.tools import ToolExecutor, ToolResult
from app.tool_packages.observation import ObservationReadTool
from tests import test_web_snapshots
from tests.test_answer_generation_recovery_quality import make_loop, response, scope

web = test_web_snapshots.web


def test_query_with_size_budget_is_not_silently_changed_into_prefix_page(web):
    web.state["html"] = "<main>" + "<p>Unrelated material.</p>" * 1800 + (
        "<p>Unique-target supports export.</p><p>Only if explicitly enabled.</p></main>")
    selected = web.call("web.open", {"url": "https://8.8.8.8/query", "query": "Unique-target",
                                     "max_chars": 6000}).output
    assert selected["view_mode"] == "overview" and selected["query_status"] == "lexical_match"
    assert selected["offset"] > 20000 and "Only if explicitly enabled." in selected["text"]
    for explicit in ({"view": "page"}, {"offset": 0}):
        result = web.call("web.open", {"snapshot_id": selected["snapshot_id"], "query": "Unique-target",
                                       "max_chars": 80, **explicit}).output
        assert result["view_mode"] == "page" and result["offset"] == 0
        assert result["query_status"] == "not_applied_page" and "Unique-target" not in result["text"]
    assert len(web.calls) == 1


def test_large_contiguous_page_middle_survives_storage_gate_and_actual_provider(web, tmp_path):
    condition = "Critical middle condition: only with independent approval."
    web.state["html"] = "<main><p>" + "a" * 1600 + condition + "b" * 6000 + "</p></main>"
    page = web.call("web.open", {"url": "https://8.8.8.8/page", "max_chars": 4000})
    raw = page.model_dump(mode="json")
    # Force the large-result boundary without relying on incidental cache fields.
    raw["output"]["diagnostics"] = "d" * 3000
    assert needs_gate(raw)
    store = SqliteAgentRunStore(tmp_path / "provider.sqlite3")
    loop, provider, manager = make_loop(tmp_path, [response("Approval is required.")])
    loop.tool_executor = ToolExecutor(web.registry)
    loop.tool_invocation_store = store
    with scope(manager) as run:
        store.put_artifact(artifact_id=f"tool_result_{page.invocation_id}", run_id=run.run_id,
            kind="tool_result", payload=raw, summary="contiguous evidence", created_at=datetime.now(UTC).isoformat())
        restored = store.load_tool_result_artifact(f"tool_result_{page.invocation_id}", run.run_id)
        observed = loop._observation_for_decision_prompt(tool_name=page.tool_name, tool_input={},
            tool_result=ToolResult.model_validate(restored), feedback={}, run_id=run.run_id)
        assert condition in json.dumps(observed)
        assert len(json.dumps(observed["result"], ensure_ascii=False)) <= 7000
        loop._answer_with_llm(user_input="What condition applies?", route={}, context_window={},
            observations=[observed], final_decision=None, llm_events=[])
    fitted = json.loads(provider.requests[0].messages[1].content)
    assert condition in json.dumps(fitted)
    receipt = next(r for r in fitted["context_delivery"] if r["path"] == "/output/text")
    assert receipt["ranges"] == [[0, 4000]] and receipt["coverage"] == "complete"
    assert receipt["upstream_coverage"] == "unknown" and len(web.calls) == 1


def test_contiguous_windows_stay_bounded_and_unknown_tool_remains_head_tail():
    text = "甲🙂e\u0301" * 2500
    raw = {"output": {"text": text, "handle": "stable"}}
    view = bounded_preview(raw, max_string_chars=1200, output_priority_fields=["text", "handle"],
                           text_mode="contiguous_pages")
    field = view["output"]["text"]
    parts = contiguous_preview_fragments(text, field, path="/output/text")
    assert parts and "".join(part["text"] for part in parts) == text[:field["visible_chars"]]
    assert field["next_offset"] == field["visible_chars"] and field["_partial"] is True
    assert len(json.dumps(view, ensure_ascii=False)) <= 7000
    field["fragments"][1]["text"] = "altered"
    assert contiguous_preview_fragments(text, field, path="/output/text") is None
    unknown = bounded_preview(raw)
    assert "head" in unknown["output"]["text"] and "fragments" not in unknown["output"]["text"]


def test_cached_read_offset_maps_chunk_pages_to_original_cached_string(web, tmp_path):
    text = "x" * 1500 + "Specific middle requirement." + "y" * 6000
    store = SqliteAgentRunStore(tmp_path / "read.sqlite3")
    store.put_artifact(artifact_id="cached-string", run_id="r", kind="tool_result",
        payload={"output": {"text": text}}, summary="original", created_at=datetime.now(UTC).isoformat())
    reader = ObservationReadTool(store)
    web.registry.register_tool(reader)
    result = web.call("observation.read", {"artifact_id": "cached-string", "path": "/output/text",
        "offset": 400, "max_chars": 4000})
    view = {"result": bounded_preview(result.model_dump(mode="json"), max_string_chars=1200,
        output_priority_fields=reader.spec.output_preview_priority_fields, text_mode="contiguous_pages")}
    bindings = reader.context_delivery_bindings(result_payload=result.model_dump(mode="json"),
        view_payload=view, context=web.context())
    assert len(bindings) == 1
    parts = bindings[0]["fragments"]
    assert parts[0]["start"] == 400 and parts[-1]["end"] == 4400
    assert "Specific middle requirement." in "".join(p["text"] for p in parts)
    assert "".join(p["text"] for p in parts) == text[400:4400]


def test_result_body_cannot_enable_contiguous_pages_for_unknown_tool(web):
    loop = object.__new__(AgentTurnLoop)
    loop.tool_executor = ToolExecutor(web.registry)
    loop.tool_invocation_store = object()
    result = ToolResult(invocation_id="unknown", tool_name="unregistered.mcp", status="completed",
        output={"text": "a" * 5000, "output_preview_text_mode": "contiguous_pages"})
    observed = loop._observation_for_decision_prompt(tool_name=result.tool_name, tool_input={},
        tool_result=result, feedback={}, run_id="r")
    assert "head" in observed["result"]["output"]["text"]
    assert "fragments" not in observed["result"]["output"]["text"]


def test_small_forced_control_preview_preserves_structure_without_expanding_past_cap(web):
    loop = object.__new__(AgentTurnLoop)
    loop.tool_executor = ToolExecutor(web.registry)
    loop.tool_invocation_store = object()
    result = ToolResult(invocation_id="small", tool_name="web.open", status="completed",
        output={**{f"field_{i}": str(i) * 1350 for i in range(4)}, "missing": ["approval"],
                "marker": False, **{f"status_{i}": i for i in range(20)}})
    raw = result.model_dump(mode="json")
    assert not needs_gate(raw)
    observed = loop._observation_for_decision_prompt(tool_name=result.tool_name, tool_input={},
        tool_result=result, feedback={}, run_id="r", force_gate=True)
    view = observed["result"]
    assert len(json.dumps(view, ensure_ascii=False)) <= 7000
    assert view["output"]["missing"] == ["approval"] and view["output"]["marker"] is False
    assert all(view["output"][f"status_{i}"] == i for i in range(20))
