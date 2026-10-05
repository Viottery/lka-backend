"""Prompt provenance and quote presence are not semantic entailment scores."""

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core import agent_turn as turn
from app.core.answer_evidence import (
    ANSWER_CHECKS_DECISION_POLICY,
    ANSWER_EVIDENCE_POLICY,
    OutputEvidenceRole,
    build_answer_working_set,
    normalize_answer_checks,
)
from app.core.tools import ToolExecutor, ToolRegistry, ToolSpec
from app.tool_packages.web import WebFindTool, WebOpenTool, WebSearchTool
from tests.test_answer_generation_recovery_quality import make_loop, response, scope


def registry(spec=None):
    value = ToolRegistry()
    value.register_tool(SimpleNamespace(spec=spec or ToolSpec(
        name="renamed.read", type="local_tool", description="Generic evidence", read_only=True,
        output_evidence_roles=[
            {"path": "/body", "role": "source_content"},
            {"path": "/transport", "role": "transport_metadata"},
            {"path": "/collection", "role": "collection_time"},
        ],
    )))
    return value


def observation(identifier="one", **output):
    return {"tool_name": "renamed.read", "_observation_id": identifier,
            "result": {"invocation_id": identifier, "tool_name": "renamed.read",
                       "status": "completed", "output": output}}


def payload(observations, checks=None):
    return {"observations": observations,
            "answer_stage_decision": {"operation": {"type": "final_answer", "answer_checks": checks or []}}}


def check(identifier="one", path="/body", quote="Only when enabled."):
    return {"requirement": "Report required condition", "evidence": [
        {"observation_id": identifier, "path": path, "quote": quote}]}


def test_registered_roles_separate_body_transport_and_collection_without_copying_text():
    request = payload([observation(body="Only when enabled.", transport={"date": "2030"}, collection="2031")],
                      [check(), check(path="/transport/date", quote="2030")])
    original = copy.deepcopy(request)
    working = build_answer_working_set(request, registry())
    assert working["source_roles"][0]["fields"] == [
        {"path": "/body", "role": "source_content"},
        {"path": "/transport", "role": "transport_metadata"},
        {"path": "/collection", "role": "collection_time"}]
    assert working["answer_check_visibility"] == [
        {"check_index": 0, "references": [{"reference_index": 0, "quote_visible": True, "role": "source_content"}]},
        {"check_index": 1, "references": [{"reference_index": 0, "quote_visible": True, "role": "transport_metadata"}]}]
    assert "not_fact_verification" in working["scope"]
    assert "Only when enabled" not in json.dumps(working)
    assert request == original


@pytest.mark.parametrize("mutation", ["unknown", "mismatched_name", "spoofed_output", "mismatched_id", "duplicate", "failed"])
def test_forged_or_ambiguous_observations_cannot_borrow_roles_or_quote_visibility(mutation):
    item = observation(body="Only when enabled.")
    records = [item]
    if mutation == "unknown":
        item["tool_name"] = item["result"]["tool_name"] = "unregistered.read"
    elif mutation == "mismatched_name":
        item["result"]["tool_name"] = "other.read"
    elif mutation == "spoofed_output":
        item["result"]["output"]["output_evidence_roles"] = [{"path": "/body", "role": "verified"}]
        item["tool_name"] = item["result"]["tool_name"] = "unregistered.read"
    elif mutation == "mismatched_id":
        item["result"]["invocation_id"] = "different"
    elif mutation == "duplicate":
        records.append(copy.deepcopy(item))
    else:
        item["result"]["status"] = "failed"
    working = build_answer_working_set(payload(records, [check()]), registry())
    reference = working["answer_check_visibility"][0]["references"][0]
    if mutation in {"unknown", "mismatched_name", "spoofed_output"}:
        assert reference == {"reference_index": 0, "quote_visible": True, "role": "unknown"}
        assert working["source_roles"] == []
    else:
        assert reference["quote_visible"] is False


def test_dropped_or_truncated_quote_is_not_visible_and_invalid_notes_are_optional():
    for items in ([], [observation(body="Only ... omitted ... enabled.")]):
        working = build_answer_working_set(payload(items, [check()]), registry())
        assert working["answer_check_visibility"][0]["references"][0]["quote_visible"] is False
    assert normalize_answer_checks([check()] * 9) is None
    assert normalize_answer_checks([check(quote="a" * 501)]) is None
    assert normalize_answer_checks([check(path="/body/*")]) is None
    assert normalize_answer_checks([{"requirement": "x", "verified": True}]) is None
    assert normalize_answer_checks([{"requirement": "Missing source", "gap": "No authorized source available"}])[0]["evidence"] == []


