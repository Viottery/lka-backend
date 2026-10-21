from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.context_driver import ToolView
from app.core.session_progress_cache import SessionProgressCacheService
from app.core.sessions import AgentSession, SessionRunPolicy, SessionWorkspace
from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolRegistry, ToolSpec
from app.tool_packages.observation import ObservationReadTool
from app.tool_packages.progress import PROGRESS_PACKAGE, ProgressLoadTool, ProgressSearchTool


@pytest.fixture
def harness(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "runs.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    now = datetime.now(UTC).isoformat()
    session = AgentSession(session_id="same", title="Task", status="active", created_at=now, updated_at=now)
    sessions = {session.session_id: session}
    policy = SessionRunPolicy()
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="example", description="test"))
    tool = SimpleNamespace(spec=ToolSpec(name="example.run", package="example", type="local_tool", description="test", read_only=False))
    registry.register_tool(tool)
    service = SessionProgressCacheService(store=store, registry=registry, run_manager=manager,
        session_service=SimpleNamespace(get_session_or_none=lambda *, session_id: sessions.get(session_id),
                                        run_policy=lambda _session_id: policy))

    def view_for(run_id):
        data = store.load_run(run_id)["metadata"].get("view")
        return ToolView.model_validate(data) if data else None

    def workspace_for(current, run_id):
        return store.load_run(run_id)["metadata"].get("workspace", current.workspace.backend_path if current.workspace else None)

    service.bind_turn_loop(SimpleNamespace(_tool_view_for_run=view_for, _workspace_for_run=workspace_for,
        _current_observation_cache_scope=lambda context: {
            "workspace": context.workspace_root,
            "view": context.tool_view.model_dump(mode="json", exclude={"snapshot_id", "child_run_id"}) if context.tool_view else None,
        }))

    def run(task="Fix parser bug", **kwargs):
        record = manager.create_run(session_id="same", user_input=task, **kwargs)
        return record, service._context_for_run(record.run_id)

    def record(context, identifier="evidence-1", text="SECRET_FULL_RESULT", status="completed", state=None,
               output=None, tool_input=None):
        now = datetime.now(UTC).isoformat()
        result = {"tool_name": tool.spec.name, "invocation_id": identifier, "status": status, "output": output if output is not None else {"text": text}}
        store.put_artifact(artifact_id=f"tool_result_{identifier}", run_id=context.run_id,
                           kind="tool_result", payload=result, summary="private", created_at=now)
        events = [{"tool_name": tool.spec.name, "input": tool_input if tool_input is not None else {"command": "inspect parser"},
                   "result": result, "completed_at": now}]
        service.record(context=context, tool_events=events, working_state=state)
        return result, events

    return SimpleNamespace(store=store, manager=manager, session=session, sessions=sessions,
                           policy=policy, registry=registry, tool=tool, service=service, run=run, record=record)


def read(h, context, artifact_id, **arguments):
    tool = ObservationReadTool(h.store)
    return tool.invoke(invocation=ToolInvocation(invocation_id="read", tool=tool.spec,
        session_id=context.session_id, context_id="test", input={"artifact_id": artifact_id, **arguments}), context=context)


