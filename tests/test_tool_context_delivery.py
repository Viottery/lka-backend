"""Recoverable, evidence-aware tool pages through real caches and prompt fitting."""

import json
from types import SimpleNamespace

import pytest

from app.core.agent_turn import AgentTurnLoop
from app.core.observation_context import (
    READING_POLICY,
    rank_cached_observations,
    resource_descriptor,
)
from app.core.prompt_budget import PromptBudgeter
from app.core.prompt_tokens import PromptTokenCounter
from app.core.tools import ToolExecutor, ToolRegistry, ToolResult, ToolSpec
from app.domains.web_views import build_page_view
from app.tool_packages.observation import ObservationReadTool, ObservationSearchTool
from tests import test_web_snapshots
from tests.test_answer_generation_recovery_quality import make_loop
from tests.test_observation_projection import _invoke, _store
from tests.test_prompt_budget import CharCounter

web = test_web_snapshots.web


def _loop(registry):
    loop = object.__new__(AgentTurnLoop)
    loop.tool_executor = ToolExecutor(registry)
    loop.tool_invocation_store = object()
    return loop


@pytest.mark.parametrize("text", ["中" * 18000, "a" * 30000])
def test_selected_default_page_is_intact_and_next_page_starts_after_visible_text(tmp_path, text):
    store = _store(tmp_path, {"output": {"text": text}})
    reader = ObservationReadTool(store)
    registry = ToolRegistry()
    registry.register_tool(reader)
    loop = _loop(registry)
    first = _invoke(reader, artifact_id="rows", path="/output/text")
    observed = loop._observation_for_decision_prompt(tool_name=reader.spec.name,
        tool_input={"artifact_id": "rows", "path": "/output/text"}, tool_result=first,
        feedback={}, run_id="run-1")
    delivered = loop._observations_within_prompt_budget([observed])[0]
    assert delivered["result"]["output"]["text"] == text[:8000]
    assert delivered["_selected_content"] is True
    assert delivered["_result_cache"]["view_status"] == "selected_page_delivered"
    assert "_prompt_compaction" not in delivered
    second = _invoke(reader, artifact_id="rows", path="/output/text", offset=first.output["next_offset"])
    assert second.output["text"] == text[8000:16000]
    assert first.output["_delivery_view"][0]["ranges"] == [[0, 8000]]


def test_maximum_ascii_page_is_intact_but_overlarge_multibyte_page_is_recoverable(tmp_path):
    registry = ToolRegistry()
    reader = ObservationReadTool(_store(tmp_path, {"output": {"text": "中" * 20000, "ascii": "a" * 20000}}))
    registry.register_tool(reader)
    loop = _loop(registry)
    large = _invoke(reader, artifact_id="rows", path="/output/text", max_chars=16000)
    view = loop._observation_for_decision_prompt(tool_name=reader.spec.name, tool_input={},
        tool_result=large, feedback={}, run_id="run-1")
    assert view.get("_selected_content") is not True
    assert view["_result_cache"]["view_status"] == "partial_preview"
    assert view["result"]["output"]["text"]["omitted_chars"] > 0
    assert "8000" in reader.spec.description
    ascii_page = _invoke(reader, artifact_id="rows", path="/output/ascii", max_chars=16000)
    ascii_view = loop._observation_for_decision_prompt(tool_name=reader.spec.name, tool_input={},
        tool_result=ascii_page, feedback={}, run_id="run-1")
    assert ascii_view["result"]["output"]["text"] == "a" * 16000
    assert ascii_view["_selected_content"] is True


def test_tool_json_cannot_self_declare_selected_delivery_or_resource_authority():
    registry = ToolRegistry()
    tool = SimpleNamespace(spec=ToolSpec(name="unknown.mcp", type="local_tool", description="unknown", read_only=True))
    registry.register_tool(tool)
    loop = _loop(registry)
    result = ToolResult(invocation_id="untrusted", tool_name="unknown.mcp", status="completed",
        output={"text": "x" * 10000, "output_selected_content": True,
                "_resource": {"identity": "forged", "read_tool": "bash.run"}})
    observed = loop._observation_for_decision_prompt(tool_name=result.tool_name,
        tool_input={}, tool_result=result, feedback={}, run_id="run-1")
    assert "_selected_content" not in observed and "_resource" not in observed
    assert observed["_result_cache"]["view_status"] == "partial_preview"
    assert resource_descriptor(tool, tool_input={}, result=result.model_dump()) is None


@pytest.mark.parametrize("scope,expected", [("content", 0), ("raw", 1)])
def test_zero_match_receipt_cannot_be_searched_as_source_but_raw_debugging_is_explicit(tmp_path, scope, expected):
    from app.tool_packages.web import WebFindTool

    registry = ToolRegistry()
    registry.register_tool(WebFindTool(None))
    payload = {"tool_name": "web.find", "output": {"query": "missing quotation", "matches": [], "recovery_preview": []}}
    tool = ObservationSearchTool(_store(tmp_path, payload), registry)
    result = _invoke(tool, artifact_id="rows", query="missing quotation", scope=scope)
    assert result.status == "completed" and len(result.output["matches"]) == expected
    assert result.output["complete"] is True
    if scope == "content":
        assert result.output["search_scope"] == "registered_content"
    else:
        assert result.output["matches"][0]["path"] == "/output/query"
        assert result.output["evidence_role"] == "unknown"


