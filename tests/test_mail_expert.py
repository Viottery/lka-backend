from __future__ import annotations

import asyncio
import json

import pytest

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.config import Settings
from app.core.context_driver import AgentView, AuditView, ContextViews, PlannerView, ToolView
from app.core.llm import LLMResponse
from app.core.multi_agent import (
    ContextSnapshot,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResultStatus,
)
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import ToolExecutor, ToolRegistry, ToolResult, ToolSpec
from app.domains.mail import MailAccountInput, MailMessageInput, MailService
from app.domains.mail_knowledge import MailKnowledgeMirror
from app.experts.mail import MailExpertExecutor, MailIntent
from app.storage.db import connect
from app.tool_packages.mail import MAIL_PACKAGE
from app.tool_packages.mail_expert_tools import MailBatchLoadTool, MailSnapshotTool

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeLLM:
    def __init__(self, mode: str, *, bad_ids: bool = False):
        self.mode = mode
        self.bad_ids = bad_ids
        self.stages = []

    async def complete(self, request):
        stage = request.metadata["stage"]
        self.stages.append(stage)
        if stage == "route":
            content = json.dumps({
                "mode": self.mode, "received_from": "2026-06-01T00:00:00+08:00",
                "received_before": "2026-06-03T00:00:00+08:00", "query": "invoice",
            })
        elif stage == "analyze_batch":
            ids = [m["message_id"] for m in json.loads(request.messages[-1].content)["messages"]]
            if self.bad_ids:
                ids = ids[:-1]
            content = json.dumps({"items": [
                {"message_id": item, "summary": "摘要", "priority": "low", "reason": "无急事", "action_required": False}
                for item in ids
            ]})
        elif stage == "align_matter":
            rows = json.loads(request.messages[-1].content)["messages"]
            content = json.dumps({"items": [
                {"message_id": row["message_id"], "matter_id": row["candidates"][0]["matter_id"]}
                for row in rows
            ]})
        else:
            assert request.require_json is False
            content = "邮件检索答案"
        return LLMResponse(provider="fake", status="completed", content=content,
                           prompt_summary=request.prompt_summary, model="fake", usage={"total_tokens": 50})


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "mail_expert.sqlite3"
    store = SqliteAgentRunStore(path)

    def db():
        return connect(path)

    service = MailService(db)
    manager = InMemoryAgentRunManager(durable_store=store)
    registry = ToolRegistry()
    registry.register_package(MAIL_PACKAGE)

    class Mirror:
        def active_account_ids(self, *, session_id=None):
            return service.list_authorized_account_ids()

    registry.register_tool(MailSnapshotTool(service, Mirror()))
    registry.register_tool(MailBatchLoadTool(service))

    class SearchStub:
        spec = ToolSpec(name="mail.search", package="mail", type="local_tool", description="Fixture search",
                        read_only=True, scope_uses_sources=True, scope_uses_accounts=True,
                        scope_filtering_required=True,
                        input_schema={"type": "object", "required": ["query"], "properties": {
                            "query": {"type": "string"}, "limit": {"type": "integer"},
                            "max_snippet_chars": {"type": "integer"},
                        }})

        def invoke(self, *, invocation, context):
            return ToolResult(invocation_id=invocation.invocation_id, tool_name="mail.search",
                              status="completed", output={"messages": [{
                                  "message_id": "example-id", "subject": "Invoice", "sender": "sender@example.com",
                                  "received_at": "2026-06-01T12:00:00Z", "snippet": "Invoice evidence",
                                  "source_ref": "mail_message:example-id",
                              }], "applied_limit": 12, "possible_more": False})

    registry.register_tool(SearchStub())

    class MatterStub:
        spec = ToolSpec(name="matter.list", package="matter", type="local_tool", description="Fixture matter listing",
                        read_only=True, scope_uses_sources=True, scope_uses_accounts=True,
                        scope_filtering_required=True,
                        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}})

        def invoke(self, *, invocation, context):
            return ToolResult(invocation_id=invocation.invocation_id, tool_name="matter.list", status="completed",
                              output={"matters": [{"matter_id": "matter-1", "title": "Subject",
                                                   "summary": "Subject 0", "source_links": []}]})

    from app.core.tools import ToolPackageSpec
    registry.register_package(ToolPackageSpec(name="matter", description="Fixture matters"))
    registry.register_tool(MatterStub())
    return service, store, manager, ToolExecutor(registry)


