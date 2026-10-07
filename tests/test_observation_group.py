from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.agent_storage import SqliteAgentRunStore
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext, ToolExecutor, ToolInvocation, ToolPackageSpec, ToolRegistry
from app.tool_packages.observation_group import ObservationGroupTool


def _store(tmp_path, rows):
    store = SqliteAgentRunStore(tmp_path / "agent.sqlite3")
    store.put_artifact(
        artifact_id="group-artifact", run_id="run-1", kind="tool_result",
        payload={"output": {"rows": rows}}, summary="rows",
        created_at=datetime.now(UTC).isoformat(),
    )
    return store


def _invoke(tool, *, run_id="run-1", tool_view=None, **tool_input):
    return tool.invoke(
        invocation=ToolInvocation(
            invocation_id="group-call", tool=tool.spec, session_id="session-1",
            context_id="context-1", input=tool_input,
        ),
        context=ToolContext(session_id="session-1", run_id=run_id, tool_view=tool_view),
    )


def test_groups_raw_cached_rows_with_exact_counts_across_pages(tmp_path):
    rows = [
        {"team": f"队{i % 30:02d}", "id": i, "unused": "x" * 900}
        for i in range(60)
    ]
    tool = ObservationGroupTool(_store(tmp_path, rows))
    pages = []
    offset = 0
    while True:
        result = _invoke(
            tool, artifact_id="group-artifact", path="/output/rows", field="team",
            sample_fields=["id"], offset=offset, limit=7,
        )
        assert result.status == "completed", result.error
        pages.extend(result.output["groups"])
        if result.output["next_offset"] is None:
            break
        offset = result.output["next_offset"]

    assert [group["count"] for group in pages] == [2] * 30
    assert [group["key"]["value"] for group in pages] == [f"队{i:02d}" for i in range(30)]
    assert all(len(group["samples"]) <= 2 for group in pages)
    assert sum(group["count"] for group in pages) == 60
    assert result.output["total_records"] == result.output["scanned"] == 60
    assert result.output["groups_total"] == 30
    assert result.output["scan_complete"] is True


def test_missing_null_bool_and_number_types_remain_distinct(tmp_path):
    rows = [
        {}, {"value": None}, {"value": False}, {"value": 0}, {"value": 0.0},
        {"value": True}, {"value": "0"},
    ]
    result = _invoke(
        ObservationGroupTool(_store(tmp_path, rows)), artifact_id="group-artifact",
        path="/output/rows", field="value", limit=20,
    )

    assert result.status == "completed", result.error
    groups = result.output["groups"]
    assert len(groups) == 7
    assert sum(group["count"] for group in groups) == 7
    assert sum(group["key"]["type"] == "missing" for group in groups) == 1
    assert {(group["key"].get("type"), repr(group["key"].get("value"))) for group in groups} == {
        ("missing", "None"), ("null", "None"), ("bool", "False"), ("bool", "True"),
        ("int", "0"), ("float", "0.0"), ("string", "'0'"),
    }


def test_skips_non_objects_and_non_scalar_group_values_explicitly(tmp_path):
    rows = [
        {"v": "ok", "id": 1}, "not an object", {"v": ["nested"], "id": 2},
        {"id": 3}, {"v": None, "id": 4},
    ]
    result = _invoke(
        ObservationGroupTool(_store(tmp_path, rows)), artifact_id="group-artifact",
        path="/output/rows", field="v", sample_fields=["id"], limit=20,
    )

    assert result.status == "completed", result.error
    assert result.output["total_records"] == 5
    assert result.output["scanned"] == 5
    assert result.output["skipped_non_object"] == 1
    assert result.output["skipped_non_scalar"] == 1
    assert sum(group["count"] for group in result.output["groups"]) == 3
    assert result.output["scan_complete"] is True