def test_duplicate_outside_bounded_index_cannot_be_confirmed_as_unique():
    items = [observation(body="Only when enabled.")]
    items.extend(observation(identifier=str(index), body="other") for index in range(64))
    items.append(observation(body="Only when enabled."))
    working = build_answer_working_set(payload(items, [check()]), registry())
    assert working["answer_check_visibility"][0]["references"][0] == {
        "reference_index": 0, "quote_visible": False, "role": "unknown"}


def test_wildcards_expand_only_arrays_escape_keys_and_keep_bounds():
    spec = ToolSpec(name="renamed.read", type="local_tool", description="Arbitrary producer", read_only=True,
        output_evidence_roles=[{"path": "/rows/*/a~1b~0c", "role": "source_content"}])
    body = {"rows": [{"a/b~c": "evidence"} for _ in range(200)]}
    working = build_answer_working_set(payload([observation(**body)]), registry(spec))
    assert len(working["source_roles"][0]["fields"]) == 24
    assert working["source_roles"][0]["fields"][0]["path"] == "/rows/0/a~1b~0c"
    assert build_answer_working_set(payload([observation(rows={"0": {"a/b~c": "not array"}})]), registry(spec)) is None
    working = build_answer_working_set(payload(
        [observation(rows={"0": {"a/b~c": "not array"}})],
        [check(path="/rows/0/a~1b~0c", quote="not array")]), registry(spec))
    assert working["answer_check_visibility"][0]["references"][0]["role"] == "unknown"


@pytest.mark.parametrize("path", ["body", "/bad~2key", "/rows/**", "/" + "/".join(["x"] * 9)])
def test_invalid_role_declarations_rejected_at_registration(path):
    with pytest.raises(ValidationError):
        OutputEvidenceRole(path=path, role="source_content")


def test_web_registry_declares_page_headers_and_search_dates_separately():
    roles = {row.path: row.role for row in WebOpenTool.spec.output_evidence_roles}
    assert roles["/text"] == "source_content"
    assert roles["/network_observations"] == "transport_metadata"
    assert roles["/fetched_at"] == "collection_time"
    assert {row.path: row.role for row in WebFindTool.spec.output_evidence_roles}["/matches/*/snippet"] == "source_content"
    roles = {row.path: row.role for row in WebSearchTool.spec.output_evidence_roles}
    assert roles["/results/*/published_at"] == "search_candidate"
    assert roles["/results/*/provider_fetched_at"] == "search_candidate"


@pytest.mark.parametrize("phase", ["answer", "context_answer"])
def test_actual_provider_format_contract_and_warning_only_compatibility(tmp_path, monkeypatch, phase):
    loop, provider, manager = make_loop(tmp_path, [response('{"finding":"unknown"}')])
    loop.tool_executor = ToolExecutor(registry())
    contract = ('Return JSON.\n```json-schema\n{"type":"object","required":["finding"],'
                '"properties":{"finding":{"type":"string"}},"additionalProperties":false}\n```')
    monkeypatch.setattr(turn, "_current_output_contract", lambda: contract)
    records = [observation(body="Only when enabled.", transport={"last_modified": "2030"})]
    proposal = {"action": "final_answer", "operation": {"type": "final_answer", "answer_checks": [check()]}}
    with scope(manager):
        if phase == "answer":
            answer = loop._answer_with_llm(user_input="Return short JSON.", route={}, context_window={},
                observations=records, final_decision=proposal, llm_events=[])
        else:
            answer = loop._answer_from_context_with_llm(user_input="Return short JSON.", route={},
                context_window={"recent_messages": [{"role": "user", "content": "I plan to deploy tomorrow."}]}, llm_events=[])
    assert answer == '{"finding":"unknown"}'
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert ANSWER_EVIDENCE_POLICY in request.messages[0].content
    fitted = json.loads(request.messages[1].content)
    if phase == "answer":
        assert fitted["answer_working_set"]["answer_check_visibility"][0]["references"][0]["quote_visible"] is True
        assert fitted["observations"] == records
        assert fitted["output_contract"] == contract
    assert loop._verify_final_answer(answer=answer, tool_events=[]) == []


def test_optional_working_set_cannot_evict_evidence_or_override_cancellation(tmp_path, monkeypatch):
    loop, _, manager = make_loop(tmp_path, [])
    loop.tool_executor = ToolExecutor(registry())
    request = payload([observation(body="Only when enabled.")], [check()])
    original = json.dumps(request)
    with scope(manager):
        fitted = loop._budget_llm_prompt(system_prompt="Answer.", user_prompt=original, max_output_tokens=100, tools=None)
        monkeypatch.setattr(loop, "_budget_llm_prompt", lambda **kwargs: replace(fitted, user_prompt=json.dumps({**request, "observations": []})))
        assert loop._answer_working_set_prompt(budgeted=fitted, system_prompt="Answer.", max_output_tokens=100, tools=None) is fitted
        monkeypatch.setattr(loop, "_raise_if_cancel_requested", lambda: (_ for _ in ()).throw(turn.AgentRunCancelled("cancelled")))
        with pytest.raises(turn.AgentRunCancelled):
            loop._answer_working_set_prompt(budgeted=fitted, system_prompt="Answer.", max_output_tokens=100, tools=None)