def _case(setup, count: int, mode: str, *, budget=None, bad_ids=False, grant=True, matter_grant=False):
    service, store, manager, tools = setup
    imported = service.import_messages(
        account=MailAccountInput(email_address="expert@example.com"),
        messages=[MailMessageInput(
            external_id=f"msg-{i}", folder="Inbox", subject=f"Subject {i}", sender="sender@example.com",
            received_at="2026-06-01T12:00:00Z", body_text=f"Body {i}",
        ) for i in range(count)],
    )
    parent = manager.create_run(session_id="parent-session", user_input="mail")
    child = manager.create_child_run(parent_run_id=parent.run_id, plan_id="plan", step_id="step", attempt=1,
                                     user_input="mail", agent_id="mail_expert", agent_version="1", executor_kind="workflow")
    account_ids = (imported.account_id,) if grant else ()
    source_ids = (MailKnowledgeMirror.source_id_for_account(imported.account_id),) if grant else ()
    names = ("mail.snapshot", "mail.batch_load", "mail.search") + (("matter.list",) if matter_grant else ())
    packages = ("mail", "matter") if matter_grant else ("mail",)
    scope = ScopeGrant(account_ids=account_ids, source_ids=source_ids, allowed_packages=packages,
                       allowed_tools=names, side_effect_level=SideEffectLevel.READ)
    snap = ContextSnapshot(correlation_id=parent.trace_id, snapshot_id="snap", parent_run_id=parent.run_id, child_run_id=child.run_id,
                           session_id=child.session_id, plan_id="plan", step_id="step", agent_id="mail_expert", agent_version="1",
                           objective="列出并分析所有邮件", output_contract="有证据的摘要", effective_scope=scope,
                           budget=budget or RuntimeBudget(max_tool_calls=10, max_llm_calls=30, max_tokens=100000),
                           policy_version="1", workspace_version="1", permission_version="1")
    views = ContextViews(
        agent=AgentView(objective=snap.objective, output_contract=snap.output_contract, mode="working"),
        tool=ToolView(snapshot_id="snap", child_run_id=child.run_id, allowed_packages=packages,
                      allowed_tools=names, allowed_account_ids=account_ids, allowed_source_ids=source_ids,
                      side_effect_level=SideEffectLevel.READ),
        planner=PlannerView(plan_id="plan", step_id="step"),
        audit=AuditView(snapshot_id="snap", child_run_id=child.run_id, session_id=child.session_id,
                        policy_version="1", workspace_version="1", permission_version="1"),
    )
    llm = FakeLLM(mode, bad_ids=bad_ids)
    return MailExpertExecutor(run_manager=manager, tool_executor=tools, artifact_store=store, llm_service=llm), child, snap, views, llm


async def _execute(expert, child, snap, views):
    return await expert.execute(child_run_id=child.run_id, snapshot=snap, views=views)


@pytest.fixture(autouse=True)
def synchronous_tool_boundary(monkeypatch):
    # Workflow tests cover orchestration and contracts; thread dispatch is a
    # separate runtime concern and is deliberately not exercised here.
    async def run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)


@pytest.mark.parametrize("count", [25, 262, 505])
async def test_complete_metadata_snapshot(setup, count):
    expert, child, snap, views, llm = _case(setup, count, "list")
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.COMPLETED
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert payload["coverage"]["enumerated"] == count
    assert payload["coverage"]["metadata_complete"] is True
    assert len({m["message_id"] for m in payload["messages"]}) == count
    assert payload["usage"]["tool_calls"] == (2 if count > 500 else 1)
    assert llm.stages == ["route"]
    assert "messages" not in setup[2].get_run(child.run_id).result_snapshot
    with connect(setup[1].db_path) as conn:
        cached = conn.execute(
            "SELECT artifact_id FROM agent_run_artifacts WHERE run_id = ? AND kind = 'tool_result'",
            (child.run_id,),
        ).fetchall()
    assert len(cached) == payload["usage"]["tool_calls"]
    assert setup[1].load_tool_result_artifact(cached[0]["artifact_id"], child.run_id)["status"] == "completed"
    assert setup[1].load_tool_result_artifact(cached[0]["artifact_id"], "unrelated-run") is None


async def test_review_loads_and_analyzes_every_message(setup):
    expert, child, snap, views, llm = _case(setup, 25, "review")
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.COMPLETED
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert payload["coverage"]["body_loaded"] == 25
    assert payload["coverage"]["semantically_analyzed"] == 25
    assert len(payload["analysis"]) == 25
    assert llm.stages.count("analyze_batch") == 2


async def test_group_sender_uses_metadata_only(setup):
    expert, child, snap, views, llm = _case(setup, 25, "group_sender")
    result = await _execute(expert, child, snap, views)
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert result.status == TaskResultStatus.COMPLETED
    assert payload["groups"][0]["count"] == 25
    assert payload["coverage"]["sender_groups"] == 1
    assert llm.stages == ["route"]


async def test_matter_alignment_requires_explicit_grant(setup):
    expert, child, snap, views, llm = _case(setup, 2, "align_matter")
    result = await _execute(expert, child, snap, views)
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert result.status == TaskResultStatus.PARTIAL
    assert payload["matches"] == []
    assert llm.stages == ["route"]