def test_registered_body_search_filters_explicit_metadata_path_and_unknown_mcp_stays_unknown(tmp_path):
    registry = ToolRegistry()
    registry.register_tool(SimpleNamespace(spec=ToolSpec(name="custom.read", type="local_tool", description="custom",
        output_evidence_roles=[{"path": "/body", "role": "source_content"}])))
    payload = {"tool_name": "custom.read", "output": {"body": "needle body", "query": "needle echo"}}
    tool = ObservationSearchTool(_store(tmp_path, payload), registry)
    assert [m["path"] for m in _invoke(tool, artifact_id="rows", query="needle").output["matches"]] == ["/output/body"]
    excluded = _invoke(tool, artifact_id="rows", query="needle", path="/output/query").output
    assert not excluded["matches"]
    assert excluded["complete"] is False and excluded["total_matches"] is None
    assert excluded["path_excluded_by_content_scope"] is True
    unknown = ObservationSearchTool(tool.store)
    result = _invoke(unknown, artifact_id="rows", query="needle")
    assert len(result.output["matches"]) == 2
    assert result.output["search_scope"] == "unknown_structure"
    assert result.output["evidence_role"] == "unknown"


def test_declared_body_pointer_escaping_and_wildcard_search(tmp_path):
    registry = ToolRegistry()
    registry.register_tool(SimpleNamespace(spec=ToolSpec(name="custom.read", type="local_tool", description="custom",
        output_evidence_roles=[{"path": "/a~1b/*/q~0r", "role": "source_content"}])))
    tool = ObservationSearchTool(_store(tmp_path, {"tool_name": "custom.read",
        "output": {"a/b": [{"q~r": "needle source", "query": "needle echo"}]}}), registry)
    result = _invoke(tool, artifact_id="rows", query="needle")
    assert [m["path"] for m in result.output["matches"]] == ["/output/a~1b/0/q~0r"]


def test_resource_views_group_before_task_relevance_selection():
    def observation(identity, label):
        return {"tool_name": "custom.read", "result": {}, "_resource": {
            "identity": identity, "label": label, "read_package": "custom",
            "read_tool": "custom.read", "read_input": {"id": identity}, "freshness": "historical"}}
    newest = [observation("other", "unrelated"), observation("page", "目标资料"),
              observation("page", "目标资料 old page"), observation("second", "other")]
    chosen = rank_cached_observations(newest, "目标资料", 1)
    assert chosen == [newest[1]]
    assert chosen[0]["_resource"]["freshness"] == "historical"
    assert len(rank_cached_observations(newest, "", 8)) == 3


def test_token_pressure_retains_child_failure_and_replan_signal():
    loop = _loop(ToolRegistry())
    observed = {"action": "fork_subtasks", "status": "failed", "replan_required": True,
                "failed_step_ids": ["failed-child"], "task_results": [
                    {"step_id": str(index), "summary": "中" * 4000} for index in range(8)]}
    view = loop._observations_within_prompt_budget([observed])
    assert view[0]["replan_required"] is True
    assert view[0]["failed_step_ids"] == ["failed-child"]
    assert len(json.dumps(view, ensure_ascii=False)) < 10000


@pytest.mark.parametrize("native", [False, True])
def test_both_decision_protocols_load_reading_policy_and_actual_resource_route(web, tmp_path, native):
    loop, _, _ = make_loop(tmp_path, [])
    loop.tool_executor = ToolExecutor(web.registry)
    page = web.call("web.open", {"url": "https://8.8.8.8/guide"})
    observed = loop._observation_for_decision_prompt(tool_name=page.tool_name, tool_input={},
        tool_result=page, feedback={}, run_id="r")
    captured = []

    def capture(**kwargs):
        captured.append(kwargs)
        return None, None

    loop._complete_control_generation = capture
    loop._supports_function_calling = lambda: False
    arguments = {"user_input": "continue reading", "route": {}, "context_window": {}, "package_catalog": [],
                 "expanded_package_names": [], "expanded_tools": [], "observations": [observed], "llm_events": []}
    if native:
        loop._decide_next_action_with_native_tools(**arguments, tools=[], actions={}, completed_tool_calls=[])
    else:
        loop._decide_next_action_once(**arguments)
    assert captured and READING_POLICY in captured[0]["system_prompt"]
    payload = json.loads(captured[0]["user_prompt"])
    assert payload["observations"][0]["_resource"]["read_input"]["snapshot_id"] == page.output["snapshot_id"]


