"""Executable reader guidance preserves source paths, bounds and authority."""
from datetime import UTC, datetime

from app.core.agent_storage import SqliteAgentRunStore
from app.core.tools import ToolContext, ToolInvocation, ToolResult
from app.tool_packages.observation import ObservationReadTool


def setup(tmp_path, output):
    store = SqliteAgentRunStore(tmp_path / "reader.sqlite3")
    payload = {"invocation_id": "source", "tool_name": "example.read", "status": "completed", "output": output}
    store.put_artifact(artifact_id="source", run_id="current", kind="tool_result", payload=payload,
                      summary="synthetic source", created_at=datetime.now(UTC).isoformat())
    return store, ObservationReadTool(store), payload


def invoke(tool, *, run_id="current", **arguments):
    return tool.invoke(invocation=ToolInvocation(invocation_id="reader", tool=tool.spec,
        session_id="same", context_id="test", input={"artifact_id": "source", **arguments}),
        context=ToolContext(session_id="same", run_id=run_id))


def test_object_key_pagination_is_independent_of_complete_content(tmp_path):
    store, tool, payload = setup(tmp_path, {"content": "Complete saved evidence", "status": "old"})
    page = invoke(tool, path="/output", limit=1)
    assert page.status == "completed"
    assert page.output["entries"]["content"] == "Complete saved evidence"
    assert page.output["has_more"] is True  # More keys, no missing body text.
    assert page.output["text_coverage"]["content"] == "complete"
    assert page.output["pagination_scope"].startswith("object_keys_only")
    assert page.output["read_paths"] == {"content": "/output/content"}
    next_page = invoke(tool, **{key: value for key, value in page.output["next_read_request"].items() if key != "artifact_id"})
    assert next_page.output["keys"] == ["status"]
    assert next_page.output["has_more"] is False
    assert store.load_tool_result_artifact("source", "current") == payload


def test_object_read_paths_point_to_raw_escaped_keys_and_partial_text(tmp_path):
    store, tool, payload = setup(tmp_path, {"a/b~c": "z" * 20000})
    page = invoke(tool, path="/output")
    pointer = page.output["read_paths"]["a/b~c"]
    assert pointer == "/output/a~1b~0c"
    assert page.output["text_coverage"]["a/b~c"] == "partial_preview"
    assert page.output["has_more"] is False  # Container covered, text still partial.
    text = invoke(tool, path=pointer, max_chars=8000)
    assert text.output["pagination_scope"] == "string_characters"
    assert text.output["text"] == "z" * 8000
    assert text.output["next_offset"] == 8000
    assert store.load_tool_result_artifact("source", "current") == payload


def test_max_chars_object_rejection_supplies_executable_request(tmp_path):
    _, tool, _ = setup(tmp_path, {"content": "Evidence"})
    original = {"artifact_id": "source", "path": "/output", "max_chars": 80, "limit": 1}
    rejected = invoke(tool, path="/output", max_chars=80, limit=1)
    recovery = tool.result_recovery(tool_input=original, result=rejected)
    assert recovery["protocol"] == "observation.read.v1"
    assert recovery["code"] == "max_chars_requires_string"
    assert recovery["retryable"] is True and recovery["retry_same_input"] is False
    assert recovery["read_request"] == {"artifact_id": "source", "path": "/output", "limit": 1}
    request = {key: value for key, value in recovery["read_request"].items() if key != "artifact_id"}
    assert invoke(tool, **request).output["entries"]["content"] == "Evidence"
    # The trusted hook does not accept an output-shaped completed result.
    completed = ToolResult(invocation_id="fake", tool_name=tool.spec.name, status="completed", output=rejected.output)
    assert tool.result_recovery(tool_input=original, result=completed) is None


def test_invalid_wrapper_path_returns_real_parent_keys_without_guessing(tmp_path):
    _, tool, _ = setup(tmp_path, {"content": "Evidence", "state": "old"})
    rejected = invoke(tool, path="/output/entries/content")
    recovery = tool.result_recovery(tool_input={"path": "/output/entries/content"}, result=rejected)
    assert recovery["code"] == "invalid_path"
    assert recovery["parent_path"] == "/output"
    assert recovery["available_keys"] == ["content", "state"]
    assert recovery["read_request"] == {"artifact_id": "source", "path": "/output"}
    assert recovery["retry_same_input"] is False
    # The rejected call neither reads the sibling body nor guesses /output/content.
    assert "Evidence" not in str(rejected.output)


def test_source_key_named_entries_is_not_treated_as_a_preview_wrapper(tmp_path):
    _, tool, _ = setup(tmp_path, {"entries": {"other": "value"}, "content": "Sibling"})
    rejected = invoke(tool, path="/output/entries/content")
    assert rejected.output["recovery"]["parent_path"] == "/output/entries"
    assert rejected.output["recovery"]["available_keys"] == ["other"]
    assert "Sibling" not in str(rejected.output)


def test_invalid_path_recovery_does_not_reveal_foreign_or_revoked_keys(tmp_path):
    store, tool, _ = setup(tmp_path, {"private_key": "secret"})
    rejected = invoke(tool, run_id="other", path="/output/missing")
    assert rejected.status == "rejected" and rejected.output == {}
    assert tool.result_recovery(tool_input={}, result=rejected) is None
    # An imported cache read must pass its live validator before even listing keys.
    store.put_artifact(artifact_id="progress_import_revoked", run_id="current", kind="tool_result",
        payload={"output": {"private_key": "secret"}}, summary="private", created_at=datetime.now(UTC).isoformat())
    store.progress_import_validator = lambda *_args: False
    result = tool.invoke(invocation=ToolInvocation(invocation_id="read", tool=tool.spec, session_id="same",
        context_id="test", input={"artifact_id": "progress_import_revoked", "path": "/output/missing"}),
        context=ToolContext(session_id="same", run_id="current"))
    assert result.output == {} and result.status == "rejected"
