from __future__ import annotations

import json
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.agent import run_agent_turn
from app.api.routes.matters import (
    create_matter,
    link_matter_source,
    list_matters,
    search_matters,
    update_matter,
)
from app.api.schemas import (
    AgentTurnRequest,
    MatterCreateRequest,
    MatterLinkSourceRequest,
    MatterUpdateRequest,
)
from app.core.config import get_settings
from app.core.llm import LLMResponse
from app.core.tools import ToolContext


def test_matter_api_create_search_update_and_link(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    assert "/matters" in app.openapi()["paths"]
    assert "/matters/search" in app.openapi()["paths"]

    created = create_matter(
        MatterCreateRequest(
            title="Submit ICA student pass documents",
            summary="Prepare IPA letter and appointment documents.",
            priority="high",
            due_at="2026-08-10T09:00:00+08:00",
            tags=["ICA", "NTU", "ICA"],
            source_links=[
                {
                    "source_type": "mail_message",
                    "source_id": "mail_msg_001",
                    "reason": "Imported from ICA email.",
                }
            ],
        ),
        request,
    )

    assert created.status == "open"
    assert created.priority == "high"
    assert created.tags == ["ICA", "NTU"]
    assert created.source_links[0].source_id == "mail_msg_001"

    search_result = search_matters(request, q="ICA documents", limit=10)

    assert len(search_result.matters) == 1
    assert search_result.matters[0].matter_id == created.matter_id

    updated = update_matter(
        created.matter_id,
        MatterUpdateRequest(status="in_progress", priority="urgent"),
        request,
    )

    assert updated.status == "in_progress"
    assert updated.priority == "urgent"

    linked = link_matter_source(
        created.matter_id,
        MatterLinkSourceRequest(
            source_type="agent_trace",
            source_id="trace_001",
            reason="User confirmed this matter.",
        ),
        request,
    )

    assert len(linked.source_links) == 2

    listed = list_matters(request, limit=10, status="in_progress")

    assert [matter.matter_id for matter in listed.matters] == [created.matter_id]


def test_matter_tools_and_runtime_now_are_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    context = ToolContext(
        session_id="session_matter_tools",
        trace_id="trace_matter_tools",
        context_id="ctx_matter_tools",
    )

    matter_tool_names = {
        tool.name for tool in app.state.runtime.tool_registry.list_tools(package="matter")
    }
    matter_tool_specs = {
        tool.name: tool
        for tool in app.state.runtime.tool_registry.list_tools(package="matter")
    }
    runtime_tool_names = {
        tool.name for tool in app.state.runtime.tool_registry.list_tools(package="runtime")
    }

    assert matter_tool_names == {
        "matter.create",
        "matter.create_many",
        "matter.search",
        "matter.list",
        "matter.update",
        "matter.link_source",
    }
    assert runtime_tool_names == {"runtime.now"}
    create_schema = matter_tool_specs["matter.create"].input_schema
    create_many_schema = matter_tool_specs["matter.create_many"].input_schema
    assert create_schema["required"] == ["title", "summary"]
    assert create_schema["properties"]["status"]["allowed_values"] == [
        "open",
        "in_progress",
        "waiting",
        "done",
        "cancelled",
    ]
    assert create_schema["properties"]["priority"]["allowed_values"] == [
        "low",
        "normal",
        "high",
        "urgent",
    ]
    assert create_many_schema["properties"]["matters"]["items"] == create_schema

    create_result = app.state.runtime.tool_executor.execute(
        invocation_id="matter_create_001",
        tool_name="matter.create",
        tool_input={
            "title": "NTU course registration",
            "summary": "Check registration platform and deadline.",
            "tags": ["NTU"],
            "source_links": [],
            "metadata": {},
        },
        context=context,
    )
    matter_id = create_result.output["matter"]["matter_id"]

    assert create_result.status == "completed"

    create_many_result = app.state.runtime.tool_executor.execute(
        invocation_id="matter_create_many_001",
        tool_name="matter.create_many",
        tool_input={
            "matters": [
                {
                    "title": "ICA appointment",
                    "summary": "Attend ICA student pass formalities.",
                    "due_at": "2026-08-14T13:00:00+08:00",
                    "tags": ["ICA"],
                    "source_links": [
                        {
                            "source_type": "mail_message",
                            "source_id": "mail_msg_ica",
                            "reason": "Extracted from ICA email.",
                        }
                    ],
                    "metadata": {},
                },
                {
                    "title": "NTUSO audition",
                    "summary": "Prepare audition materials.",
                    "due_at": "2026-08-12T19:20:00+08:00",
                    "tags": ["NTUSO"],
                    "source_links": [],
                    "metadata": {},
                },
            ],
        },
        context=context,
    )
    search_result = app.state.runtime.tool_executor.execute(
        invocation_id="matter_search_001",
        tool_name="matter.search",
        tool_input={"query": "registration", "limit": 10},
        context=context,
    )
    now_result = app.state.runtime.tool_executor.execute(
        invocation_id="runtime_now_001",
        tool_name="runtime.now",
        tool_input={"timezone": "Asia/Shanghai"},
        context=context,
    )

    assert search_result.output["matters"][0]["matter_id"] == matter_id
    assert create_many_result.status == "completed"
    assert create_many_result.output["matters_created"] == 2
    assert create_many_result.output["matters"][0]["source_links"][0]["source_id"] == (
        "mail_msg_ica"
    )
    assert now_result.status == "completed"
    assert now_result.output["timezone"] == "Asia/Shanghai"
    assert "T" in now_result.output["local"]


def test_agent_turn_can_create_matter_and_receives_current_time(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _MatterCreatingLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_agent_matter_create",
            user_input="帮我新增一个待办：8月10日前完成ICA学生签证材料准备。",
        ),
        request,
    )

    assert response.selected_package == "matter"
    assert [event.tool_name for event in response.tool_events] == ["matter.create"]
    assert response.tool_events[0].feedback["status"] == "accepted"
    assert response.tool_events[0].feedback["source"] == "llm"
    assert response.answer == "已创建 ICA 学生签证材料准备事项。"
    assert fake_llm.route_context is not None
    assert fake_llm.decision_context is not None
    assert "current_time" in fake_llm.route_context
    assert "current_time" in fake_llm.decision_context
    assert fake_llm.decision_context["current_time"]["timezone"] == "Asia/Shanghai"

    created = app.state.runtime.search_matters(query="ICA 学生签证", limit=10)

    assert len(created.matters) == 1
    assert created.matters[0].due_at == "2026-08-10T23:59:00+08:00"


