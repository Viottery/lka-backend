from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.agent_storage import SqliteAgentRunStore
from app.core.context_driver import ToolView
from app.core.tools import ToolContext, ToolInvocation
from app.tool_packages.observation import ObservationReadTool


def _store(tmp_path, payload):
    store = SqliteAgentRunStore(tmp_path / "agent.sqlite3")
    store.put_artifact(
        artifact_id="rows", run_id="run-1", kind="tool_result", payload=payload,
        summary="cached rows", created_at=datetime.now(UTC).isoformat(),
    )
    store.put_artifact(
        artifact_id="foreign", run_id="run-2", kind="tool_result",
        payload={"output": {"rows": [{"subject": "private"}]}}, summary="foreign",
        created_at=datetime.now(UTC).isoformat(),
    )
    return store


def _invoke(tool, *, run_id="run-1", tool_view=None, **tool_input):
    return tool.invoke(
        invocation=ToolInvocation(
            invocation_id="projection-call", tool=tool.spec, session_id="session-1",
            context_id="context-1", input=tool_input,
        ),
        context=ToolContext(session_id="session-1", run_id=run_id, tool_view=tool_view),
    )


def test_reader_projects_requested_late_fields_without_changing_cached_payload(tmp_path):
    payload = {"output": {"rows": [
        {"chunkid": "c1", "documentid": "d1", "folder": "Inbox", "messageid": "m1",
         "sender": "Ada", "subject": "Launch", "body": "details", "nested": {"secret": "x"}},
    ]}}
    store = _store(tmp_path, payload)
    tool = ObservationReadTool(store)
    result = _invoke(tool, artifact_id="rows", path="/output/rows", fields=["sender", "subject", "missing", "nested"])
    assert result.status == "completed"
    assert result.output["projected_fields"] == ["sender", "subject", "missing", "nested"]
    item = result.output["items"][0]
    assert item["index"] == 0
    assert item["fields"]["sender"] == "Ada"
    assert item["fields"]["subject"] == "Launch"
    assert item["missing_fields"] == ["missing"]
    assert item["fields"]["nested"]["_partial"] is True
    raw = _invoke(tool, artifact_id="rows", path="/output/rows", limit=1)
    assert raw.output["items"][0]["preview"]["chunkid"] == "c1"


def test_projection_paginates_and_respects_page_budget(tmp_path):
    fields = [f"field-{index}" for index in range(12)]
    rows = [{field: f"row-{row}-{field}-" + "x" * 3000 for field in fields} for row in range(8)]
    tool = ObservationReadTool(_store(tmp_path, {"output": {"rows": rows}}))
    first = _invoke(tool, artifact_id="rows", path="/output/rows", limit=20, fields=fields)
    assert len(first.output["items"]) < len(rows)
    assert first.output["has_more"] is True
    assert first.output["next_offset"] == len(first.output["items"])
    assert first.output["items"][0]["fields"].keys() == set(fields)
    assert first.output["items"][0]["fields"][fields[-1]]["truncated"] is True
    assert len(json.dumps(first.output, ensure_ascii=False)) < 6000
    second = _invoke(tool, artifact_id="rows", path="/output/rows",
                     offset=first.output["next_offset"], limit=1, fields=fields)
    assert second.output["items"][0]["index"] == first.output["next_offset"]


def test_projection_rejects_invalid_fields_and_preserves_reader_scope(tmp_path):
    tool = ObservationReadTool(_store(tmp_path, {"output": {"rows": [{"subject": "ok"}]}}))
    invalid = [None, [], [""], [1], ["x"] * 13, ["x" * 65], ["x", "x"]]
    for fields in invalid:
        assert _invoke(tool, artifact_id="rows", path="/output/rows", fields=fields).status == "rejected"
    assert _invoke(tool, artifact_id="rows", path="/output/rows", run_id="run-2").status == "rejected"
    assert _invoke(tool, artifact_id="foreign", path="/output/rows").status == "rejected"
    mismatch = ToolView(snapshot_id="snapshot", child_run_id="run-2", side_effect_level="read")
    assert _invoke(tool, artifact_id="rows", path="/output/rows", fields=["subject"],
                   tool_view=mismatch).status == "rejected"


def test_truncated_field_read_path_escapes_keys_and_string_pages_continue(tmp_path):
    text = "x" * 520 + " decisive condition " + "y" * 1800
    tool = ObservationReadTool(_store(tmp_path, {"output": {"a/b": [{"q~r": text}]}}))
    projected = _invoke(tool, artifact_id="rows", path="/output/a~1b", fields=["q~r"])
    field = projected.output["items"][0]["fields"]["q~r"]
    assert field["read_path"] == "/output/a~1b/0/q~0r"
    assert projected.output["has_more"] is False and field["truncated"] is True
    page = _invoke(tool, artifact_id="rows", path=field["read_path"],
                   offset=field["next_offset"], max_chars=1200)
    assert page.output["text"] == text[500:1700]
    assert "decisive condition" in page.output["text"] and page.output["has_more"] is True
    tail = _invoke(tool, artifact_id="rows", path=field["read_path"],
                   offset=page.output["next_offset"], max_chars=1200)
    assert tail.output["text"] == text[1700:] and tail.output["has_more"] is False
    for invalid in (0, 16001, True):
        assert _invoke(tool, artifact_id="rows", path=field["read_path"], max_chars=invalid).status == "rejected"
    assert _invoke(tool, artifact_id="rows", path="/output/a~1b", max_chars=1200).status == "rejected"
