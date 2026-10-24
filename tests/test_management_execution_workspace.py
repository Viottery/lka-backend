"""Offline controller CAS and independent execution workspace contracts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from app.domains.management import ManagementConflictError
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_management_tasks import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    return context_fixture.__wrapped__(runtime)


def configure(runtime, manager_context, parent):
    runtime.management_service.workspace_resolver = runtime._resolve_session_workspace
    current = runtime.management_service.get(manager_context.session_id)["controller"]
    return runtime.management_service.update(manager_context.session_id, expected_revision=current["revision"],
        default_execution_workspace={"path": str(parent), "platform": "linux"})


def test_controller_workspace_is_separate_and_revision_fenced(runtime, manager_context, tmp_path):
    configured = configure(runtime, manager_context, tmp_path)
    assert configured["controller"]["default_execution_workspace"]["backend_path"] == str(tmp_path)
    assert configured["session"]["workspace"] is None
    assert configured["session"]["metadata"] == {}
    with pytest.raises(ManagementConflictError):
        runtime.management_service.update(manager_context.session_id, expected_revision=1,
                                         default_execution_workspace=None)
    revision = configured["controller"]["revision"]
    cleared = runtime.management_service.update(manager_context.session_id, expected_revision=revision,
                                                default_execution_workspace=None)
    assert cleared["controller"]["default_execution_workspace"] is None


def test_default_creation_concurrent_retries_never_replace_configuration(runtime, tmp_path):
    runtime.management_service.workspace_resolver = runtime._resolve_session_workspace
    selection = {"path": str(tmp_path), "platform": "linux"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runtime.management_service.get_default(
            default_execution_workspace=selection), range(8)))
    assert len({r["session"]["session_id"] for r in results}) == 1
    assert {r["controller"]["revision"] for r in results} == {1}
    other = tmp_path / "other"
    other.mkdir()
    original = results[0]
    assert runtime.management_service.get_default(default_execution_workspace={
        "path": str(tmp_path / "missing"), "platform": "linux"}) == original
    assert runtime.management_service.get_default(default_execution_workspace={
        "path": str(other), "platform": "linux"}) == original
    sid = original["session"]["session_id"]
    cleared = runtime.management_service.update(sid, expected_revision=1, default_execution_workspace=None)
    assert runtime.management_service.get_default(default_execution_workspace=selection) == cleared


def test_new_tasks_get_independent_directories_and_retry_preserves_frozen_workspace(runtime, manager_context, tmp_path):
    configure(runtime, manager_context, tmp_path)
    task = runtime.managed_tasks
    first = task.start_task(manager_context, command_id="first", purpose="Read work")
    second = task.start_task(manager_context, command_id="second", purpose="Read work")
    workspace = runtime.get_session(session_id=first["session_id"]).session.workspace
    other = runtime.get_session(session_id=second["session_id"]).session.workspace
    assert workspace.backend_path != other.backend_path
    assert Path(workspace.backend_path).parent == tmp_path
    assert Path(workspace.backend_path).is_dir()
    record = runtime.agent_run_store.load_run(first["run_id"])
    assert record["metadata"]["execution_workspace"] == workspace.model_dump()
    assert record["metadata"]["session_policy"]["allow_default_workspace"] is False
    configure(runtime, manager_context, Path(other.backend_path))
    assert task.start_task(manager_context, command_id="first", purpose="Read work") == first
    assert runtime.agent_run_store.load_run(first["run_id"]) == record


def test_explicit_task_workspace_overrides_controller_parent(runtime, manager_context, tmp_path):
    configure(runtime, manager_context, tmp_path)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    result = runtime.managed_tasks.start_task(manager_context, command_id="explicit", purpose="Inspect",
                                               workspace_path=str(explicit))
    assert runtime.get_session(session_id=result["session_id"]).session.workspace.backend_path == str(explicit)


def test_no_parent_allows_data_work_but_disables_checkout_fallback(runtime, manager_context):
    result = runtime.managed_tasks.start_task(manager_context, command_id="data", purpose="Read mail data")
    record = runtime.agent_run_store.load_run(result["run_id"])
    assert record["metadata"]["execution_workspace"] is None
    assert record["metadata"]["session_policy"]["allow_default_workspace"] is False
    assert runtime.get_session(session_id=result["session_id"]).session.workspace is None
    # The receipt says the tools are withheld instead of leaving it to discovery.
    assert result["workspace_notice"].startswith("no_execution_workspace")


def test_server_task_parent_serves_assistants_without_a_default(runtime, manager_context, tmp_path):
    parent = tmp_path / "server-tasks"
    parent.mkdir()
    runtime.managed_tasks.default_task_parent = {"path": str(parent), "platform": runtime.platform.name}
    result = runtime.managed_tasks.start_task(manager_context, command_id="server", purpose="Check the server")
    workspace = runtime.get_session(session_id=result["session_id"]).session.workspace
    assert workspace is not None and workspace.backend_path.startswith(str(parent))
    assert "workspace_notice" not in result


def test_windows_default_keeps_frontend_path_and_backend_mapping(runtime, manager_context, tmp_path):
    def resolve(*, path, platform):
        assert platform == "windows"
        local = tmp_path.joinpath(*path.removeprefix("D:\\AgentWork").strip("\\").split("\\"))
        if not local.is_dir():
            raise ValueError("missing directory")
        return {"path": path, "platform": platform, "backend_path": str(local)}
    runtime.management_service.workspace_resolver = resolve
    revision = runtime.management_service.get(manager_context.session_id)["controller"]["revision"]
    runtime.management_service.update(manager_context.session_id, expected_revision=revision,
        default_execution_workspace={"path": "D:\\AgentWork", "platform": "windows"})
    result = runtime.managed_tasks.start_task(manager_context, command_id="windows", purpose="Inspect")
    workspace = runtime.get_session(session_id=result["session_id"]).session.workspace
    assert workspace.platform == "windows" and workspace.path.startswith("D:\\AgentWork\\task_")
    assert Path(workspace.backend_path).parent == tmp_path


def test_queue_uses_same_default_workspace_preparation(runtime, manager_context, tmp_path):
    configure(runtime, manager_context, tmp_path)
    result = runtime.management_queue.queue_work(manager_context, command_id="queued", purpose="Inspect", read_only_work=True)
    workspace = runtime.get_session(session_id=result["session_id"]).session.workspace
    assert Path(workspace.backend_path).parent == tmp_path
    run = runtime.agent_run_store.load_run(result["run_id"])
    assert run["metadata"]["execution_workspace"] == workspace.model_dump()
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM management_commands WHERE command_id='queued'").fetchone()[0] == 1


def test_manager_workspace_api_validates_and_cas_updates(runtime, tmp_path):
    import asyncio

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.routes.managers import router

    runtime.management_service.workspace_resolver = runtime._resolve_session_workspace
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(router)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=("127.0.0.1", 9000)),
                               base_url="http://test") as client:
            selection = {"path": str(tmp_path), "platform": "linux"}
            default = await client.post("/managers/default", json={"default_execution_workspace": selection})
            assert default.status_code == 200, default.text
            assert default.json()["controller"]["default_execution_workspace"]["backend_path"] == str(tmp_path)
            created = await client.post("/managers", json={"default_execution_workspace": selection})
            assert created.status_code == 201, created.text
            sid = created.json()["session"]["session_id"]
            assert created.json()["session"]["workspace"] is None
            invalid = await client.patch(f"/managers/{sid}", json={"expected_revision": 1,
                "default_execution_workspace": {"path": str(tmp_path / "missing"), "platform": "linux"}})
            assert invalid.status_code == 422
            changed = await client.patch(f"/managers/{sid}", json={"expected_revision": 1,
                                                                        "default_execution_workspace": None})
            assert changed.status_code == 200
            stale = await client.patch(f"/managers/{sid}", json={"expected_revision": 1,
                "default_execution_workspace": selection})
            assert stale.status_code == 409
    asyncio.run(exercise())


def test_start_work_uses_same_default_workspace_preparation(runtime, manager_context, tmp_path):
    from app.domains.management_assignments import ManagementAssignmentService
    from app.domains.management_work import ManagementWorkService
    from tests.test_management_work import create

    configure(runtime, manager_context, tmp_path)
    runtime.local_app_config.management.internal_items_enabled = True
    work = ManagementWorkService(runtime._conn, matters=runtime.matter_service, tasks=runtime.managed_tasks,
        management=runtime.management_service, goals=runtime.management_goals,
        run_store=runtime.agent_run_store, run_manager=runtime.agent_run_manager)
    work.ensure_schema()
    assignments = ManagementAssignmentService(runtime._conn, tasks=runtime.managed_tasks,
        matters=runtime.matter_service, work=work, goals=runtime.management_goals)
    assignments.ensure_schema()
    mid = work.organize_work(manager_context, "matter", [create()])["refs"]["a"]
    result = assignments.start_work(manager_context, command_id="work", matter_id=mid, expected_revision=1,
                                    purpose="Inspect")
    workspace = runtime.get_session(session_id=result["session_id"]).session.workspace
    assert Path(workspace.backend_path).parent == tmp_path
    run = runtime.agent_run_store.load_run(result["run_id"])
    assert run["metadata"]["execution_workspace"] == workspace.model_dump()


def test_delegate_default_is_stable_and_tasks_can_select_another_workspace(runtime, manager_context, tmp_path):
    from tests.test_management_delegates import child_context

    configure(runtime, manager_context, tmp_path)
    receipt = runtime.management_delegates.delegate_work(manager_context, command_id="delegate", objective="Inspect")
    with closing(runtime._conn()) as conn:
        frozen = runtime.management_delegates.get_for_session(conn, receipt["session_id"])["workspace"]
    configure(runtime, manager_context, Path(frozen["backend_path"]))
    context = child_context(runtime, receipt["session_id"])
    result = runtime.managed_tasks.start_task(context, command_id="child-task", purpose="Inspect")
    workspace = runtime.get_session(session_id=result["session_id"]).session.workspace
    assert workspace.model_dump() == frozen
    other = runtime.managed_tasks.start_task(
        context, command_id="other-directory", purpose="Inspect", workspace_path=str(tmp_path),
    )
    assert runtime.get_session(session_id=other["session_id"]).session.workspace.backend_path == str(tmp_path)
    assert runtime.get_session(session_id=result["session_id"]).session.workspace.model_dump() == frozen


def test_no_workspace_executor_rejects_file_and_shell_access_without_side_effects(runtime, manager_context, tmp_path):
    from app.core.tools import ToolContext

    result = runtime.managed_tasks.start_task(manager_context, command_id="no-file-authority", purpose="Read mail data")
    record = runtime.agent_run_store.load_run(result["run_id"])
    assert record["metadata"]["session_policy"]["allowed_paths"] == []
    view = runtime.agent_turn_loop._tool_view_for_run(result["run_id"])
    assert not view.full_workspace_authority and view.allowed_paths == ()
    context = ToolContext(session_id=result["session_id"], run_id=result["run_id"], tool_view=view,
                          safety_review_approved=True)
    target = tmp_path / "must-not-exist.txt"
    for tool_name, tool_input in (
        ("filesystem.create_file", {"path": str(target), "content": "forbidden"}),
        ("bash.run", {"command": "touch must-not-exist.txt", "cwd": str(tmp_path)}),
        ("filesystem.list_directory", {"path": str(tmp_path)}),
    ):
        rejected = runtime.tool_executor.execute(invocation_id="workspace-denial:" + tool_name,
            tool_name=tool_name, tool_input=tool_input, context=context)
        assert rejected.status == "rejected", rejected.model_dump()
        assert rejected.execution_started is False
        assert "workspace" in rejected.error.lower(), rejected.error
    assert not target.exists()