class _MatterCreatingLLM:
    def __init__(self) -> None:
        self.route_context: dict | None = None
        self.decision_context: dict | None = None
        self.decision_calls = 0

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        payload = json.loads(user_prompt)
        if "Tool Result Checker" in system_prompt:
            content = json.dumps(
                {
                    "status": "accepted",
                    "message": "Tool result is a valid observation.",
                    "remaining_work": "Answer the user.",
                }
            )
        elif "Choose at most one tool package" in system_prompt:
            self.route_context = payload["session_context_window"]
            content = json.dumps(
                {
                    "selected_package": "matter",
                    "reason": "User asks to create a local task.",
                    "search_query": "ICA 学生签证",
                }
            )
        elif "Choose the next single action" in system_prompt:
            self.decision_calls += 1
            self.decision_context = payload["session_context_window"]
            if self.decision_calls == 1:
                content = json.dumps(
                    {
                        "action": "call_tool",
                        "tool_name": "matter.create",
                        "tool_input": {
                            "title": "完成ICA学生签证材料准备",
                            "summary": "8月10日前完成ICA学生签证材料准备。",
                            "status": "open",
                            "priority": "high",
                            "due_at": "2026-08-10T23:59:00+08:00",
                            "tags": ["ICA", "student pass"],
                            "source_links": [],
                            "metadata": {"created_from": "agent_turn"},
                        },
                        "reason": "Persist the requested matter.",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "action": "answer",
                        "answer": "已创建 ICA 学生签证材料准备事项。",
                        "reason": "matter.create completed.",
                    }
                )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_matter_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )
