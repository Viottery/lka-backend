"""Scripted, isolated turns exercise generic session policies without providers."""
import pytest

from app.core.agent_graph import AgentTurnPaused
from app.core.sessions import SessionRunPolicy
from app.domains.management import ManagementService
from tests.test_unified_entry_quality import operation, runtime_for


def configure_policy(runtime):
    service = ManagementService(runtime._conn, runtime.session_service)
    service.ensure_schema()
    sid = service.create()["session"]["session_id"]
    runtime.session_service.run_policy_provider = lambda session_id: (
        SessionRunPolicy(allow_default_workspace=False, allowed_tools=[], allow_fork=False,
                         llm_client_name="session-client", llm_model="session-model")
        if session_id == sid else SessionRunPolicy()
    )
    return sid


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_management_chat_has_no_default_workspace_or_tools(tmp_path, monkeypatch, orchestrator):
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("final_answer", reason="A conversational response needs no tools."), "Hello.",
    ])
    try:
        sid = configure_policy(runtime)
        guidance_paths, memory_paths = [], []

        def guidance(path):
            guidance_paths.append(path)
            return [{"content": "DEFAULT_PROJECT_ONLY"}] if path else []

        monkeypatch.setattr(runtime.agent_turn_loop.instruction_files, "for_workspace", guidance)
        runtime.agent_turn_loop.memory_context_provider = lambda session, path, text: (
            memory_paths.append(path) or {"items": []}
        )
        result = runtime.run_agent_turn(session_id=sid, user_input="Hello")
        assert result.answer == "Hello."
        assert not result.tool_events
        run = runtime.agent_run_manager.get_run(result.run_id)
        assert run.metadata["session_policy"]["allowed_tools"] == []
        view = runtime.agent_turn_loop._tool_view_for_run(result.run_id)
        assert view.allowed_packages == () and view.allowed_tools == ()
        assert runtime.agent_turn_loop._package_catalog(view) == []
        assert not view.allows_tool(tool_name="filesystem.read_file", package="filesystem", read_only=True)
        assert guidance_paths and all(path is None for path in guidance_paths)
        assert memory_paths == [None]
        assert all("DEFAULT_PROJECT_ONLY" not in call["user_prompt"] for call in client.calls)
        assert all(call["client_name"] == "session-client" and call["model"] == "session-model"
                   for call in client.calls)
        assert all(call["metadata"]["inference_selection_source"] == "session" for call in client.calls)
        message = runtime.session_service.get_session(session_id=sid).messages[-1]
        assert message.payload["workspace_backend_path"] is None
        assert runtime.session_service.get_session(session_id=sid).session.workspace is None
    finally:
        runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_ordinary_policy_keeps_workspace_and_tool_access(tmp_path, monkeypatch, orchestrator):
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": "fact.txt"}),
        operation("final_answer", reason="Read the file."), "ORDINARY_EVIDENCE",
    ])
    (tmp_path / "fact.txt").write_text("ORDINARY_EVIDENCE", encoding="utf-8")
    try:
        configure_policy(runtime)
        result = runtime.run_agent_turn(session_id="ordinary", user_input="Read fact.txt")
        assert result.answer == "ORDINARY_EVIDENCE"
        assert [event.tool_name for event in result.tool_events] == ["filesystem.read_file"]
        assert runtime.agent_turn_loop._tool_view_for_run(result.run_id) is None
        message = runtime.session_service.get_session(session_id="ordinary").messages[-1]
        assert message.payload["workspace_backend_path"] == str(tmp_path)
        assert client.calls
    finally:
        runtime.stop()


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_request_model_overrides_session_defaults(tmp_path, monkeypatch, orchestrator):
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation("final_answer", reason="Just answer."), "Override.",
    ])
    try:
        sid = configure_policy(runtime)
        result = runtime.run_agent_turn(session_id=sid, user_input="Hello", llm_client_name="request-client",
                                        llm_model="request-model")
        assert result.answer == "Override."
        assert all(call["client_name"] == "request-client" and call["model"] == "request-model"
                   for call in client.calls)
        assert all(call["metadata"]["inference_selection_source"] == "request" for call in client.calls)
    finally:
        runtime.stop()


def test_sqlite_resume_keeps_frozen_session_policy(tmp_path, monkeypatch):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    sid = configure_policy(runtime)
    run = runtime.create_agent_run(session_id=sid, user_input="Hello")
    try:
        runtime.interrupt_agent_run(run.run_id)
        with pytest.raises(AgentTurnPaused):
            runtime.run_agent_turn(session_id=sid, user_input="Hello", existing_run_id=run.run_id)
        assert not client.calls
    finally:
        runtime.stop()
    reopened, recovered_client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("final_answer", reason="Resume chat."), "Recovered.",
    ])
    try:
        reopened.session_service.run_policy_provider = lambda _: SessionRunPolicy(
            llm_client_name="changed-client", llm_model="changed-model"
        )
        result = reopened.resume_agent_run(run.run_id)
        assert result.answer == "Recovered."
        assert all(call["client_name"] == "session-client" and call["model"] == "session-model"
                   for call in recovered_client.calls)
        assert reopened.agent_turn_loop._package_catalog(reopened.agent_turn_loop._tool_view_for_run(run.run_id)) == []
        assert reopened.agent_run_manager.get_run(run.run_id).metadata["session_policy"]["allow_default_workspace"] is False
    finally:
        reopened.stop()


