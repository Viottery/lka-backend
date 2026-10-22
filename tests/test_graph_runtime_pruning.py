"""Finished LangGraph runs keep only the runtime snapshot their last checkpoint loads."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.agent import run_agent_turn
from app.api.schemas import AgentTurnRequest
from app.core.config import get_settings


def _runtime_artifacts(app, run_id):
    with sqlite3.connect(app.state.runtime.agent_run_store.db_path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT artifact_id FROM agent_run_artifacts WHERE run_id=? AND kind='agent_graph_runtime'", (run_id,),
        )]


def test_completed_run_keeps_only_the_referenced_runtime_snapshot(tmp_path, monkeypatch):
    config = tmp_path / "local.toml"
    config.write_text('[agent]\norchestrator = "langgraph"\nmax_decision_steps = 10\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    app = create_app()
    runner = app.state.runtime.agent_turn_runner

    response = run_agent_turn(AgentTurnRequest(session_id="session_prune", user_input="Answer directly."),
                              SimpleNamespace(app=app))

    kept = _runtime_artifacts(app, response.run_id)
    ref = runner.get_state(response.run_id).values["runtime_artifact_ref"]
    assert kept == [ref["artifact_id"]]
    # The finished run still recovers its result and loads its latest runtime data.
    assert runner._completed_result_from_checkpoint(response.run_id).answer == response.answer
    assert runner._load_data(ref)["tool_events"] == []
    get_settings.cache_clear()


def test_prune_refuses_runs_that_are_not_finished(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    store = create_app().state.runtime.agent_run_store
    assert store.prune_graph_runtime_artifacts("no_such_run", keep_artifact_id="x") == 0
    get_settings.cache_clear()
