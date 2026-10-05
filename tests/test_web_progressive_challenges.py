"""Hard evidence delivery regressions, including an explicitly known failure."""

import json
from datetime import UTC, datetime

import pytest

from app.core.agent_storage import SqliteAgentRunStore
from app.core.tool_result_gate import bounded_preview, needs_gate
from app.tool_packages.observation import ObservationReadTool
from tests import test_web_snapshots

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


@pytest.mark.xfail(strict=True, reason="Known gap: durable canonical JSON ordering hides web matches/snapshot in gate")
def test_durable_roundtrip_must_preserve_find_evidence_and_continuation(web):
    store, identity, raw, condition = _cached_match(web)
    direct = bounded_preview(raw, max_string_chars=1200)
    assert condition in json.dumps(direct)  # Isolated tool-return check looks green.
    restored = store.load_tool_result_artifact(identity, "r")
    delivered = bounded_preview(restored, max_string_chars=1200)
    # Production Graph persists its pending ToolResult before observation building.
    # This desired assertion currently fails; remove xfail when fixing the gap.
    assert condition in json.dumps(delivered)
    assert raw["output"]["snapshot_id"] in json.dumps(delivered)


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
    full = web.call("observation.read", {"artifact_id": identity, "path": "/output/matches/0/snippet"})
    assert full.status == "completed" and condition in full.output["text"]
    assert full.output["text"] == raw["output"]["matches"][0]["snippet"]
    assert len(web.calls) == 1
