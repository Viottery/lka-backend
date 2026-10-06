from __future__ import annotations

import json
from datetime import UTC, datetime

from app.api.main import create_app
from app.core.agent_turn import AgentTurnLoop
from app.core.config import get_settings
from app.core.tool_result_gate import bounded_preview, needs_gate
from app.core.tools import ToolContext, ToolResult


def test_generic_gate_exposes_recoverable_handle_without_full_payload():
    loop = object.__new__(AgentTurnLoop)
    loop.tool_invocation_store = object()
    result = ToolResult(
        invocation_id="inv-1", tool_name="unknown.mcp_tool", status="completed",
        output={"rows": [{"text": f"row-{index}:" + "x" * 1500} for index in range(80)]},
    )

    observed = loop._observation_for_decision_prompt(
        tool_name=result.tool_name, tool_input={}, tool_result=result,
        feedback={"status": "completed"}, run_id="run-1",
    )
    prompt_view = loop._observations_within_prompt_budget([observed])

    assert observed["_result_cache"]["artifact_id"] == "tool_result_inv-1"
    assert observed["_result_cache"]["read_tool"] == "observation.read"
    assert observed["result"]["output"]["rows"]["total_count"] == 80
    assert observed["result"]["output"]["rows"]["omitted_count"] == 76
    assert "row-79" not in json.dumps(prompt_view)
    assert len(json.dumps(prompt_view)) < 16_000
    assert prompt_view[0]["_result_cache"] == observed["_result_cache"]


def test_adversarially_wide_preview_retains_root_paths():
    value = {f"key-{index}": ["x" * 900] * 4 for index in range(100)}
    result = bounded_preview(value)
    assert len(json.dumps(result)) < 7_000
    assert result["total_keys"] == 100
    assert result["fields"][0]["path"] == "/key-0"


def test_small_serialized_but_long_list_is_gated():
    assert needs_gate({"output": {"ids": list(range(40))}})


def test_registered_priority_survives_sorting_and_cap_pressure_without_mutating_raw():
    raw = {"invocation_id": "large", "tool_name": "custom.inspect", "status": "completed", "output": {
        **{f"diagnostic_{i}": "d" * 1300 for i in range(30)},
        "evidence": [{"snippet": f"Necessary condition {i}. " + "x" * 1150} for i in range(5)],
        "handle": "immutable-version", "next_offset": 5,
    }}
    raw = json.loads(json.dumps(raw, sort_keys=True))
    view = bounded_preview(raw, max_string_chars=1200,
                           output_priority_fields=["evidence", "handle", "next_offset"])
    assert len(json.dumps(view, ensure_ascii=False)) <= 7000
    assert view["output"]["handle"] == "immutable-version"
    assert "Necessary condition 0." in view["output"]["evidence"]["items"][0]["snippet"]
    assert view["output"]["evidence"]["omitted_count"] >= 1
    assert view["output"]["_partial"] is True
    assert len(raw["output"]["evidence"]) == 5 and "diagnostic_29" in raw["output"]


def test_fallback_overlong_keys_cannot_escape_size_cap_or_forge_truncated_pointers():
    for length in (300, 10000):
        value = {str(index) + "x" * length: ["y" * 1200] * 4 for index in range(30)}
        view = bounded_preview(value, max_string_chars=1200)
        assert len(json.dumps(view, ensure_ascii=False)) <= 7000
        assert view["total_keys"] == 30
        assert view["omitted_keys"] == 30 - len(view["fields"])
        assert all(field["path"] is None or field["path"][1:] in value for field in view["fields"])


def test_aggregate_budget_retains_omitted_artifact_handles():
    loop = object.__new__(AgentTurnLoop)
    observations = [
        {"result": ["x" * 5_000] * 3, "_result_cache": {"artifact_id": "tool_result_old"}},
        {"result": ["y" * 5_000] * 3, "_result_cache": {"artifact_id": "tool_result_new"}},
    ]
    bounded = loop._observations_within_prompt_budget(observations, max_chars=16_000)
    assert bounded[0]["omitted_result_artifacts"] == ["tool_result_old"]
    assert bounded[1]["_result_cache"]["artifact_id"] == "tool_result_new"


def test_registered_reader_uses_executor_and_rejects_other_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    first = runtime.create_agent_run(session_id="gate-session", user_input="first")
    second = runtime.create_agent_run(session_id="gate-session", user_input="second")
    runtime.agent_run_store.put_artifact(
        artifact_id="tool_result_gate", run_id=first.run_id, kind="tool_result",
        payload={"output": {"ids": list(range(25))}}, summary="test",
        created_at=datetime.now(UTC).isoformat(),
    )
    assert runtime.tool_registry.get_tool_or_none("observation.read") is not None
    assert runtime.tool_registry.get_tool_or_none("observation.search") is not None
    result = runtime.tool_executor.execute(
        invocation_id="read-gate", tool_name="observation.read",
        tool_input={"artifact_id": "tool_result_gate", "path": "/output/ids", "limit": 5},
        context=ToolContext(session_id="gate-session", run_id=first.run_id),
    )
    assert result.status == "completed"
    assert result.output["items"] == [0, 1, 2, 3, 4]
    denied = runtime.tool_executor.execute(
        invocation_id="read-other", tool_name="observation.read",
        tool_input={"artifact_id": "tool_result_gate"},
        context=ToolContext(session_id="gate-session", run_id=second.run_id),
    )
    assert denied.status == "rejected"
    found = runtime.tool_executor.execute(
        invocation_id="search-gate", tool_name="observation.search",
        tool_input={"artifact_id": "tool_result_gate", "query": "not in numeric results"},
        context=ToolContext(session_id="gate-session", run_id=first.run_id),
    )
    assert found.status == "completed"
    assert found.output["matches"] == []
    assert found.output["complete"] is True
    assert runtime.tool_registry.get_tool_or_none("observation.group") is not None
    runtime.agent_run_store.put_artifact(
        artifact_id="tool_result_groups", run_id=first.run_id, kind="tool_result",
        payload={"output": {"rows": [{"team": "a"}, {"team": "a"}, {"team": "b"}]}},
        summary="group test", created_at=datetime.now(UTC).isoformat(),
    )
    grouped = runtime.tool_executor.execute(
        invocation_id="group-gate", tool_name="observation.group",
        tool_input={"artifact_id": "tool_result_groups", "path": "/output/rows", "field": "team"},
        context=ToolContext(session_id="gate-session", run_id=first.run_id),
    )
    assert grouped.status == "completed"
    assert [group["count"] for group in grouped.output["groups"]] == [2, 1]
    denied_group = runtime.tool_executor.execute(
        invocation_id="group-other", tool_name="observation.group",
        tool_input={"artifact_id": "tool_result_groups", "path": "/output/rows", "field": "team"},
        context=ToolContext(session_id="gate-session", run_id=second.run_id),
    )
    assert denied_group.status == "rejected"
