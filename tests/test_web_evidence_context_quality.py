"""Source context and observed transport facts are not semantic verification."""

import copy
import json

import httpx
import pytest

from app.core import tool_result_gate
from app.core.agent_storage import SqliteAgentRunStore
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult
from app.integrations.web_search import MAX_FIND_CONTEXT_CHARS, PublicPageFetcher, WebSearchError
from app.tool_packages.web import WebFindTool, WebOpenTool
from tests.test_answer_generation_recovery_quality import make_loop, response, scope
from tests.test_web_page_paging_quality import fetcher

URL = "https://8.8.8.8/guide"


def test_adjacent_condition_survives_when_old_fixed_excerpt_lost_it():
    statement = "The Orion mode supports parallel operations. " + "Explanation. " * 40
    condition = "Only with the explicit opt-in flag; the default build does NOT enable it."
    client = fetcher(f"<main><p>{statement}</p><p>{condition}</p></main>")
    page = client.open(URL)
    result = client.find(URL, "Orion mode", expected_text_sha256=page["text_sha256"])
    hit = result["matches"][0]
    old_start = max(0, hit["match_start"] - 100)
    assert condition not in page["text"][old_start:old_start + 400]
    assert condition in hit["snippet"]
    assert hit["context_complete"] is True
    assert hit["snippet_scope"] == "adjacent_readable_blocks"
    assert hit["snippet"] == page["text"][hit["snippet_start"]:hit["snippet_end"]]
    assert result["coverage_scope"] == "literal_match_page_not_semantic_verification"


def test_preceding_qualification_is_kept_without_keyword_rules():
    condition = "限制：只有用户明确启用才有效；默认不支持🙂。"
    client = fetcher(f"<main><p>{condition}</p><p>任意改名功能X 已提供。</p><p>其他说明。</p></main>")
    hit = client.find(URL, "改名功能X")["matches"][0]
    assert condition in hit["snippet"] and hit["context_complete"] is True


def test_large_neighbors_are_explicitly_omitted_and_recoverable_with_revision_guard():
    body = f"<main><p>{'before ' * 220}</p><p>Orion mode exists.</p><p>{'after ' * 240}Only under condition Z.</p></main>"
    client = fetcher(body)
    result = client.find(URL, "Orion mode")
    hit = result["matches"][0]
    assert hit["context_complete"] is False
    assert hit["snippet_scope"] == "matched_readable_block"
    assert hit["context_start"] < hit["snippet_start"] < hit["context_end"]
    assert "condition Z" not in hit["snippet"]
    recovered = client.open(URL, offset=hit["context_start"], max_chars=20_000,
                            expected_text_sha256=result["text_sha256"])
    assert "Only under condition Z." in recovered["text"]
    with pytest.raises(WebSearchError, match="changed"):
        fetcher(body.replace("condition Z", "condition Y")).open(
            URL, offset=hit["context_start"], expected_text_sha256=result["text_sha256"])


def test_long_casefold_expansion_keeps_entire_match_in_bounded_fragment():
    text = "前🙂" * 600 + "S" * 400 + "尾" * 2000
    client = fetcher(f"<main>{text}</main>")
    hit = client.find(URL, "ß" * 200)["matches"][0]
    assert hit["snippet_start"] <= hit["match_start"] < hit["match_end"] <= hit["snippet_end"]
    assert text[hit["match_start"]:hit["match_end"]] == "S" * 400
    assert len(hit["snippet"]) <= MAX_FIND_CONTEXT_CHARS
    assert hit["context_complete"] is False and hit["snippet_scope"] == "bounded_fragment"


def test_network_observations_are_actual_hops_not_inferred_search_cache_causes():
    requests = []
    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/guide":
            return httpx.Response(302, headers={"Location": "/second"})
        if request.url.path == "/second":
            return httpx.Response(307, headers={"Location": "https://1.1.1.1/final"})
        return httpx.Response(200, headers={"Content-Type": "text/plain", "Age": "120",
                                            "Cache-Control": "public, max-age=300"}, text="Orion available.")
    client = PublicPageFetcher(transport=httpx.MockTransport(respond))
    result = client.find(URL, "Orion")
    facts = result["network_observations"]
    assert requests == [URL, "https://8.8.8.8/second", "https://1.1.1.1/final"]
    assert facts["requested_url"] == URL and result["url"] == requests[-1]
    assert facts["redirect_count"] == 2
    assert [hop["url"] for hop in facts["redirects"]] == requests[:-1]
    assert [hop["target"] for hop in facts["redirects"]] == requests[1:]
    assert facts["response_headers"]["age"] == "120"
    assert facts["cache_origin"] == "not_determined"
    assert facts["scope"] == "this_fetch_only_not_search_provider_diagnostics"
    direct = fetcher("<main>body</main>").open(URL)["network_observations"]
    assert direct["redirect_count"] == 0 and direct["redirects"] == []


