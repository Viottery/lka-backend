from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.main import create_app
from app.api.routes.memories import (
    CreateMemoryRequest,
    RelocateProjectRequest,
    background_health,
    create_memory,
    export_memories,
    forget_memory,
    get_memory_file,
    import_memory_file,
    list_memories,
    memory_sources,
    preview_memory_file,
    relocate_project,
    require_local_memory_control,
)
from app.core.config import get_settings
from app.core.llm.errors import LLMRateLimitError
from app.core.sessions import SessionRecentMessage, SessionWorkspace


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    app = create_app()
    # These tests explicitly drive the worker instead of advancing wall time.
    app.state.runtime.memory_background.debounce_seconds = 0
    return app


def _completed_run(runtime, session_id: str, run_id: str):
    from app.storage.db import connect

    conn = connect(runtime.db_path)
    try:
        conn.execute(
            "INSERT INTO agent_runs(run_id,session_id,status,record_payload,created_at,updated_at) "
            "VALUES(?,?, 'completed','{}','2026-01-01','2026-01-01')",
            (run_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_project_relocation_control_checks_both_paths_and_keeps_memory(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(root))
    app = _app(tmp_path, monkeypatch)
    request = SimpleNamespace(app=app)
    old, new = root / "original", root / "moved"
    record = create_memory(CreateMemoryRequest(content="发布必须先灰度", scope="project", workspace_path=str(old)), request)
    project = app.state.runtime.memory_service.resolve_project(old)
    result = relocate_project(project, RelocateProjectRequest(old_workspace_path=str(old), new_workspace_path=str(new)), request)
    assert result["project_id"] == project
    assert list_memories(request, scope="project", workspace_path=str(new), limit=100)["memories"][0]["memory_id"] == record["memory_id"]
    with pytest.raises(HTTPException) as error:
        relocate_project(project, RelocateProjectRequest(old_workspace_path=str(new), new_workspace_path=str(tmp_path / "outside")), request)
    assert error.value.status_code == 403


def test_memory_export_pages_and_provenance_are_scoped(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    request = SimpleNamespace(app=app)
    for content in ("回答先给结论", "回答提供来源", "回答标记风险"):
        create_memory(CreateMemoryRequest(content=content), request)
    first = export_memories(request, limit=2)
    second = export_memories(request, limit=2, offset=first["next_offset"])
    ids = [row["memory_id"] for page in (first, second) for row in page["memories"]]
    assert len(ids) == len(set(ids)) == 3
    assert second["next_offset"] is None
    sources = memory_sources(ids[0], request)["sources"]
    assert len(sources) == 1 and sources[0]["source_type"] == "user_api"
    assert "metadata" not in sources[0] and "content" not in sources[0]


def test_memory_file_api_rejects_oversized_view_without_materializing_response(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    request = SimpleNamespace(app=app)
    create_memory(CreateMemoryRequest(content="回答先给结论"), request)
    path = app.state.runtime.memory_files.path_for(scope="global")
    path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(HTTPException) as error:
        get_memory_file(request)
    assert error.value.status_code == 422
    assert "paginated" in error.value.detail
    assert path.stat().st_size == 8 * 1024 * 1024 + 1


def test_unknown_project_export_is_empty_with_terminal_page_and_validates_offset(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(root))
    app = _app(tmp_path, monkeypatch)
    request = SimpleNamespace(app=app)
    create_memory(CreateMemoryRequest(content="项目甲决定", scope="project", workspace_path=str(root / "a")), request)
    page = export_memories(request, scope="project", workspace_path=str(root / "unknown"), limit=10)
    assert page["memories"] == [] and page["next_offset"] is None
    with pytest.raises(HTTPException) as error:
        list_memories(request, scope="project", workspace_path=str(root / "unknown"), limit=10, offset=-1)
    assert error.value.status_code == 422


def test_completed_answer_background_extracts_then_recall_and_forget(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    session = runtime.session_service.ensure_session(session_id="memory_e2e")
    runtime.session_service.append_message(
        session_id=session.session_id, role="user", content="记住：我希望回答简洁",
        payload={"trace_id": "trace_memory"},
    )
    runtime.session_service.append_message(
        session_id=session.session_id, role="agent", content="好的",
        payload={"trace_id": "trace_memory", "run_id": "run_memory"},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    _completed_run(runtime, session.session_id, "run_memory")
    assert runtime.memory_background.worker.run_one()
    memories = runtime.memory_service.list(scope="global")
    assert len(memories) == 1 and memories[0].content == "我希望回答简洁"
    assert runtime.memory_files.path_for(scope="global").exists()
    view = runtime.agent_turn_loop.memory_context_provider("memory_e2e", None, "回答")
    assert view["items"][0]["memory_id"] == memories[0].memory_id
    request = SimpleNamespace(app=app)
    assert len(list_memories(request, limit=100)["memories"]) == 1
    forgotten = forget_memory(
        memories[0].memory_id, request, expected_version=memories[0].version,
    )
    assert forgotten["status"] == "retracted"
    assert runtime.agent_turn_loop.memory_context_provider("memory_e2e", None, "回答")["items"] == []


def test_direct_long_term_preference_becomes_active_without_remember_command(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    runtime.session_service.ensure_session(session_id="ordinary_preference")
    runtime.session_service.append_message(
        session_id="ordinary_preference", role="user",
        content="我希望以后回答时先给结论", payload={"trace_id": "pref_trace"},
    )
    runtime.session_service.append_message(
        session_id="ordinary_preference", role="agent", content="好的",
        payload={"trace_id": "pref_trace", "run_id": "pref_run"},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    _completed_run(runtime, "ordinary_preference", "pref_run")
    assert runtime.memory_background.worker.run_one()
    memories = runtime.memory_service.list(scope="global")
    assert [item.content for item in memories] == ["以后回答时先给结论"]
    view = runtime.agent_turn_loop.memory_context_provider("ordinary_preference", None, "回答")
    assert view["items"][0]["memory_id"] == memories[0].memory_id


def test_remote_extraction_rate_limit_retries_without_publishing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    runtime.session_service.ensure_session(session_id="limited_extraction")
    runtime.session_service.append_message(
        session_id="limited_extraction", role="user", content="我倾向简洁的回答",
        payload={"trace_id": "limited_trace"},
    )
    runtime.session_service.append_message(
        session_id="limited_extraction", role="agent", content="好的",
        payload={"trace_id": "limited_trace", "run_id": "limited_run"},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    _completed_run(runtime, "limited_extraction", "limited_run")

    class LimitedClient:
        def complete_text(self, **kwargs):
            raise LLMRateLimitError(status_code=429, message="limited")

    runtime.memory_background.llm_client = LimitedClient()
    runtime.memory_background.allow_remote_extraction = True
    assert runtime.memory_background.worker.run_one()
    jobs = runtime.background_job_store.list(
        kind="memory_extract", scope_id="limited_extraction"
    )
    assert jobs[0]["status"] == "retry_wait"
    assert jobs[0]["error_class"] == "provider_http_429"
    assert runtime.memory_service.list(scope="global") == []


def test_background_compaction_awaits_async_llm_service(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    class AsyncClient:
        async def complete_text(self, **kwargs):
            return SimpleNamespace(content='{"summary":"保留已确认的目标"}')

    coordinator = app.state.runtime.memory_background
    coordinator.llm_client = AsyncClient()
    summary = coordinator._summarize(
        "", [SessionRecentMessage(
            role="user", content="请保留已确认的目标", created_at="2026-01-01T00:00:00+00:00",
        )], 65536,
    )
    assert summary == "保留已确认的目标"


def test_session_delete_suppresses_derived_memory_and_restore_does_not_republish(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    runtime.session_service.ensure_session(session_id="delete_memory")
    runtime.session_service.append_message(
        session_id="delete_memory", role="user", content="记住：用中文回答",
        payload={"trace_id": "t"},
    )
    runtime.session_service.append_message(
        session_id="delete_memory", role="agent", content="好的",
        payload={"trace_id": "t", "run_id": "run_delete"},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    _completed_run(runtime, "delete_memory", "run_delete")
    runtime.memory_background.worker.run_one()
    assert runtime.memory_service.list(scope="global")
    assert runtime.delete_session(session_id="delete_memory")
    assert runtime.memory_service.list(scope="global") == []
    assert runtime.restore_session(session_id="delete_memory")
    assert runtime.memory_service.list(scope="global") == []
    # Registering the same source after restore must not reactivate revoked provenance.
    from app.domains.memory import MemorySourceInput
    from app.storage.db import connect
    conn = connect(runtime.db_path)
    try:
        source = conn.execute("SELECT source_ref,checksum FROM memory_sources").fetchone()
    finally:
        conn.close()
    runtime.memory_service.register_source(MemorySourceInput(
        source_type="user_message", source_ref=source["source_ref"], checksum=source["checksum"],
    ))
    assert runtime.memory_service.list(scope="global") == []


def test_manual_memory_is_available_offline(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    request = SimpleNamespace(app=app)
    item = create_memory(CreateMemoryRequest(content="Use concise answers"), request)
    assert item["status"] == "active"
    assert list_memories(request, limit=100)["memories"][0]["memory_id"] == item["memory_id"]
    view = get_memory_file(request)
    assert "Use concise answers" in view["content"]
    path = app.state.runtime.memory_files.path_for(scope="global")
    path.write_text(view["content"].replace("Use concise answers", "Use short answers"), encoding="utf-8")
    assert len(preview_memory_file(request)["edits"]) == 1
    imported = import_memory_file(request)
    assert imported["memory_file_status"] == "synced"
    assert list_memories(request, limit=100)["memories"][0]["content"] == "Use short answers"


def test_background_health_exposes_only_aggregates(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    response = background_health(SimpleNamespace(app=app))
    assert "queue_depth_by_status" in response
    assert "payload" not in str(response)
    assert "scope_id" not in str(response)


def test_recovery_scan_repairs_answer_without_outbox(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    runtime.session_service.ensure_session(session_id="outbox_gap")
    runtime.session_service.append_message(
        session_id="outbox_gap", role="user", content="记住：优先中文",
        payload={"trace_id": "trace_gap"},
    )
    runtime.session_service.append_message(
        session_id="outbox_gap", role="agent", content="明白",
        payload={"trace_id": "trace_gap", "run_id": "run_gap"},
    )
    _completed_run(runtime, "outbox_gap", "run_gap")
    assert runtime.background_job_store.list(kind="memory_extract", scope_id="outbox_gap") == []
    assert runtime.memory_background.recover_missing_jobs() == 1
    assert runtime.memory_background.recover_missing_jobs() == 0
    assert runtime.memory_background.worker.run_one()
    assert runtime.memory_service.list(scope="global")[0].content == "优先中文"


def test_delete_before_worker_then_restore_cannot_publish_old_memory(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    runtime.session_service.ensure_session(session_id="late_memory")
    runtime.session_service.append_message(
        session_id="late_memory", role="user", content="记住：旧偏好",
        payload={"trace_id": "late_trace"},
    )
    runtime.session_service.append_message(
        session_id="late_memory", role="agent", content="好的",
        payload={"trace_id": "late_trace", "run_id": "run_late"},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    _completed_run(runtime, "late_memory", "run_late")
    assert runtime.delete_session(session_id="late_memory")
    assert runtime.restore_session(session_id="late_memory")
    assert runtime.memory_background.worker.run_one()
    assert runtime.memory_service.list(scope="global") == []


def test_memory_api_requires_loopback_or_configured_token(monkeypatch):
    monkeypatch.delenv("LKA_MEMORY_API_TOKEN", raising=False)
    remote = SimpleNamespace(
        client=SimpleNamespace(host="192.0.2.5"), headers={},
    )
    with pytest.raises(HTTPException) as denied:
        require_local_memory_control(remote)
    assert denied.value.status_code == 403
    require_local_memory_control(SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"), headers={},
    ))
    monkeypatch.setenv("LKA_MEMORY_API_TOKEN", "test-secret")
    with pytest.raises(HTTPException) as unauthenticated:
        require_local_memory_control(remote)
    assert unauthenticated.value.status_code == 401
    require_local_memory_control(SimpleNamespace(
        client=SimpleNamespace(host="192.0.2.5"),
        headers={"authorization": "Bearer test-secret"},
    ))


def test_worker_uses_project_snapshot_not_current_session_workspace(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    project_a_path = tmp_path / "project_a"
    project_b_path = tmp_path / "project_b"
    project_a_path.mkdir()
    project_b_path.mkdir()
    project_a = runtime.memory_service.resolve_project(project_a_path)
    project_b = runtime.memory_service.resolve_project(project_b_path)
    runtime.session_service.ensure_session(session_id="switch_project")
    runtime.session_service.set_workspace(
        session_id="switch_project", workspace=SessionWorkspace(
            path=str(project_a_path), backend_path=str(project_a_path), platform="linux",
        ),
    )
    runtime.session_service.append_message(
        session_id="switch_project", role="user", content="记住：这个项目使用 pytest",
        payload={"trace_id": "switch_trace"},
    )
    runtime.session_service.append_message(
        session_id="switch_project", role="agent", content="好的",
        payload={"trace_id": "switch_trace", "run_id": "run_switch",
                 "memory_project_id": project_a,
                 "workspace_backend_path": str(project_a_path)},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    _completed_run(runtime, "switch_project", "run_switch")
    runtime.session_service.set_workspace(
        session_id="switch_project", workspace=SessionWorkspace(
            path=str(project_b_path), backend_path=str(project_b_path), platform="linux",
        ),
    )

    class ProjectExtractor:
        def complete_text(self, **kwargs):
            return SimpleNamespace(content=(
                '{"candidates":[{"claim":"这个项目使用 pytest",'
                '"kind":"project_decision","evidence":"这个项目使用 pytest",'
                '"explicit":true,"confidence":1}]}'
            ))

    runtime.memory_background.llm_client = ProjectExtractor()
    runtime.memory_background.allow_remote_extraction = True
    assert runtime.memory_background.worker.run_one()
    memories_a = runtime.memory_service.list(scope="project", project_id=project_a)
    memories_b = runtime.memory_service.list(scope="project", project_id=project_b)
    assert len(memories_a) == 1 and memories_b == []


def test_memory_disabled_preserves_sessions_without_tool_or_injection(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text("[memory]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    assert runtime.agent_turn_loop.memory_context_provider is None
    assert runtime.agent_turn_loop.memory_answer_callback is None
    assert "memory" not in {package.name for package in runtime.tool_registry.list_packages()}
    session = runtime.session_service.ensure_session(session_id="no_memory")
    assert session.session_id == "no_memory"


def test_model_explicit_flag_alone_cannot_publish_memory(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    runtime.session_service.ensure_session(session_id="model_hint")
    runtime.session_service.append_message(
        session_id="model_hint", role="user", content="我倾向简洁的回答",
        payload={"trace_id": "model_trace"},
    )
    runtime.session_service.append_message(
        session_id="model_hint", role="agent", content="知道了",
        payload={"trace_id": "model_trace", "run_id": "run_model"},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    _completed_run(runtime, "model_hint", "run_model")

    class FakeExtractor:
        def complete_text(self, **kwargs):
            return SimpleNamespace(content=(
                '{"candidates":[{"claim":"偏好简洁回答",'
                '"kind":"preference","evidence":"我倾向简洁的回答",'
                '"explicit":true,"confidence":0.9}]}'
            ))

    runtime.memory_background.llm_client = FakeExtractor()
    runtime.memory_background.allow_remote_extraction = True
    assert runtime.memory_background.worker.run_one()
    assert runtime.memory_service.list(scope="global") == []
    candidates = runtime.memory_service.list(scope="global", statuses=("candidate",))
    assert len(candidates) == 1


def test_real_agent_turn_enqueues_completed_answer_for_background_memory(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    result = runtime.run_agent_turn(
        session_id="real_memory_turn", user_input="记住：回答优先给结论",
    )
    assert result.session_id == "real_memory_turn"
    jobs = runtime.background_job_store.list(
        kind="memory_extract", scope_id="real_memory_turn",
    )
    assert len(jobs) == 1 and jobs[0]["status"] == "queued"
    assert runtime.memory_background.worker.run_one()
    assert runtime.memory_service.list(scope="global")[0].content == "回答优先给结论"


def test_langgraph_turn_enqueues_completed_answer_for_background_memory(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text('[agent]\norchestrator = "langgraph"\n[memory]\nextraction_debounce_seconds=0\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    result = runtime.run_agent_turn(
        session_id="graph_memory_turn", user_input="记住：回答时给出来源",
    )
    assert result.session_id == "graph_memory_turn"
    assert runtime.memory_background.worker.run_one()
    assert runtime.memory_service.list(scope="global")[0].content == "回答时给出来源"