async def test_matter_alignment_checks_visible_candidates_without_writes(setup):
    expert, child, snap, views, llm = _case(setup, 2, "align_matter", matter_grant=True)
    result = await _execute(expert, child, snap, views)
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert result.status == TaskResultStatus.COMPLETED
    assert payload["coverage"]["aligned_or_checked"] == 2
    assert {match["matter_ids"][0] for match in payload["matches"]} == {"matter-1"}
    assert llm.stages == ["route", "align_matter"]
    assert payload["usage"]["tool_calls"] == 2


async def test_bad_model_shard_is_partial(setup):
    expert, child, snap, views, _ = _case(setup, 25, "review", bad_ids=True)
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.PARTIAL
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert payload["coverage"]["semantically_analyzed"] == 0
    assert payload["missing_requirements"]


async def test_scope_denial_is_failure_not_empty_mailbox(setup):
    expert, child, snap, views, _ = _case(setup, 2, "list", grant=False)
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.FAILED
    assert setup[2].get_run(child.run_id).status == AgentRunStatus.FAILED


async def test_llm_budget_marks_review_partial(setup):
    expert, child, snap, views, _ = _case(setup, 25, "review", budget=RuntimeBudget(
        max_tool_calls=10, max_llm_calls=2, max_tokens=100000,
    ))
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.PARTIAL
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert payload["coverage"]["semantically_analyzed"] == 20
    assert payload["missing_requirements"]


async def test_tool_budget_marks_large_list_partial(setup):
    expert, child, snap, views, _ = _case(setup, 505, "list", budget=RuntimeBudget(
        max_tool_calls=1, max_llm_calls=2, max_tokens=100000,
    ))
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.PARTIAL
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert payload["coverage"]["local_total"] == 505
    assert payload["coverage"]["enumerated"] == 500
    assert payload["coverage"]["metadata_complete"] is False
    assert payload["next_range"]["start_rank"] == 501


async def test_cancelled_before_route(setup):
    expert, child, snap, views, _ = _case(setup, 2, "list")
    setup[2].request_cancel(child.run_id, reason="test")
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.CANCELLED


async def test_search_uses_text_answer_without_snapshot(setup):
    expert, child, snap, views, llm = _case(setup, 1, "search")
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.COMPLETED
    assert llm.stages == ["route", "search_answer"]
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert payload["summary"] == "邮件检索答案"
    assert payload["usage"]["tool_calls"] == 1


async def test_search_with_more_candidates_is_partial(setup, monkeypatch):
    expert, child, snap, views, _ = _case(setup, 1, "search")
    tool = setup[3].registry.get_tool("mail.search")
    original = tool.invoke

    def with_more(*, invocation, context):
        result = original(invocation=invocation, context=context)
        result.output["possible_more"] = True
        return result

    monkeypatch.setattr(tool, "invoke", with_more)
    result = await _execute(expert, child, snap, views)
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert result.status == TaskResultStatus.PARTIAL
    assert payload["missing_requirements"]
    assert "更多候选" in payload["summary"]


async def test_unsupported_intent_does_not_invoke_tools(setup):
    expert, child, snap, views, llm = _case(setup, 1, "unsupported")
    result = await _execute(expert, child, snap, views)
    assert result.status == TaskResultStatus.PARTIAL
    assert llm.stages == ["route"]
    payload = setup[1].load_artifact(f"mail_expert_{child.run_id}")
    assert payload["usage"]["tool_calls"] == 0


def test_runtime_registration_is_explicit_opt_in(tmp_path):
    config = tmp_path / "local.toml"
    config.write_text("[embedding]\nenabled = false\n[reranker]\nenabled = false\n[agent]\nmail_expert_enabled = true\n",
                      encoding="utf-8")
    runtime = LocalKnowledgeAgentRuntime(Settings(LKA_DATA_DIR=tmp_path / "runtime", LKA_LOCAL_CONFIG=config))
    definition, executor = runtime.agent_executor_registry.resolve("mail_expert", "1")
    assert definition.executor_kind == "workflow"
    assert isinstance(executor, MailExpertExecutor)
    assert runtime.tool_registry.get_tool_or_none("mail.snapshot") is not None
    assert runtime.tool_registry.get_tool_or_none("mail.batch_load") is not None


@pytest.mark.parametrize("start,end", [
    ("2026-06-01T00:00:00", "2026-06-02T00:00:00"),
    ("2026-06-02T00:00:00Z", "2026-06-01T00:00:00Z"),
    ("2025-01-01T00:00:00Z", "2026-06-01T00:00:00Z"),
])
def test_interval_must_be_explicit_bounded_and_ordered(start, end):
    with pytest.raises(ValueError):
        MailIntent(mode="list", received_from=start, received_before=end)