def test_bounds_samples_nested_values_and_serialized_output(tmp_path):
    rows = [
        {"group": "common", "id": index, "note": "界" * 1000, "nested": {"secret": "x" * 8000}}
        for index in range(60)
    ]
    result = _invoke(
        ObservationGroupTool(_store(tmp_path, rows)), artifact_id="group-artifact",
        path="/output/rows", field="group", sample_fields=["id", "note", "nested"],
        limit=20,
    )

    assert result.status == "completed", result.error
    assert len(json.dumps(result.output, ensure_ascii=False)) < 5_500
    group = result.output["groups"][0]
    assert group["count"] == 60
    assert len(group["samples"]) <= 2
    assert len(group["samples"][0]["fields"]["note"]["value"]) == 256
    assert group["samples"][0]["fields"]["note"]["truncated"] is True
    assert group["samples"][0]["fields"]["nested"] == {"_type": "object", "preview_omitted": True}


def test_output_reduces_page_without_dropping_group_keys_or_misreporting_offset(tmp_path):
    rows = [{"key": f"group-{index:02d}-" + "k" * 990} for index in range(20)]
    result = _invoke(
        ObservationGroupTool(_store(tmp_path, rows)), artifact_id="group-artifact",
        path="/output/rows", field="key", limit=20,
    )

    assert result.status == "completed", result.error
    assert len(json.dumps(result.output, ensure_ascii=False).encode("utf-8")) <= 5_500
    assert 0 < len(result.output["groups"]) < 20
    assert result.output["groups_total"] == 20
    assert result.output["next_offset"] == len(result.output["groups"])
    assert all(len(group["key"]["value"]) > 900 for group in result.output["groups"])


def test_rejects_record_group_and_key_length_budgets(tmp_path):
    tool = ObservationGroupTool(_store(tmp_path, [{"v": str(index)} for index in range(5001)]))
    too_many_groups = _invoke(
        tool, artifact_id="group-artifact", path="/output/rows", field="v", limit=20,
    )
    assert too_many_groups.status == "rejected"
    assert "group limit" in (too_many_groups.error or "")

    too_long_key = _invoke(
        ObservationGroupTool(_store(tmp_path, [{"v": "k" * 1025}])),
        artifact_id="group-artifact", path="/output/rows", field="v",
    )
    assert too_long_key.status == "rejected"
    assert "1024" in (too_long_key.error or "")

    too_many_records = _invoke(
        ObservationGroupTool(_store(tmp_path, [{"v": "same"}] * 100_001)),
        artifact_id="group-artifact", path="/output/rows", field="v",
    )
    assert too_many_records.status == "rejected"
    assert "record limit" in (too_many_records.error or "")


def test_output_byte_budget_includes_normal_json_framing(tmp_path):
    for key_length in range(990, 1020):
        rows = [{"key": f"{index:02d}-" + "k" * key_length} for index in range(20)]
        result = _invoke(
            ObservationGroupTool(_store(tmp_path, rows)), artifact_id="group-artifact",
            path="/output/rows", field="key", limit=20,
        )
        assert result.status == "completed"
        assert len(json.dumps(result.output, ensure_ascii=False).encode("utf-8")) <= 5_500


def test_validates_arguments_and_enforces_current_run_child_scope(tmp_path):
    tool = ObservationGroupTool(_store(tmp_path, [{"v": "ok"}]))
    base = {"artifact_id": "group-artifact", "path": "/output/rows", "field": "v"}
    for invalid in (
        {**base, "field": ""}, {**base, "field": "x" * 65},
        {**base, "offset": True}, {**base, "limit": 21},
        {**base, "sample_fields": []}, {**base, "sample_fields": ["a"] * 7},
        {**base, "sample_fields": ["x" * 65]},
    ):
        assert _invoke(tool, **invalid).status == "rejected"

    assert _invoke(tool, run_id=None, **base).status == "rejected"
    mismatched_child = ToolView(
        snapshot_id="child", child_run_id="other-run", side_effect_level=SideEffectLevel.READ,
    )
    assert _invoke(tool, tool_view=mismatched_child, **base).status == "rejected"
    assert _invoke(tool, run_id="other-run", **base).status == "rejected"

    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="observation", description="cached results"))
    registry.register_tool(tool)
    executor_result = ToolExecutor(registry).execute(
        invocation_id="schema-check", tool_name="observation.group",
        tool_input={**base, "limit": 21}, context=ToolContext(session_id="session-1", run_id="run-1"),
    )
    assert executor_result.status == "rejected"
    assert executor_result.output["safety_review_required"] is True
    assert executor_result.execution_started is False
