from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.agent_storage import SqliteAgentRunStore
from app.core.tools import ToolContext, ToolInvocation
from app.tool_packages.observation import OBSERVATION_PACKAGE, ObservationReadTool


def _invoke(tool, *, run_id="run-1", **tool_input):
    return tool.invoke(
        invocation=ToolInvocation(
            invocation_id="observation-call",
            tool=tool.spec,
            session_id="session-1",
            context_id="context-1",
            input=tool_input,
        ),
        context=ToolContext(session_id="session-1", run_id=run_id),
    )


def _store(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "agent.sqlite3")
    now = datetime.now(UTC).isoformat()
    store.put_artifact(
        artifact_id="artifact-1", run_id="run-1", kind="tool_result",
        payload={"output": {"rows": list(range(12)), "nested": {"answer": "ok"}}},
        summary="result", created_at=now,
    )
    store.put_artifact(
        artifact_id="wrong-kind", run_id="run-1", kind="other",
        payload={"output": {"secret": True}}, summary="other", created_at=now,
    )
    store.put_artifact(
        artifact_id="other-run", run_id="run-2", kind="tool_result",
        payload={"output": {"secret": True}}, summary="foreign", created_at=now,
    )
    return store


def test_observation_package_contract_and_scoped_artifact_reads(tmp_path):
    store = _store(tmp_path)
    tool = ObservationReadTool(store)
    assert OBSERVATION_PACKAGE.name == "observation"
    assert tool.spec.name == "observation.read"
    assert tool.spec.read_only is True

    result = _invoke(tool, artifact_id="artifact-1", path="/output/rows", offset=4, limit=3)
    assert result.status == "completed"
    assert result.output["items"] == [4, 5, 6]
    assert result.output["total"] == 12
    assert result.output["has_more"] is True
    assert result.output["next_offset"] == 7

    nested = _invoke(tool, artifact_id="artifact-1", path="/output/nested/answer")
    assert nested.output["text"] == "ok"
    assert _invoke(tool, artifact_id="other-run").status == "rejected"
    assert _invoke(tool, artifact_id="wrong-kind").status == "rejected"
    assert _invoke(tool, artifact_id="missing").status == "rejected"


def test_observation_paginates_object_keys_and_bounds_long_strings(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(UTC).isoformat()
    store.put_artifact(
        artifact_id="large", run_id="run-1", kind="tool_result",
        payload={"output": {"long": "x" * 9000, **{f"k{i}": i for i in range(9)}}},
        summary="large", created_at=now,
    )
    tool = ObservationReadTool(store)

    page = _invoke(tool, artifact_id="large", path="/output", offset=2, limit=3)
    assert page.output["keys"] == ["k2", "k3", "k4"]
    assert page.output["has_more"] is True
    assert page.output["next_offset"] == 5

    text = _invoke(tool, artifact_id="large", path="/output/long")
    assert len(text.output["text"]) == 4000
    assert text.output["has_more"] is True
    assert text.output["next_offset"] == 4000


def test_observation_rejects_invalid_bounds_and_pointer(tmp_path):
    tool = ObservationReadTool(_store(tmp_path))
    assert _invoke(tool, artifact_id="artifact-1", offset=-1).status == "rejected"
    assert _invoke(tool, artifact_id="artifact-1", limit=21).status == "rejected"
    assert _invoke(tool, artifact_id="artifact-1", path="output").status == "rejected"
    assert _invoke(tool, artifact_id="artifact-1", run_id=None).status == "rejected"


def test_reader_page_stays_bounded_with_large_nested_items(tmp_path):
    store = _store(tmp_path)
    store.put_artifact(
        artifact_id="nested", run_id="run-1", kind="tool_result",
        payload={"output": {"rows": [{"a": "x" * 5000, "b": "y" * 5000} for _ in range(30)]}},
        summary="nested", created_at=datetime.now(UTC).isoformat(),
    )
    page = _invoke(ObservationReadTool(store), artifact_id="nested", path="/output/rows", limit=20)
    assert len(json.dumps(page.output)) < 7_000
    assert page.output["has_more"] is True
    assert 0 < page.output["next_offset"] <= 20