def test_replacement_freezes_current_policy_and_retries_keep_binding(tmp_path, monkeypatch):
    runtime, client = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("final_answer", reason="New conversational task."), "Replacement.",
    ])
    try:
        sid = configure_policy(runtime)
        original = runtime.create_agent_run(session_id=sid, user_input="Original")
        runtime.interrupt_agent_run(original.run_id)
        with pytest.raises(AgentTurnPaused):
            runtime.run_agent_turn(session_id=sid, user_input="Original", existing_run_id=original.run_id)
        new_policy = SessionRunPolicy(allow_default_workspace=False, allowed_tools=[], allow_fork=False,
                                      llm_client_name="new-session-client", llm_model="new-session-model")
        runtime.session_service.run_policy_provider = lambda _: new_policy
        replacement = runtime.replace_agent_run(original.run_id, user_input="New task",
                                                 command_id="replace", request_fingerprint="same-input")
        assert replacement.metadata["session_policy"] == new_policy.model_dump(mode="json")
        # Retrying the accepted command must not rebind it to later settings.
        runtime.session_service.run_policy_provider = lambda _: SessionRunPolicy()
        replay = runtime.replace_agent_run(original.run_id, user_input="New task",
                                            command_id="replace", request_fingerprint="same-input")
        assert replay.run_id == replacement.run_id
        assert replay.metadata["session_policy"] == new_policy.model_dump(mode="json")
        result = runtime.run_agent_turn(session_id=sid, user_input="New task", existing_run_id=replacement.run_id)
        assert result.answer == "Replacement."
        assert runtime.agent_turn_loop._package_catalog(runtime.agent_turn_loop._tool_view_for_run(result.run_id)) == []
        assert runtime.session_service.get_session(session_id=sid).messages[-1].payload["workspace_backend_path"] is None
        assert all(call["client_name"] == "new-session-client" and call["model"] == "new-session-model"
                   for call in client.calls)
    finally:
        runtime.stop()


def test_low_level_replacement_inherits_policy_when_not_supplied(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    try:
        sid = configure_policy(runtime)
        run = runtime.create_agent_run(session_id=sid, user_input="Original")
        runtime.interrupt_agent_run(run.run_id)
        with pytest.raises(AgentTurnPaused):
            runtime.run_agent_turn(session_id=sid, user_input="Original", existing_run_id=run.run_id)
        replacement = runtime.agent_run_manager.replace_paused(
            run.run_id, user_input="New", command_id="inherit", request_fingerprint="same"
        )
        assert replacement.metadata["session_policy"] == run.metadata["session_policy"]
    finally:
        runtime.stop()


def test_frozen_session_policy_rejects_fork_at_prompt_and_execution(tmp_path, monkeypatch):
    from app.core.agent_turn import _fork_subtasks_shape_example, _turn_run_id
    from app.core.multi_agent import ForkPolicy, ScopeGrant, SideEffectLevel

    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    try:
        sid = configure_policy(runtime)
        loop = runtime.agent_turn_loop
        # Enable delegation globally; this particular root must still reject it.
        loop.fork_policy = ForkPolicy(max_depth=1, max_children=2, max_fork_size=1,
                                     allowed_scope=ScopeGrant(allowed_packages=("filesystem",),
                                                              allowed_tools=("filesystem.read_file",),
                                                              side_effect_level=SideEffectLevel.READ))
        run = runtime.create_agent_run(session_id=sid, user_input="Delegate a file read")
        runtime.agent_run_manager.mark_running(run.run_id)
        # Live settings cannot relax the already frozen root policy.
        runtime.session_service.run_policy_provider = lambda _: SessionRunPolicy()
        token = _turn_run_id.set(run.run_id)
        try:
            assert loop._can_fork_for_prompt() is False
        finally:
            _turn_run_id.reset(token)
        resolved = []
        loop.fork_scope_resolver = lambda *args, **kwargs: resolved.append(True)
        outcome = loop._handle_fork_subtasks_decision(
            run_id=run.run_id, user_input=run.user_input,
            operation=_fork_subtasks_shape_example()["operation"],
        )
        assert outcome["status"] == "rejected"
        assert "session policy" in outcome["message"]
        assert loop._execute_fork_plan(run_id=run.run_id, operation_id="forged")["status"] == "rejected"
        assert resolved == []
        assert runtime.agent_run_manager.child_tree(run.run_id) == []
        assert "multi_agent_plan" not in runtime.agent_run_manager.get_run(run.run_id).metadata
        assert any(event.type == "fork_subtasks_rejected"
                   for event in runtime.agent_run_manager.list_events(run.run_id))
    finally:
        runtime.stop()
