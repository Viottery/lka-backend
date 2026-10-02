"""Small HTTP-level check of Planner -> Codex expert wiring."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.api.main import create_app
from app.core.agent_executors import AgentDefinition
from app.core.codex_app_server import ServerNotification
from app.core.config import get_settings
from app.core.multi_agent import PlanStep, ScopeGrant, SideEffectLevel
from app.core.multi_agent_scheduler import MultiAgentScheduler


class _FakeCodexClient:
    def __init__(self) -> None:
        self.events: asyncio.Queue[ServerNotification] = asyncio.Queue()
        self.events.put_nowait(ServerNotification(
            method="item/completed",
            params={"threadId": "thread-api", "turnId": "turn-api", "item": {
                "id": "message-api", "type": "agentMessage", "text": "README contains one line.",
            }},
        ))
        self.events.put_nowait(ServerNotification(
            method="turn/completed",
            params={"threadId": "thread-api", "turn": {"id": "turn-api", "status": "completed"}},
        ))
        self.started = False

    async def initialize(self, **_kwargs):
        return {}

    async def start_thread(self, **_kwargs):
        return {"id": "thread-api"}

    async def start_turn(self, **_kwargs):
        self.started = True
        return {"id": "turn-api"}

    async def next_event(self):
        return await self.events.get()

    async def interrupt_turn(self, **_kwargs):
        return None


class _EditingFakeCodexClient(_FakeCodexClient):
    async def start_thread(self, **kwargs):
        self.cwd = Path(kwargs["cwd"])
        return await super().start_thread(**kwargs)

    async def start_turn(self, **kwargs):
        (self.cwd / "RESULT.txt").write_text("staged only\n", encoding="utf-8")
        return await super().start_turn(**kwargs)


def test_workspace_sandbox_scope_projection_only_removes_grants(tmp_path):
    original_scope = ScopeGrant(
        workspace_paths=(str(tmp_path),),
        source_ids=("source-1",),
        account_ids=("account-1",),
        allowed_packages=("filesystem",),
        allowed_tools=("filesystem.edit_file",),
        side_effect_level=SideEffectLevel.EXTERNAL,
    )
    step = PlanStep(
        correlation_id="scope-projection",
        step_id="codex-step",
        objective="Inspect the workspace.",
        output_contract="One sentence.",
        effective_scope=original_scope,
    )
    expert = AgentDefinition(
        agent_id="codex_expert", version="0.1", executor_kind="external_cli",
        scope_mode="workspace_sandbox",
    )
    general = AgentDefinition(
        agent_id="general_agent", version="1", executor_kind="react",
    )

    projected = MultiAgentScheduler._execution_step(step, expert)

    assert projected.effective_scope.workspace_paths == (str(tmp_path),)
    assert projected.effective_scope.side_effect_level == SideEffectLevel.WRITE
    assert projected.effective_scope.source_ids == ()
    assert projected.effective_scope.account_ids == ()
    assert projected.effective_scope.allowed_packages == ()
    assert projected.effective_scope.allowed_tools == ()
    assert step.effective_scope == original_scope
    assert MultiAgentScheduler._execution_step(step, general) is step
    read_step = step.model_copy(update={
        "effective_scope": original_scope.model_copy(update={
            "side_effect_level": SideEffectLevel.READ,
        }),
    })
    assert (
        MultiAgentScheduler._execution_step(read_step, expert)
        .effective_scope.side_effect_level == SideEffectLevel.READ
    )


@pytest.mark.parametrize("configured_key", ["codex_state_home", "codex_home"])
def test_runtime_rejects_codex_state_inside_source_workspace(
    tmp_path, monkeypatch, configured_key
):
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "local.toml"
    config.write_text(
        '[agent]\ncodex_expert_enabled = true\n'
        f'codex_binary_path = "{sys.executable}"\n'
        f'codex_workspace_base = "{tmp_path / "staged"}"\n'
        f'{configured_key} = "{source / "codex-state"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(source))
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="state home must be separate"):
        create_app()
    get_settings.cache_clear()


@pytest.mark.parametrize("staged_write", [False, True])
@pytest.mark.parametrize("omit_audit", [False, True])
def test_codex_expert_runs_through_agent_http_api(tmp_path, monkeypatch, staged_write, omit_audit):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("one line\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    config = tmp_path / "local.toml"
    config.write_text(
        '[agent]\norchestrator = "langgraph"\ncheckpoint_backend = "sqlite"\n'
        'max_decision_steps = 3\n'
        'multi_agent_planning_enabled = true\ncodex_expert_enabled = true\n'
        f'codex_binary_path = "{sys.executable}"\n'
        f'codex_workspace_base = "{tmp_path / "staged"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(source))
    get_settings.cache_clear()


    app = create_app()
    runtime = app.state.runtime
    fake = _EditingFakeCodexClient() if staged_write else _FakeCodexClient()
    assert runtime.codex_expert_executor is not None
    runtime.codex_expert_executor.client_factory = lambda _run_id: fake
    if omit_audit:
        real_record = runtime.codex_expert_executor._record

        def without_audit(child, event_type, message, payload=None):
            if event_type != "child_tool_audit":
                real_record(child, event_type, message, payload)

        monkeypatch.setattr(runtime.codex_expert_executor, "_record", without_audit)
    loop = runtime.agent_turn_loop
    loop._route = lambda **_kwargs: {"selected_package": "filesystem", "reason": "Inspect workspace."}
    child_failure_codes: list[str] = []

    def decide(**kwargs):
        fork_results = [
            item for item in kwargs["observations"]
            if item.get("action") == "fork_subtasks"
        ]
        if fork_results:
            for result in fork_results[-1].get("task_results", []):
                failure = result.get("failure") or {}
                code = failure.get("code")
                if code and code not in child_failure_codes:
                    child_failure_codes.append(code)
            return {"action": "final_answer", "reason": "Child finished."}
        return {
            "action": "fork_subtasks",
            "operation": {
                "correlation_id": "codex_api_trace",
                "operation_id": "codex_api_fork",
                "parent_step_id": "root_coordinator",
                "subtasks": [{
                    "step_id": "inspect_readme",
                    "agent_id": "codex_expert",
                    "objective": "Read README.md and report its content. Do not change files.",
                    "output_contract": "One short sentence.",
                }],
            },
            "reason": "Delegate a workspace inspection to Codex.",
        }

    loop._decide_next_action = decide
    loop._answer_with_llm = lambda **_kwargs: "Codex inspection finished."

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await asyncio.wait_for(client.post("/agent/turn", json={
                "user_input": "Ask Codex to inspect README.md without changing files.",
            }), timeout=15)
            assert response.status_code == 200, response.text
            run_id = response.json()["run_id"]
            snapshot = await client.get(f"/agent/runs/{run_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            return response.json(), snapshot.json()

    response, snapshot = asyncio.run(exercise())
    assert fake.started
    if staged_write or omit_audit:
        assert response["answer"].startswith("子任务仍有未解决的失败或阻塞")
        parent = runtime.agent_run_manager.get_run(response["run_id"])
        assert parent.metadata["multi_agent_replan_required"] is True
        assert parent.status.value == "failed"
        if omit_audit:
            assert "actual_side_effects_unknown" in parent.metadata["multi_agent_verification"]["missing_requirements"]
            assert parent.metadata["multi_agent_plan"]["status"] == "failed"
        assert not (source / "RESULT.txt").exists()
    else:
        assert child_failure_codes == []
        assert response["answer"] == "Codex inspection finished."
    assert snapshot["children"][0]["status"] == "completed"
    child = runtime.agent_run_manager.get_run(snapshot["children"][0]["run_id"])
    assert child is not None
    effective_scope = child.metadata["context_snapshot"]["effective_scope"]
    assert effective_scope["side_effect_level"] == "write"
    assert effective_scope["allowed_packages"] == []
    assert effective_scope["allowed_tools"] == []
    assert effective_scope["source_ids"] == []
    assert effective_scope["account_ids"] == []
    get_settings.cache_clear()


@pytest.mark.skipif(
    not os.environ.get("LKA_CODEX_LIVE_BINARY"),
    reason="Set LKA_CODEX_LIVE_BINARY to run a real authenticated Codex CLI smoke.",
)
@pytest.mark.parametrize("write_mode", [False, True])
def test_live_codex_expert_runs_through_agent_http_api(tmp_path, monkeypatch, write_mode):
    """Opt-in, bounded real CLI check; all source files remain untouched."""
    binary = Path(os.environ["LKA_CODEX_LIVE_BINARY"]).resolve()
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("LKA live smoke marker\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    config = tmp_path / "local.toml"
    config.write_text(
        '[agent]\norchestrator = "langgraph"\ncheckpoint_backend = "sqlite"\n'
        'max_decision_steps = 3\nmulti_agent_planning_enabled = true\n'
        'codex_expert_enabled = true\ncodex_permission_mode = "profile"\n'
        f'codex_binary_path = {json.dumps(str(binary))}\n'
        f'codex_workspace_base = {json.dumps(str(tmp_path / "staged"))}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(source))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    loop = runtime.agent_turn_loop
    loop._route = lambda **_kwargs: {
        "selected_package": "filesystem", "reason": "Inspect the temporary workspace."
    }

    def decide(**kwargs):
        if any(item.get("action") == "fork_subtasks" for item in kwargs["observations"]):
            return {"action": "final_answer", "reason": "Codex child returned."}
        return {
            "action": "fork_subtasks",
            "operation": {
                "correlation_id": "codex_live_smoke",
                "operation_id": "codex_live_fork",
                "parent_step_id": "root_coordinator",
                "subtasks": [{
                    "step_id": "inspect_readme",
                    "agent_id": "codex_expert",
                    "objective": (
                        "Create RESULT.txt in this workspace with exactly "
                        "LKA staged write marker followed by a newline."
                        if write_mode else
                        "Use a shell command to read README.md in this workspace, "
                        "then report its first line. Do not change files."
                    ),
                    "output_contract": (
                        "Confirm the created file." if write_mode
                        else "One short sentence based on README.md."
                    ),
                }],
            },
            "reason": "Delegate workspace inspection to Codex.",
        }

    loop._decide_next_action = decide
    loop._answer_with_llm = lambda **_kwargs: "Codex inspection finished."

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await asyncio.wait_for(client.post("/agent/turn", json={
                "user_input": "Ask Codex to inspect the temporary README.md only.",
            }), timeout=120)
            assert response.status_code == 200, response.text
            run_id = response.json()["run_id"]
            snapshot = await client.get(f"/agent/runs/{run_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            return response.json(), snapshot.json()

    try:
        response, snapshot = asyncio.run(exercise())
        assert snapshot["children"][0]["status"] == "completed"
        child = runtime.agent_run_manager.get_run(snapshot["children"][0]["run_id"])
        assert child is not None and child.result_snapshot is not None
        staged = child.result_snapshot
        if write_mode:
            assert runtime.agent_run_manager.get_run(response["run_id"]).status.value == "failed"
            assert staged["staged_change_count"] >= 1
            assert (Path(staged["staged_workspace"]) / "RESULT.txt").read_text(
                encoding="utf-8"
            ) == "LKA staged write marker\n"
        else:
            assert runtime.agent_run_manager.get_run(response["run_id"]).status.value == "completed"
            assert staged["staged_change_count"] == 0
        assert (source / "README.md").read_text(encoding="utf-8") == "LKA live smoke marker\n"
        assert sorted(path.name for path in source.iterdir()) == [".git", "README.md"]
    finally:
        get_settings.cache_clear()