def test_failed_root_retains_real_operations_and_bounded_task_index(harness):
    h = harness
    old, context = h.run()
    h.record(context, state={"remaining_requirements": ["Validate corrected parser"], "focus_gap": "Regression input",
        "findings": [{"summary": "Parser located", "evidence_ids": ["evidence-1"], "kind": "finding"}]})
    # An exception in a subsequent LLM call need not publish an agent message.
    failed = h.store.load_run(old.run_id) | {"status": "failed"}
    h.store.save_run(failed, updated_at=datetime.now(UTC).isoformat())
    _, current = h.run("Continue parser investigation")
    index = h.service.search(context=current, query="parser", limit=1)
    assert len(index["entries"]) == 1
    entry = index["entries"][0]
    assert entry["source_run_status"] == "failed"
    assert entry["operation_status"] == "completed"
    assert entry["remaining_requirements"] == ["Validate corrected parser"]
    assert entry["completion_claim_verified"] is False
    assert index["historical_only"] and not index["current_evidence"]
    assert "SECRET_FULL_RESULT" not in str(index)
    imported = h.service.load(context=current, source_run_id=old.run_id, evidence_id="evidence-1")
    assert imported["historical_only"] and imported["as_of"]
    assert imported["read_request"] == {"artifact_id": imported["artifact_id"], "path": "/output"}
    assert imported["path_value_type"] == "object"
    assert imported["string_only_parameters"] == ["max_chars"]
    reader = ObservationReadTool(h.store)
    ready = reader.invoke(invocation=ToolInvocation(invocation_id="ready", tool=reader.spec,
        session_id=current.session_id, context_id="test", input=imported["read_request"]), context=current)
    assert ready.status == "completed"
    assert read(h, current, imported["artifact_id"], path="/output/text", max_chars=6).output["text"] == "SECRET"
    assert read(h, current, "tool_result_evidence-1").status == "rejected"


@pytest.mark.parametrize("change", ["reset", "workspace", "project", "tool_policy", "tool_revocation", "source_deleted", "source_changed", "session_deleted"])
def test_imported_pages_revalidate_revocation_on_every_read(harness, change):
    h = harness
    old, context = h.run()
    raw, events = h.record(context)
    _, current = h.run()
    artifact_id = h.service.load(context=current, source_run_id=old.run_id, evidence_id="evidence-1")["artifact_id"]
    assert read(h, current, artifact_id).status == "completed"
    if change == "reset":
        h.session.metadata["history_reset_at"] = datetime.now(UTC).isoformat()
        # A still-running old graph must not republish old evidence into the new epoch.
        h.service.record(context=context, tool_events=events)
    elif change == "workspace":
        h.session.workspace = SessionWorkspace(path="/other", backend_path="/other", platform="linux")
    elif change == "project":
        h.session.project_id = "other-project"
    elif change == "tool_policy":
        h.policy.allowed_tools = ["observation.read", "progress.load"]
    elif change == "tool_revocation":
        h.tool.allow_cached_observation = lambda **kwargs: False
    elif change == "source_deleted":
        from app.storage.db import connect
        with connect(h.store.db_path) as conn:
            conn.execute("DELETE FROM agent_runs WHERE run_id=?", (old.run_id,))
    elif change == "source_changed":
        h.store.put_artifact(artifact_id="tool_result_evidence-1", run_id=old.run_id, kind="tool_result",
            payload=raw | {"output": {"text": "replacement"}}, summary="changed", created_at=datetime.now(UTC).isoformat())
    else:
        h.sessions.clear()
    assert read(h, current, artifact_id).status == "rejected"
    assert not h.service.can_read_import(artifact_id, current.run_id)


def test_same_manager_or_fork_does_not_grant_other_session_history(harness):
    h = harness
    old, context = h.run()
    h.record(context)
    h.sessions["foreign"] = h.session.model_copy(update={"session_id": "foreign"})
    foreign = h.manager.create_run(session_id="foreign", user_input="Continue parser")
    foreign_context = h.service._context_for_run(foreign.run_id)
    assert h.service.search(context=foreign_context)["entries"] == []
    with pytest.raises(ValueError, match="scope"):
        h.service.load(context=foreign_context, source_run_id=old.run_id, evidence_id="evidence-1")
    child, _ = h.run(parent_run_id=old.run_id)
    with pytest.raises(ValueError, match="root"):
        h.service.search(context=ToolContext(session_id="same", run_id=child.run_id))


def test_relevance_limit_and_unverified_findings(harness):
    h = harness
    _, context = h.run("Parser defect")
    h.record(context, "parser-proof", state={"findings": [{"summary": "Unsupported self report", "evidence_ids": ["made-up"]}]})
    _, context = h.run("Unrelated garden")
    h.record(context, "garden-proof", text="garden", state={})
    _, current = h.run("Parser")
    entries = h.service.search(context=current, query="parser", limit=1)["entries"]
    assert entries[0]["evidence_id"] == "parser-proof"
    assert entries[0]["findings"] == []
    assert h.service.search(context=current, query="absent term")["entries"] == []


