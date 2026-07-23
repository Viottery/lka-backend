from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.runtime_debug import run_debug
from app.api.schemas import RuntimeDebugRequest
from app.core.config import get_settings


def test_runtime_debug_returns_structured_chain_and_persists_trace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Demo\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    monkeypatch.setenv("LKA_DATA_DIR", str(data_dir))
    get_settings.cache_clear()

    app = create_app()
    assert "/runtime/debug" in app.openapi()["paths"]

    response = run_debug(
        RuntimeDebugRequest(
            session_id="session_test",
            workspace=str(workspace),
            user_input="Analyze this workspace for runtime debugging.",
        ),
        SimpleNamespace(app=app),
    )

    payload = response.model_dump(mode="json")

    assert payload["trace_id"].startswith("trace_")
    assert payload["session_context"]["context_type"] == "session"
    assert payload["task_context"]["context_type"] == "task"
    assert payload["task_context"]["workspace_id"].startswith("ws_")
    assert payload["task_context"]["related_files"][0]["path"] == "README.md"
    assert payload["retrieval_result"]["provider"] == "local_debug_retrieval"
    assert payload["tool_invocation"]["tool"]["name"] == "runtime_debug_echo"
    assert payload["tool_result"]["status"] == "completed"
    assert payload["llm_response"]["provider"] == "mock_llm"

    event_types = [event["event_type"] for event in payload["events"]]
    assert event_types == [
        "session_context.updated",
        "task_context.derived",
        "retrieval.completed",
        "tool.completed",
        "llm.completed",
        "trace.recorded",
    ]

    db_path = app.state.runtime.db_path
    conn = sqlite3.connect(db_path)
    try:
        trace_count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0]
    finally:
        conn.close()

    assert trace_count == 1
    assert event_count == 6