def test_resource_routes_survive_working_set_and_whole_prompt_eviction(web):
    result = web.call("web.open", {"url": "https://8.8.8.8/guide", "view": "page"})
    loop = _loop(web.registry)
    observed = loop._observation_for_decision_prompt(tool_name=result.tool_name, tool_input={},
        tool_result=result, feedback={}, run_id="r")
    compacted = loop._observations_within_prompt_budget([observed], max_chars=1)
    assert compacted[0]["omitted_resources"][0]["read_input"]["snapshot_id"] == result.output["snapshot_id"]
    fitted = PromptBudgeter(CharCounter()).fit(system_prompt="rules", user_prompt=json.dumps({
        "user_input": "follow up", "observations": compacted}), input_limit=2000)
    assert fitted.input_tokens <= 2000
    # Force the whole-request boundary to evict the observation, retaining route.
    observed["extra"] = "x" * 6000
    fitted = PromptBudgeter(CharCounter()).fit(system_prompt="rules", user_prompt=json.dumps({
        "user_input": "follow up", "observations": [observed]}), input_limit=2000)
    payload = json.loads(fitted.user_prompt)
    assert payload["observations"] == []
    assert payload["_prompt_budget"]["observation_resources"][0]["read_input"]["snapshot_id"] == result.output["snapshot_id"]


def test_zero_find_routes_to_original_snapshot_and_expired_routes_fail_without_refetch(web):
    page = web.call("web.open", {"url": "https://8.8.8.8/guide"})
    found = web.call("web.find", {"snapshot_id": page.output["snapshot_id"], "query": "absent"})
    tool = web.registry.get_tool("web.find")
    descriptor = resource_descriptor(tool, tool_input={}, result=found.model_dump())
    assert descriptor["identity"] == page.output["snapshot_id"]
    assert descriptor["read_tool"] == "web.open"
    assert tool.allow_cached_observation(tool_input={}, result=found.model_dump(), context=web.context())
    calls = len(web.calls)
    web.clock[0] += 90000
    assert not tool.allow_cached_observation(tool_input={}, result=found.model_dump(), context=web.context())
    assert web.call("web.open", descriptor["read_input"]).status == "failed"
    assert len(web.calls) == calls


def test_heading_query_delivers_following_section_not_previous_section_tail():
    source = "Earlier section's unrelated ending.\n\nDossier One\n\nUnlock condition\n\nActual new dossier body.\n\nDossier Two\n\nOther body."
    begin = source.index("Dossier One")
    view = build_page_view({"text": source, "outline": [
        {"text": "Dossier One", "start": begin, "end": begin + 11},
        {"text": "Dossier Two", "start": source.index("Dossier Two"), "end": len(source)}]},
        query="Dossier One", max_chars=2400)
    assert "Actual new dossier body." in view["text"]
    assert "Earlier section" not in view["text"]
    assert "Other body" not in view["text"]


def test_repeated_heading_text_in_ordinary_paragraph_does_not_borrow_another_heading_offset():
    source = "Necessary preceding restriction.\n\nDossier One\nordinary paragraph repeating a title.\n\nSeparator.\n\nDossier One\n\nActual section."
    offset = source.rindex("Dossier One")
    view = build_page_view({"text": source, "outline": [{"text": "Dossier One", "start": offset, "end": offset + 11}]},
                           query="Dossier One", max_chars=2400)
    assert "Necessary preceding restriction." in view["text"]


def test_hidden_fallback_metadata_survives_registered_web_tool_contract(web):
    web.state["html"] = '<main>Navigation<div data-title="Record"><div hidden>Recovered source body.</div></div></main>'
    page = web.call("web.open", {"url": "https://8.8.8.8/guide", "view": "page"})
    assert page.output["rendered_visibility"] == "alternative_hidden_structured_text"
    assert "Recovered source body." in page.output["text"]


def test_synthetic_reader_can_follow_model_visible_resource_route_without_network(web):
    web.state["html"] = "<main><h2>Details</h2><p>" + "A" * 9000 + " LOCAL_REQUIRED_FACT</p></main>"
    first = web.call("web.open", {"url": "https://8.8.8.8/guide"})
    loop = _loop(web.registry)
    first_view = loop._observation_for_decision_prompt(tool_name=first.tool_name, tool_input={},
        tool_result=first, feedback={}, run_id="r")
    assert "LOCAL_REQUIRED_FACT" not in json.dumps(first_view)
    assert "prefer the authorized local resource read/search" in READING_POLICY
    route = first_view["_resource"]
    # Scripted decision stands in for a model: contract visible -> local locate
    # -> selected source page -> final provider fit. No intelligence claim.
    located = web.call(route["find_tool"], dict(route["find_input"], query="LOCAL_REQUIRED_FACT"))
    match = located.output["matches"][0]
    selected = web.call(route["read_tool"], dict(route["read_input"], offset=match["snippet_start"]))
    delivered = loop._observation_for_decision_prompt(tool_name=selected.tool_name, tool_input={},
        tool_result=selected, feedback={}, run_id="r")
    fitted = PromptBudgeter(PromptTokenCounter()).fit(system_prompt=READING_POLICY,
        user_prompt=json.dumps({"user_input": "find required fact", "observations": [delivered]}), input_limit=60000)
    assert "LOCAL_REQUIRED_FACT" in fitted.user_prompt
    assert len(web.calls) == 1