def test_process_restart_requires_validator_and_rehydrates_persisted_progress(harness):
    h = harness
    old, context = h.run()
    h.record(context)
    _, current = h.run()
    artifact_id = h.service.load(context=current, source_run_id=old.run_id, evidence_id="evidence-1")["artifact_id"]
    fresh_store = SqliteAgentRunStore(h.store.db_path)
    assert fresh_store.load_tool_result_artifact(artifact_id, current.run_id) is None
    fresh_service = SessionProgressCacheService(store=fresh_store, session_service=h.service.session_service,
                                               registry=h.registry, run_manager=h.manager)
    fresh_service.bind_turn_loop(h.service.turn_loop)
    assert fresh_store.load_tool_result_artifact(artifact_id, current.run_id)["output"]["text"] == "SECRET_FULL_RESULT"
    assert fresh_service.search(context=current, query="parser")["entries"]


def test_progress_tools_declare_read_only_and_reject_unauthorized_scope(harness):
    h = harness
    assert PROGRESS_PACKAGE.requires_expansion
    for cls in (ProgressSearchTool, ProgressLoadTool):
        tool = cls(h.service)
        assert tool.spec.read_only is True
        arguments = {} if cls is ProgressSearchTool else {"source_run_id": "unknown", "evidence_id": "missing"}
        result = tool.invoke(invocation=ToolInvocation(invocation_id="test", tool=tool.spec,
            session_id="same", context_id="test", input=arguments), context=ToolContext(session_id="same"))
        assert result.status == "rejected"


@pytest.mark.parametrize("prefix", ["progress_import_", "progress_import_management_"])
@pytest.mark.parametrize("kind", ["read", "search", "group"])
def test_import_readers_preserve_historical_freshness_even_when_page_complete(tmp_path, prefix, kind):
    from app.tool_packages.observation import ObservationSearchTool
    from app.tool_packages.observation_group import ObservationGroupTool

    store = SqliteAgentRunStore(tmp_path / "imports.sqlite3")
    artifact_id = prefix + "example"
    payload = {"output": {"text": "Historical parser evidence", "rows": [{"state": "done"}]}}
    store.put_artifact(artifact_id=artifact_id, run_id="current", kind="tool_result", payload=payload,
                      summary="historical", created_at=datetime.now(UTC).isoformat())
    store.progress_import_validator = lambda artifact_id, run_id: run_id == "current"
    if kind == "read":
        tool, arguments = ObservationReadTool(store), {"path": "/output/text"}
    elif kind == "search":
        tool, arguments = ObservationSearchTool(store), {"query": "parser", "path": "/output/text"}
    else:
        tool, arguments = ObservationGroupTool(store), {"field": "state", "path": "/output/rows"}
    invocation = ToolInvocation(invocation_id="import-reader", tool=tool.spec, session_id="same",
                               context_id="test", input={"artifact_id": artifact_id, **arguments})
    result = tool.invoke(invocation=invocation, context=ToolContext(session_id="same", run_id="current"))
    assert result.status == "completed", result.error
    assert result.output["historical_only"] is True
    assert result.output["current_evidence"] is False
    assert result.output["freshness"] == "historical_source_not_revalidated"
    assert result.output["source_artifact_id"] == artifact_id
    assert store.load_tool_result_artifact(artifact_id, "current") == payload
    store.progress_import_validator = lambda *_args: False
    assert tool.invoke(invocation=invocation, context=ToolContext(session_id="same", run_id="current")).status == "rejected"


