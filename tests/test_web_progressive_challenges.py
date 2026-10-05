"""Hard evidence delivery regressions across durable storage and provider fitting."""

import json
from datetime import UTC, datetime

from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import AgentTurnLoop
from app.core.tool_result_gate import bounded_preview, needs_gate
from app.core.tools import ToolExecutor, ToolResult
from app.tool_packages.observation import ObservationReadTool
from tests import test_web_snapshots
from tests.test_answer_generation_recovery_quality import make_loop, response, scope

web = test_web_snapshots.web


def _cached_match(web):
    condition = "Only for explicitly approved users; all other users are excluded."
    # A large real heading directory makes the result enter the generic gate.
    web.state["html"] = (
        "<main>" + "".join(f"<h2>Directory heading {i}</h2>" for i in range(100))
        + "<p>Anchor " + "a" * 520 + " " + condition + " " + "b" * 250 + "</p></main>"
    )
    page = web.call("web.open", {"url": "https://8.8.8.8/guide"})
    result = web.call("web.find", {"snapshot_id": page.output["snapshot_id"], "query": "Anchor"})
    assert result.status == "completed" and condition in result.output["matches"][0]["snippet"]
    raw = result.model_dump(mode="json")
    assert needs_gate(raw)
    store = SqliteAgentRunStore(web.db)
    artifact_id = f"tool_result_{result.invocation_id}"
    store.put_artifact(artifact_id=artifact_id, run_id="r", kind="tool_result", payload=raw,
        summary="hard match case", created_at=datetime.now(UTC).isoformat())
    return store, artifact_id, raw, condition


def test_durable_roundtrip_must_preserve_find_evidence_and_continuation(web):
    store, identity, raw, condition = _cached_match(web)
    direct = bounded_preview(raw, max_string_chars=1200)
    assert condition in json.dumps(direct)  # Isolated tool-return check looks green.
    restored = store.load_tool_result_artifact(identity, "r")
    delivered = bounded_preview(restored, max_string_chars=1200,
        output_priority_fields=web.registry.get_tool("web.find").spec.output_preview_priority_fields)
    # Production Graph persists its pending ToolResult before observation building.
    assert condition in json.dumps(delivered)
    assert raw["output"]["snapshot_id"] in json.dumps(delivered)


def test_durable_find_reaches_both_decision_and_fitted_answer_provider(web, tmp_path):
    store, identity, raw, condition = _cached_match(web)
    restored = ToolResult.model_validate(store.load_tool_result_artifact(identity, "r"))
    loop, provider, manager = make_loop(tmp_path, [response("Only approved users.")])
    loop.tool_executor = ToolExecutor(web.registry)
    store = SqliteAgentRunStore(tmp_path / "answer.sqlite3")
    loop.tool_invocation_store = store
    with scope(manager) as run:
        store.put_artifact(artifact_id=identity, run_id=run.run_id, kind="tool_result", payload=raw,
            summary="durable provider evidence", created_at=datetime.now(UTC).isoformat())
        observed = loop._observation_for_decision_prompt(tool_name=restored.tool_name,
            tool_input={}, tool_result=restored, feedback={"status": "accepted"}, run_id=run.run_id)
        decision_view = loop._observations_within_prompt_budget([observed])
        assert condition in json.dumps(decision_view)
        loop._answer_with_llm(user_input="What conditions apply?", route={}, context_window={},
            observations=[observed], final_decision=None, llm_events=[])
    fitted = json.loads(provider.requests[0].messages[1].content)
    assert condition in json.dumps(fitted)
    assert raw["output"]["snapshot_id"] in json.dumps(fitted)
    receipt = next(row for row in fitted["context_delivery"] if row["path"] == "/output/matches/0/snippet")
    assert receipt["coverage"] == "complete" and len(web.calls) == 1


def test_tool_body_cannot_declare_preview_priority_and_mismatched_tool_cannot_borrow_it(web):
    store, identity, _raw, condition = _cached_match(web)
    loop = object.__new__(AgentTurnLoop)
    loop.tool_executor = ToolExecutor(web.registry)
    loop.tool_invocation_store = store
    restored = store.load_tool_result_artifact(identity, "r")
    restored["output"]["output_preview_priority_fields"] = ["matches", "snapshot_id"]
    result = ToolResult.model_validate(restored)
    denied = loop._observation_for_decision_prompt(tool_name="unknown.tool", tool_input={},
        tool_result=result, feedback={}, run_id="r")
    assert condition not in json.dumps(denied)


def test_projected_record_is_not_full_text_but_exact_string_path_recovers_it(web):
    store, identity, raw, condition = _cached_match(web)
    web.registry.register_tool(ObservationReadTool(store))
    summary = web.call("observation.read", {"artifact_id": identity, "path": "/output/matches",
        "fields": ["snippet", "match_start", "match_end", "context_start", "context_end"]})
    assert summary.status == "completed"
    field = summary.output["items"][0]["fields"]["snippet"]
    assert field["truncated"] is True and condition not in field["preview"]
    # Array pagination ending does not certify that projected strings were read.
    assert summary.output["has_more"] is False
    assert field["read_path"] == "/output/matches/0/snippet"
    assert "records_only" in summary.output["pagination_scope"]
    full = web.call("observation.read", {"artifact_id": identity, "path": field["read_path"]})
    assert full.status == "completed" and condition in full.output["text"]
    assert full.output["text"] == raw["output"]["matches"][0]["snippet"]
    assert len(web.calls) == 1