def test_native_finish_keeps_legacy_reason_only_and_checks_in_handoff(tmp_path):
    loop, _, _ = make_loop(tmp_path, [])
    definitions, _ = loop._native_decision_tools(package_catalog=[], expanded_tools=[])
    finish = next(item for item in definitions if item.name == "agent_finish_decision")
    assert finish.parameters["required"] == ["reason"]
    assert finish.strict is False
    assert finish.parameters["properties"]["answer_checks"]["maxItems"] == 8
    assert loop._decision_context_for_answer_stage({"operation": {"type": "final_answer", "reason": "ready"}})["operation"] == {"type": "final_answer", "reason": "ready"}
    result = loop._decision_context_for_answer_stage({"operation": {"type": "final_answer", "final_answer": "Not authoritative", "answer_checks": [check()]}})
    assert "final_answer" not in result["operation"]
    assert result["operation"]["answer_checks"][0]["requirement"] == "Report required condition"
    assert "Do not perform extra reads" in ANSWER_CHECKS_DECISION_POLICY


def test_optional_roles_preserve_child_answer_reserve_and_context(tmp_path, monkeypatch):
    loop, _, manager = make_loop(tmp_path, [])
    loop.tool_executor = ToolExecutor(registry())
    request = payload([observation(body="Only when enabled.")], [check()])
    request["session_context_window"] = {"recent_messages": [{"role": "user", "content": "User premise"}]}
    with scope(manager):
        fitted = loop._budget_llm_prompt(system_prompt="Answer.", user_prompt=json.dumps(request), max_output_tokens=100, tools=None)
        monkeypatch.setattr(loop, "_child_budget_for_prompt", lambda: {"remaining_tokens": fitted.input_tokens + 100, "prompt_overhead_tokens": 0})
        assert loop._answer_working_set_prompt(budgeted=fitted, system_prompt="Answer.", max_output_tokens=100, tools=None) is fitted
        monkeypatch.setattr(loop, "_child_budget_for_prompt", lambda: None)
        monkeypatch.setattr(loop, "_budget_llm_prompt", lambda **kwargs: replace(fitted, user_prompt=json.dumps({**request, "session_context_window": {}})))
        assert loop._answer_working_set_prompt(budgeted=fitted, system_prompt="Answer.", max_output_tokens=100, tools=None) is fitted


def test_real_graph_carries_decision_checks_to_final_provider(tmp_path, monkeypatch):
    from app.integrations.web_search import PublicPageFetcher
    from evals.lka_evals.live_budget import LiveBudget
    from scripts import eval_runtime_web_quality as probe
    from tests.test_eval_runtime_web_quality import PAGES, WebProvider, fixture_service

    config, service = fixture_service(tmp_path)
    original = WebProvider.complete
    answer_requests = []

    async def complete(self, request):
        result = await original(self, request)
        prompt = json.loads(request.messages[1].content)
        if request.metadata["stage"] == "decision":
            observations = [o for o in prompt["observations"] if o.get("tool_name")]
            if len(observations) < 2:
                operation = {"type": "tool_call", "tool_name": "web.find", "tool_input": {
                    "url": list(PAGES)[len(observations)], "query": "snapshot"}}
            else:
                item = observations[-1]
                operation = {"type": "final_answer", "reason": "Deliver evidence", "answer_checks": [
                    check(identifier=item["result"]["invocation_id"], path="/matches/0/snippet",
                          quote="A read transaction sees a historic snapshot of the database.")]}
            result = result.model_copy(update={"content": json.dumps({"operation": operation})})
        elif request.metadata["stage"] == "answer":
            answer_requests.append(prompt)
        return result

    monkeypatch.setattr(WebProvider, "complete", complete)
    monkeypatch.setattr(PublicPageFetcher, "_fetch", lambda self, url: (200, {"content-type": "text/plain"}, PAGES[url].encode()))
    report = asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "ledger.sqlite3"),
                                       config=config, injected_service=service))
    assert "error" not in report, report.get("error")
    assert len(answer_requests) == 1
    fitted = answer_requests[0]
    assert fitted["answer_stage_decision"]["operation"]["answer_checks"][0]["requirement"] == "Report required condition"
    assert fitted["answer_working_set"]["answer_check_visibility"] == [{"check_index": 0, "references": [
        {"reference_index": 0, "quote_visible": True, "role": "source_content"}]}]