def test_same_session_mail_history_remains_loadable_without_refetch(harness):
    from app.domains.mail_knowledge import MailKnowledgeMirror
    from app.tool_packages.mail import MAIL_PACKAGE, LoadMailMessagesTool

    h = harness
    # Use the registered mail implementation. Historical access must not call
    # invoke/load_messages again just to obtain the already saved source body.
    def unexpected_refetch(*args, **kwargs):
        pytest.fail("Historical loading unexpectedly refetched mail")
    mail_service = SimpleNamespace(load_messages=unexpected_refetch,
        get_message_account_ids=lambda message_ids, account_ids=None: {"mail-1": "account-1"})
    mirror = SimpleNamespace(active_account_ids=lambda *, session_id: ("account-1",),
        active_source_ids=lambda *, session_id: (MailKnowledgeMirror.source_id_for_account("account-1"),))
    mail = LoadMailMessagesTool(mail_service, mirror)
    h.registry.register_package(MAIL_PACKAGE.model_copy(deep=True))
    h.registry.register_tool(mail)
    h.tool.spec = mail.spec
    old, context = h.run("Read selected mail requirements")
    h.record(context, "mail-proof", tool_input={"message_ids": ["mail-1"]},
             output={"messages": [{"message_id": "mail-1", "body_text": "Previously loaded mail requirements"}]})
    _, current = h.run("Continue with those mail requirements")
    entries = h.service.search(context=current, query="mail requirements")["entries"]
    assert entries[0]["tool_name"] == "mail.load_messages"
    imported = h.service.load(context=current, source_run_id=old.run_id, evidence_id="mail-proof")
    page = read(h, current, imported["artifact_id"], path="/output/messages/0/body_text")
    assert page.status == "completed"
    assert page.output["text"] == "Previously loaded mail requirements"
    assert page.output["has_more"] is False
    assert page.output["historical_only"] is True
    assert page.output["current_evidence"] is False


def test_index_ranks_before_reading_bodies_and_validates_only_limit_plus_one(harness, monkeypatch):
    h = harness
    _, context = h.run("Parser defect")
    for index in range(20):
        h.record(context, f"parser-{index}", state={"findings": [{"kind": "finding",
            "summary": "Combined finding requires more than this index entry",
            "evidence_ids": ["parser-0", "parser-1"]}]})
    _, current = h.run("Continue parser")
    original = h.store.load_tool_result_artifact
    reads = []
    def counted(artifact_id, run_id):
        reads.append((artifact_id, run_id))
        return original(artifact_id, run_id)
    monkeypatch.setattr(h.store, "load_tool_result_artifact", counted)
    page = h.service.search(context=current, query="parser", limit=8)
    assert len(page["entries"]) == 8 and page["has_more"]
    assert len(reads) == 9
    assert all(entry["findings"] == [] for entry in page["entries"])


@pytest.mark.parametrize("query", ["继续", "unrelated new question"])
def test_generic_continuation_keeps_authorized_discovery_route_without_old_task_text(harness, query):
    from app.core.agent_turn import AgentTurnLoop

    h = harness
    _, context = h.run("Parser investigation PRIVATE_TASK_TEXT")
    h.record(context)
    _, current = h.run(query)
    signal = h.service.search(context=current, query=query)
    assert signal["entries"] == []
    assert signal["index_available"] is True and signal["cache_runs_available"] is True
    assert signal["search_tool"] == "progress.search"
    assert signal["search_request"] == {"query": ""}
    assert "PRIVATE_TASK_TEXT" not in str(signal)
    assert "SECRET_FULL_RESULT" not in str(signal)
    cached = AgentTurnLoop._cached_tool_observations_from_session(
        SimpleNamespace(session_progress_cache=h.service), session_id="same", context=current, query=query)
    assert cached[0]["action"] == "session_progress_index" and cached[0]["index_available"]
    assert h.service.search(context=current, query="")["entries"]
    h.policy.allowed_tools = []
    revoked = h.service.search(context=current, query=query)
    assert not revoked["index_available"] and not revoked["cache_runs_available"]
    assert "search_tool" not in revoked


def test_no_cache_does_not_advertise_history_discovery(harness):
    h = harness
    _, current = h.run("继续")
    index = h.service.search(context=current, query="继续")
    assert index["entries"] == [] and not index["index_available"]
    assert "search_tool" not in index
