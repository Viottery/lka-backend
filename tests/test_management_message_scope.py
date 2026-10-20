"""Management root policies preserve message permissions and scoped grants."""
import pytest

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewRequest
from app.core.tools import ToolContext
from tests.test_management_tasks import manager_context as manager_context_fixture
from tests.test_message_history import _message, _policy


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = LocalAppConfig()
    config.management.enabled = True
    config.agent.unified_entry_enabled = True
    config.memory.enabled = False
    config.message_history.enabled = True
    config.message_history.background_enabled = False
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    instance = LocalKnowledgeAgentRuntime(Settings(
        LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path),
    ))
    yield instance
    instance.stop()


def message_run(runtime, target):
    if target == "ordinary":
        sid = runtime.session_service.create_session(title="Messages").session.session_id
    else:
        parent = manager_context_fixture.__wrapped__(runtime)
        sid = runtime.management_delegates.delegate_work(
            parent, command_id="message-line", objective="Review permitted messages",
            allowed_tools=["messages.recent"],
        )["session_id"]
    run = runtime.create_agent_run(session_id=sid, user_input="Review messages", resumable=True)
    return run, runtime.agent_turn_loop._tool_view_for_run(run.run_id)


def recent(runtime, run, view):
    review_id = "message-review:" + run.run_id
    runtime.agent_run_manager.create_safety_review(SafetyReviewRequest(
        review_id=review_id, run_id=run.run_id, session_id=run.session_id, trace_id=run.trace_id,
        invocation_id="message-scope-probe", tool_name="messages.recent", tool_input={},
        mode=SafetyReviewMode.SKIP, reason="Offline read-only regression", created_at=run.created_at,
    ))
    runtime.agent_run_manager.decide_safety_review(
        review_id=review_id, decision=SafetyReviewDecision.APPROVE, decided_by="system.skip",
        reason="Offline read-only regression",
    )
    return runtime.tool_executor.execute(
        invocation_id="message-scope-probe", tool_name="messages.recent", tool_input={},
        context=ToolContext(session_id=run.session_id, run_id=run.run_id,
                            safety_review_approved=True, safety_review_id=review_id, tool_view=view),
    )


def seed(runtime):
    service = runtime.message_history
    first = _policy(service)
    _policy(service, conversation_id="c2", account_id="other")
    service.import_messages([_message("m1", text="first"),
                             _message("m2", conversation_id="c2", account_id="other", text="second")])
    return first


@pytest.mark.parametrize("target", ["ordinary", "delegate"])
def test_generated_root_policy_reads_only_currently_enabled_message_sources(runtime, target):
    first = seed(runtime)
    run, view = message_run(runtime, target)
    assert view.full_data_authority and not view.allowed_source_ids and not view.allowed_account_ids
    result = recent(runtime, run, view)
    assert result.status == "completed"
    assert {row["text"] for row in result.output["messages"]} == {"first", "second"}
    assert result.output["untrusted_data"] is True
    _policy(runtime.message_history, record_enabled=False, expected_revision=first["revision"])
    result = recent(runtime, run, view)
    assert result.status == "completed"
    assert [row["text"] for row in result.output["messages"]] == ["second"]


@pytest.mark.parametrize("grants", ["source", "account", "both"])
def test_full_authority_root_still_passes_explicit_narrowing_to_message_service(runtime, grants):
    first = seed(runtime)
    run, view = message_run(runtime, "ordinary")
    view = view.model_copy(update={
        "allowed_source_ids": (first["source_id"],) if grants in {"source", "both"} else (),
        "allowed_account_ids": (first["account_scope_id"],) if grants in {"account", "both"} else (),
    })
    result = recent(runtime, run, view)
    assert result.status == "completed"
    assert [row["text"] for row in result.output["messages"]] == ["first"]


@pytest.mark.parametrize("missing", ["source", "account", "both", None])
def test_scoped_dag_message_tool_requires_and_enforces_both_grants(runtime, missing):
    first = seed(runtime)
    run, view = message_run(runtime, "ordinary")
    run = runtime.agent_run_manager.create_child_run(
        parent_run_id=run.run_id, plan_id="scope-plan", step_id="messages", attempt=1,
        user_input="Read scoped messages",
    )
    view = view.model_copy(update={
        "child_run_id": run.run_id, "full_data_authority": False,
        "allowed_source_ids": () if missing in {"source", "both"} else (first["source_id"],),
        "allowed_account_ids": () if missing in {"account", "both"} else (first["account_scope_id"],),
    })
    result = recent(runtime, run, view)
    if missing is not None:
        assert result.status == "rejected" and result.execution_started is False
    else:
        assert result.status == "completed"
        assert [row["text"] for row in result.output["messages"]] == ["first"]


def test_disabled_message_package_is_not_registered(tmp_path, monkeypatch):
    config = LocalAppConfig()
    config.management.enabled = True
    config.message_history.enabled = False
    config.memory.enabled = False
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    instance = LocalKnowledgeAgentRuntime(Settings(
        LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path),
    ))
    try:
        assert instance.tool_registry.get_tool_or_none("messages.recent") is None
    finally:
        instance.stop()
