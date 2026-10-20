"""Offline supervisor history contracts using disposable runtime databases."""
from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewRequest
from app.core.tools import ToolContext
from app.domains.management import ManagementConflictError
from app.tool_packages.management_history import management_history_tools
from tests.test_management_tasks import manager_context as manager_context_fixture
from tests.test_management_tasks import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    return manager_context_fixture.__wrapped__(runtime)


def seed(runtime, title="History", texts=("hello",)):
    sid = runtime.session_service.create_session(title=title, metadata={"secret": "PRIVATE_PAYLOAD"}).session.session_id
    for index, text in enumerate(texts):
        runtime.session_service.append_message(
            session_id=sid, role="user" if index % 2 == 0 else "agent", content=text,
            message_id=f"{sid}:{index}", payload={"secret": "PRIVATE_PAYLOAD"},
        )
    return sid


def execute(runtime, context, action, inputs):
    name = "management." + action
    invocation_id = "history-test"
    review_id = "history-review:" + context.run_id
    run = runtime.agent_run_manager.get_run(context.run_id)
    runtime.agent_run_manager.create_safety_review(SafetyReviewRequest(
        review_id=review_id, run_id=run.run_id, session_id=run.session_id, trace_id=run.trace_id,
        invocation_id=invocation_id, tool_name=name, tool_input=inputs,
        mode=SafetyReviewMode.SKIP, reason="Offline history regression", created_at=run.created_at,
    ))
    runtime.agent_run_manager.decide_safety_review(
        review_id=review_id, decision=SafetyReviewDecision.APPROVE, decided_by="system.skip",
        reason="Offline history regression",
    )
    return runtime.tool_executor.execute(
        invocation_id=invocation_id, tool_name=name, tool_input=inputs,
        context=context.model_copy(update={"safety_review_approved": True, "safety_review_id": review_id}),
    )


def test_search_title_body_literal_and_safe_excerpts(runtime, manager_context):
    title = seed(runtime, title="literal %_\"'", texts=("unrelated",))
    body = seed(runtime, texts=("前" * 900 + "%_\"'" + "尾" * 900,))
    seed(runtime, title="no wildcard match", texts=("ordinary text",))
    output = runtime.management_history.search_history(manager_context, query="%_\"'")
    assert {row["session_id"] for row in output["sessions"]} == {title, body}
    body_match = next(row for row in output["sessions"] if row["session_id"] == body)["match"]
    assert body_match["message_id"] == body + ":0"
    assert "%_\"'" in body_match["snippet"] and len(body_match["snippet"]) <= 500
    assert "PRIVATE_PAYLOAD" not in str(output)
    managers = runtime.management_history.search_history(manager_context, kind="manager")
    assert manager_context.session_id in {row["session_id"] for row in managers["sessions"]}
    assert all(row["kind"] == "manager" for row in managers["sessions"])
    result = execute(runtime, manager_context, "read_history", {"session_id": body})
    assert result.status == "completed", result
    assert "PRIVATE_PAYLOAD" not in str(result.output)


def test_pagination_preserves_long_unicode_tails_and_budget(runtime, manager_context):
    texts = ["甲🙂" * 4901, "second", "丙" * 4000, "丁" * 4000, "last"]
    sid = seed(runtime, texts=texts)
    for order, expected in (("oldest", texts), ("newest", list(reversed(texts)))):
        recovered = {}
        offset = start = 0
        for _ in range(30):
            page = runtime.management_history.read_history(
                manager_context, session_id=sid, offset=offset, start_char=start, order=order,
            )
            assert sum(len(row["content"]) for row in page["messages"]) <= 12000
            for row in page["messages"]:
                assert len(row["content"]) <= 4000
                recovered[row["message_id"]] = recovered.get(row["message_id"], "") + row["content"]
            if page["next_offset"] is None:
                break
            offset, start = page["next_offset"], page["next_char"] or 0
        else:
            pytest.fail("history pagination did not terminate")
        assert list(recovered.values()) == expected
    first = runtime.management_history.read_history(manager_context, session_id=sid, message_id=sid + ":0")
    tail = runtime.management_history.read_history(manager_context, session_id=sid, message_id=sid + ":0",
                                                   start_char=first["next_char"])
    assert tail["messages"][0]["content"] == texts[0][4000:8000]


def test_deleted_sources_revalidated_and_cross_session_message_rejected(runtime, manager_context):
    sid = seed(runtime, title="delete marker", texts=("delete marker",))
    other = seed(runtime)
    assert runtime.management_history.search_history(manager_context, query="delete marker")["sessions"]
    with pytest.raises(KeyError):
        runtime.management_history.read_history(manager_context, session_id=other, message_id=sid + ":0")
    runtime.session_service.delete_session(session_id=sid)
    assert not runtime.management_history.search_history(manager_context, query="delete marker")["sessions"]
    with pytest.raises(KeyError):
        runtime.management_history.read_history(manager_context, session_id=sid, message_id=sid + ":0")