@pytest.mark.parametrize("mutation", ["complete_type", "scope", "redirect_count", "cache_cause"])
def test_executor_validates_added_evidence_fields_without_changing_flat_schema(mutation):
    client = fetcher("<main><p>Orion mode.</p><p>Only when enabled.</p></main>")
    tool = WebFindTool(client)
    registry = ToolRegistry()
    registry.register_tool(tool)
    executor = ToolExecutor(registry)
    result = executor.execute(invocation_id="context", tool_name=tool.spec.name,
        tool_input={"url": URL, "query": "Orion"}, context=ToolContext(session_id="isolated"))
    assert result.status == "completed" and executor.validate_output(tool_name=tool.spec.name, result=result) == []
    assert "matches" in tool.spec.output_schema and "text" in WebOpenTool.spec.output_schema
    if mutation == "complete_type":
        result.output["matches"][0]["context_complete"] = "true"
    elif mutation == "scope":
        result.output["matches"][0]["snippet_scope"] = "verified_full_source"
    elif mutation == "redirect_count":
        result.output["network_observations"]["redirect_count"] = True
    else:
        result.output["network_observations"]["cache_origin"] = "proven_search_cache"
    assert executor.validate_output(tool_name=tool.spec.name, result=result)


def test_gate_delivery_does_not_upgrade_source_context_to_visible_semantic_coverage():
    # Five distinct snippets force the ordinary gate; a condition in the middle
    # is in the fetched block but not in the gate's head/tail view.
    html = "<main>" + "".join(f"<p>Anchor{index} " + "a" * 500 + " ONLY UNDER CONDITION "
        + "b" * 400 + "</p><p>separator</p>" for index in range(5)) + "</main>"
    result = fetcher(html).find(URL, "Anchor")
    assert all(hit["context_complete"] for hit in result["matches"])
    raw = ToolResult(invocation_id="evidence", tool_name="renamed.find", status="completed", output=result).model_dump(mode="json")
    original = copy.deepcopy(raw)
    assert tool_result_gate.needs_gate(raw)
    view = tool_result_gate.bounded_preview(raw)
    observation = {"result": view}
    # Artifact reads return sorted JSON, as in the real SQLite serializer.
    persisted = json.loads(json.dumps(raw, sort_keys=True))
    binding = tool_result_gate.bind_context_delivery(
        observation_id="evidence", artifact_id="raw-evidence", content_hash="a" * 64,
        raw_payload=persisted, view_payload=observation,
    )
    receipts = tool_result_gate.summarize_context_delivery(
        prompt_payload={"observations": [observation]}, bindings=binding)
    snippet = next(item for item in receipts if item["path"] == "/output/matches/0/snippet")
    assert snippet["coverage"] == "partial" and snippet["upstream_coverage"] == "unknown"
    assert "ONLY UNDER CONDITION" not in json.dumps(view)
    assert raw == original


def test_casefold_partial_character_is_not_mislabeled_a_literal_match():
    client = fetcher("<main>ß S Straße</main>")
    page = client.open(URL)
    singles = client.find(URL, "s")
    assert singles["total_matches"] == 2
    assert all(page["text"][hit["match_start"]:hit["match_end"]].casefold() == "s"
               for hit in singles["matches"])
    full = client.find(URL, "SS")
    assert full["total_matches"] == 2
    assert all(page["text"][hit["match_start"]:hit["match_end"]].casefold() == "ss"
               for hit in full["matches"])


def test_actual_answer_fit_receives_partial_receipt_not_fetch_completeness(tmp_path):
    from datetime import UTC, datetime

    html = "<main>" + "".join(f"<p>Anchor{index} " + "a" * 500 + " ONLY UNDER CONDITION "
        + "b" * 400 + "</p><p>separator</p>" for index in range(5)) + "</main>"
    tool = WebFindTool(fetcher(html))
    registry = ToolRegistry()
    registry.register_tool(tool)
    loop, provider, manager = make_loop(tmp_path, [response("Read further for omitted conditions.")])
    loop.tool_executor = ToolExecutor(registry)
    store = SqliteAgentRunStore(tmp_path / "source-receipts.sqlite3")
    loop.tool_invocation_store = store
    with scope(manager) as run:
        result = loop.tool_executor.execute(invocation_id="page-conditions", tool_name=tool.spec.name,
            tool_input={"url": URL, "query": "Anchor"}, context=ToolContext(session_id=run.session_id, run_id=run.run_id))
        assert result.status == "completed"
        store.put_artifact(artifact_id="tool_result_page-conditions", run_id=run.run_id,
            kind="tool_result", payload=result.model_dump(mode="json"), summary="bounded page context",
            created_at=datetime.now(UTC).isoformat())
        observation = loop._observation_for_decision_prompt(tool_name=tool.spec.name,
            tool_input={"url": URL, "query": "Anchor"}, tool_result=result,
            feedback={"status": "accepted", "protocol_status": "valid"}, run_id=run.run_id)
        answer = loop._answer_with_llm(user_input="Report conditions from the evidence.", route={},
            context_window={}, observations=[observation], final_decision=None, llm_events=[])
    assert answer == "Read further for omitted conditions." and len(provider.requests) == 1
    payload = json.loads(provider.requests[0].messages[1].content)
    receipt = next(item for item in payload["context_delivery"] if item["path"] == "/output/matches/0/snippet")
    assert receipt["coverage"] == "partial" and receipt["upstream_coverage"] == "unknown"
    # Fake-provider text is not semantic success; these assertions test only
    # the real artifact -> fit -> provider boundary and its coverage contract.
