import pytest

from app.core.local_config import AgentConfig
from tests.test_answer_generation_recovery_quality import make_loop, response, scope
from tests.test_control_generation_quality import _decide


@pytest.mark.parametrize("capability,expected", [("deepseek", False), (None, None)])
def test_opt_in_uses_only_supported_selected_client(tmp_path, capability, expected):
    loop, provider, manager = make_loop(tmp_path, [response(
        '{"operation":{"type":"final_answer","reason":"sufficient"}}')],
        selected_thinking=capability)
    loop.decision_thinking_enabled = False
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
    assert provider.requests[0].thinking_enabled is expected


def test_default_and_route_preserve_provider_strategy(tmp_path):
    assert AgentConfig().decision_thinking_enabled is None
    loop, provider, manager = make_loop(tmp_path, [response('null')], selected_thinking="deepseek")
    loop.decision_thinking_enabled = False
    with scope(manager):
        loop._route(user_input="Confirm my statement.", package_catalog=[], context_window={},
                    llm_events=[], decision_events=[])
    assert provider.requests[0].thinking_enabled is None


def test_scope_contract_never_proves_global_absence():
    from app.tool_packages.knowledge import _indexed_evidence_scope
    scope = _indexed_evidence_scope()
    assert scope["proves_global_absence"] is False
    assert scope["includes_unindexed_workspace_files"] is False
    assert scope["query_bounded"] is True


def test_empty_search_and_inventory_return_scope_contract(monkeypatch):
    from types import SimpleNamespace

    from app.core.tools import ToolContext, ToolInvocation
    from app.tool_packages import knowledge

    monkeypatch.setattr(knowledge, "_authorized_knowledge_source_ids", lambda *_: [])
    service = SimpleNamespace(
        list_sources=lambda **_: [],
        search=lambda **_: SimpleNamespace(model_dump=lambda **_: {"results": []}),
    )
    for tool in (knowledge.SearchKnowledgeTool(service), knowledge.ListKnowledgeSourcesTool(service)):
        result = tool.invoke(invocation=ToolInvocation(invocation_id="scope-test", tool=tool.spec,
                                                       session_id="isolated", context_id="test",
                                                       input={"query": "synthetic"}),
                             context=ToolContext(session_id="isolated"))
        assert result.status == "completed"
        assert result.output["evidence_scope"]["proves_global_absence"] is False