def test_roles_and_paused_supervisor_read(runtime, manager_context):
    sid = seed(runtime)
    # Persist a system row directly only to exercise exclusion of legacy/internal rows.
    with closing(runtime._conn()) as conn, conn:
        conn.execute("INSERT INTO agent_session_messages VALUES(?,?,?,?,'{}','now')",
                     ("hidden-system", sid, "system", "SYSTEM_SECRET"))
    runtime.session_service.append_message(session_id=sid, role="tool", content="Management input source")
    runtime.management_service.update(manager_context.session_id, expected_revision=2, status="paused")
    assert not runtime.management_history.search_history(manager_context, query="SYSTEM_SECRET")["sessions"]
    page = runtime.management_history.read_history(manager_context, session_id=sid)
    assert {row["role"] for row in page["messages"]} == {"user", "tool"}
    assert "SYSTEM_SECRET" not in str(page)


def test_session_and_text_share_one_read_snapshot(runtime, manager_context, monkeypatch):
    sid = seed(runtime, title="Before", texts=("before",))
    original = runtime.management_history._conn
    changed = False

    class ConcurrentUpdate:
        def __init__(self):
            self.conn = original()

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, parameters=()):
            nonlocal changed
            cursor = self.conn.execute(sql, parameters)
            if "FROM agent_sessions WHERE session_id=?" in sql and not changed:
                changed = True
                with closing(original()) as writer, writer:
                    writer.execute("UPDATE agent_sessions SET title='After' WHERE session_id=?", (sid,))
                    writer.execute("UPDATE agent_session_messages SET content='after' WHERE session_id=?", (sid,))
            return cursor

    monkeypatch.setattr(runtime.management_history, "_conn", ConcurrentUpdate)
    page = runtime.management_history.read_history(manager_context, session_id=sid)
    assert changed
    assert page["session"]["title"] == "Before"
    assert page["messages"][0]["content"] == "before"
    next_page = runtime.management_history.read_history(manager_context, session_id=sid)
    assert next_page["session"]["title"] == "After"
    assert next_page["messages"][0]["content"] == "after"


def test_ordinary_metadata_and_child_cannot_authorize_history(runtime, manager_context):
    sid = runtime.session_service.create_session(title="Spoof", metadata={"kind": "manager"}).session.session_id
    run = runtime.create_agent_run(session_id=sid, user_input="Spoof", resumable=True)
    runtime.agent_run_manager._update_run(run.run_id, status=run.status, metadata_patch={"kind": "manager"})
    runtime.agent_run_manager.mark_running(run.run_id)
    ordinary = ToolContext(session_id=sid, run_id=run.run_id)
    with pytest.raises(ManagementConflictError):
        runtime.management_history.search_history(ordinary)
    child = runtime.agent_run_manager.create_child_run(
        parent_run_id=manager_context.run_id, plan_id="history", step_id="child", attempt=1, user_input="Read",
    )
    runtime.agent_run_manager.mark_child_running(child.run_id)
    with pytest.raises(ManagementConflictError):
        runtime.management_history.search_history(ToolContext(session_id=child.session_id, run_id=child.run_id))
    result = execute(runtime, ordinary, "search_history", {})
    assert result.status != "completed"


def test_delegate_and_stale_manager_cannot_read(runtime, manager_context):
    receipt = runtime.management_delegates.delegate_work(manager_context, command_id="history-line", objective="Inspect")
    sid = receipt["session_id"]
    run = runtime.create_agent_run(session_id=sid, user_input="Read", resumable=True)
    runtime.agent_run_manager.mark_running(run.run_id)
    with pytest.raises(ManagementConflictError):
        runtime.management_history.search_history(ToolContext(session_id=sid, run_id=run.run_id))
    runtime.agent_run_manager._update_run(manager_context.run_id, status=AgentRunStatus.COMPLETED)
    with pytest.raises(ManagementConflictError):
        runtime.management_history.search_history(manager_context)


@pytest.mark.parametrize("args", [
    {"limit": True}, {"limit": 51}, {"offset": False}, {"offset": -1},
    {"kind": "system"}, {"query": "x" * 501}, {"query": 4},
])
def test_search_validation(runtime, manager_context, args):
    with pytest.raises(ValueError):
        runtime.management_history.search_history(manager_context, **args)


@pytest.mark.parametrize("args", [{"start_char": True}, {"start_char": -1}, {"order": "random"},
                                  {"message_id": 4}, {"session_id": 4}])
def test_read_validation(runtime, manager_context, args):
    with pytest.raises(ValueError):
        runtime.management_history.read_history(manager_context, **{"session_id": "session", **args})


def test_schemas_and_executor_reject_extra_fields(runtime, manager_context):
    tools = management_history_tools(runtime.management_history)
    assert {tool.spec.name for tool in tools} == {"management.search_history", "management.read_history"}
    for tool in tools:
        assert tool.spec.package == "history" and tool.spec.read_only is True
        assert tool.spec.risk == "low" and not tool.spec.requires_confirmation
        assert tool.spec.input_schema["additionalProperties"] is False
    result = execute(runtime, manager_context, "search_history", {"query": "", "payload": "forged"})
    assert result.status == "rejected"
    assert result.execution_started is False
    assert "payload" in str(result.output.get("validation_errors"))
