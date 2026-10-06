"""R23-V red contracts; production is intentionally frozen in this phase.

Proposed API in the existing tool_result_gate module:
    summarize_context_delivery(*, prompt_payload, bindings) -> list[dict]
Bindings are ephemeral, SERVER-produced projection metadata, not tool JSON.
They identify an observation by result.invocation_id (not its shifting list
index), an artifact version/JSON Pointer, and exact projected text fragments.
Fragment view_path is relative to the observation; start/end index the cached
value in Unicode code points. A changed fragment without a fresh projection
mapping is UNKNOWN, never reconstructed by parsing truncation-marker prose.

ObservationRead/Search output._delivery_view is a list of cached-value origin
descriptors with the same summary fields. It is descriptive, not authority for
the summarizer: only server bindings plus the FINAL fitted payload count.
No descriptor below claims an upstream document was fetched/read in full.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from app.core import tool_result_gate
from app.core.agent_runs import AgentRunCancelled
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import AgentTurnLoop
from app.core.context_driver import ToolView
from app.core.prompt_budget import PromptBudgeter, serialize_prompt_payload
from app.core.tools import ToolContext, ToolInvocation, ToolResult
from app.tool_packages.observation import ObservationReadTool, ObservationSearchTool


def _hash(payload):
    # Match SqliteAgentRunStore's existing artifact serialization, not text SHA.
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _store(tmp_path, payload):
    store = SqliteAgentRunStore(tmp_path / "delivery.sqlite3")
    artifact = store.put_artifact(
        artifact_id="cached-page", run_id="delivery-run", kind="tool_result",
        payload=payload, summary="cached value, not an upstream completeness claim",
        created_at=datetime.now(UTC).isoformat(),
    )
    assert artifact["content_hash"] == _hash(payload)
    return store, artifact


def _invoke(tool, **inputs):
    return tool.invoke(
        invocation=ToolInvocation(
            invocation_id="read-window", tool=tool.spec,
            session_id="delivery-session", context_id="delivery-context", input=inputs,
        ),
        context=ToolContext(session_id="delivery-session", run_id="delivery-run"),
    )


def _expected(artifact_id, content_hash, path, total_chars, ranges, coverage):
    return {
        "scope": "cached_value", "artifact_id": artifact_id,
        "content_hash": content_hash, "path": path, "unit": "unicode_codepoints",
        "total_chars": total_chars, "ranges": ranges,
        "covered_chars": sum(end - start for start, end in ranges),
        "coverage": coverage, "upstream_coverage": "unknown",
    }


def _observation(identity, output, *, artifact_id="cached-page"):
    return {
        "result": {"invocation_id": identity, "output": output},
        "_result_cache": {"artifact_id": artifact_id},
    }


def _fragment(view_path, value, start, end):
    return {"view_path": view_path, "start": start, "end": end,
            "text": value[start:end]}


def _binding(identity, value, fragments, *, artifact_id="cached-page",
             content_hash=None, path="/output/text"):
    return {
        "observation_id": identity, "artifact_id": artifact_id,
        "content_hash": content_hash or _hash({"output": {"text": value}}),
        "path": path, "total_chars": len(value), "fragments": fragments,
    }


def _summary(payload, bindings):
    summarize = getattr(tool_result_gate, "summarize_context_delivery", None)
    assert callable(summarize), "R23-V missing final-payload cached-value delivery summarizer"
    # API must be pure and must not mutate caller-owned observations/bindings.
    before = serialize_prompt_payload({"payload": payload, "bindings": bindings})
    result = summarize(prompt_payload=payload, bindings=bindings)
    assert serialize_prompt_payload({"payload": payload, "bindings": bindings}) == before
    assert isinstance(result, list)
    return result


def test_read_page_reports_exact_unicode_cache_origin_and_interval(tmp_path):
    text = "甲🙂e\u0301ß" * 1600
    path = "/output/a~1b~0c"
    payload = {"output": {"a/b~c": text}}
    store, artifact = _store(tmp_path, payload)
    result = _invoke(ObservationReadTool(store), artifact_id="cached-page",
                     path=path, offset=7, limit=1, max_chars=4000)
    assert result.status == "completed"
    assert result.output["text"] == text[7:4007]
    assert result.output["next_offset"] == 4007
    assert result.output["_delivery_view"] == [
        _expected("cached-page", artifact["content_hash"], path, len(text),
                  [[7, 4007]], "partial")]


def test_complete_cached_value_does_not_claim_upstream_document_complete(tmp_path):
    text = "Only the cached page was returned."
    payload = {"output": {"text": text, "offset": 9000, "total_chars": 24000,
                          "has_more": True}}
    store, artifact = _store(tmp_path, payload)
    result = _invoke(ObservationReadTool(store), artifact_id="cached-page",
                     path="/output/text", offset=0)
    assert result.output["has_more"] is False  # Exhausts this cached value only.
    assert result.output["_delivery_view"] == [
        _expected("cached-page", artifact["content_hash"], "/output/text", len(text),
                  [[0, len(text)]], "complete")]


def test_search_scan_complete_reports_only_delivered_original_snippet(tmp_path):
    text = "甲🙂" * 900 + "Straße" + "尾" * 1800
    store, artifact = _store(tmp_path, {"output": {"text": text}})
    result = _invoke(ObservationSearchTool(store), artifact_id="cached-page",
                     path="/output/text", query="STRASSE", limit=10)
    assert result.output["complete"] is True
    match, = result.output["matches"]
    start, end = match["snippet_start"], match["snippet_end"]
    assert match["snippet"] == text[start:end]
    assert text[match["match_start"]:match["match_end"]] == "Straße"
    assert result.output["_delivery_view"] == [
        _expected("cached-page", artifact["content_hash"], "/output/text", len(text),
                  [[start, end]], "partial")]


def test_no_match_complete_scan_delivers_zero_cached_text(tmp_path):
    text = "未命中的原文" * 900
    store, artifact = _store(tmp_path, {"output": {"text": text}})
    result = _invoke(ObservationSearchTool(store), artifact_id="cached-page",
                     path="/output/text", query="absent literal")
    assert result.output["complete"] is True and result.output["matches"] == []
    assert result.output["_delivery_view"] == [
        _expected("cached-page", artifact["content_hash"], "/output/text", len(text),
                  [], "partial")]


def test_gate_head_tail_maps_to_exact_cached_ranges_not_the_gap():
    text = "甲🙂" * 600
    view = tool_result_gate.preview(text, path="/output/text")
    assert view["head_range"] == [0, 350] and view["tail_range"] == [1050, 1200]
    assert view["_partial"] is True
    binding = _binding("gate", text, [
        _fragment("/result/output/head", text, 0, 350),
        _fragment("/result/output/tail", text, 1050, 1200),
    ])
    result = _summary({"observations": [_observation("gate", view)]}, [binding])
    assert result == [_expected("cached-page", binding["content_hash"], "/output/text",
                                len(text), [[0, 350], [1050, 1200]], "partial")]


def test_adjacent_windows_complete_only_the_same_cached_value():
    text = "甲🙂ße\u0301" * 120
    split = 317
    bindings = [_binding(identity, text, [_fragment("/result/output/text", text, start, end)])
                for identity, start, end in (("first", 0, split), ("second", split, len(text)))]
    payload = {"observations": [_observation("first", {"text": text[:split]}),
                                _observation("second", {"text": text[split:]})]}
    assert _summary(payload, bindings) == [
        _expected("cached-page", bindings[0]["content_hash"], "/output/text", len(text),
                  [[0, len(text)]], "complete")]


def test_repeated_overlapping_windows_do_not_sum_into_false_completeness():
    text = "abcdefghij"
    windows = (("one", 0, 6), ("repeat", 0, 6), ("overlap", 3, 8))
    bindings = [_binding(identity, text, [_fragment("/result/output/text", text, start, end)])
                for identity, start, end in windows]
    payload = {"observations": [_observation(identity, {"text": text[start:end]})
                                for identity, start, end in windows]}
    assert _summary(payload, bindings) == [
        _expected("cached-page", bindings[0]["content_hash"], "/output/text", len(text),
                  [[0, 8]], "partial")]


def test_artifact_versions_and_distinct_cached_paths_never_merge():
    old, new = "abcde12345", "ABCDE67890"
    old_hash = _hash({"output": {"text": old, "other": old}})
    new_hash = _hash({"output": {"text": new, "other": old}})
    bindings = [
        _binding("old", old, [_fragment("/result/output/text", old, 0, 5)],
                 content_hash=old_hash),
        _binding("new", new, [_fragment("/result/output/text", new, 5, 10)],
                 content_hash=new_hash),
        _binding("other-path", old, [_fragment("/result/output/text", old, 5, 10)],
                 content_hash=old_hash, path="/output/other"),
    ]
    payload = {"observations": [_observation("old", {"text": old[:5]}),
                                _observation("new", {"text": new[5:]}),
                                _observation("other-path", {"text": old[5:]})]}
    result = _summary(payload, bindings)
    assert len(result) == 3
    assert {(item["content_hash"], item["path"]) for item in result} == {
        (binding["content_hash"], binding["path"]) for binding in bindings}
    assert all(item["coverage"] == "partial" and item["covered_chars"] == 5 for item in result)


@dataclass
class _Count:
    count: int
    method: str = "test_chars"
    conservative: bool = True


class _CharCounter:
    def count_request(self, system_prompt, user_prompt, tools=None):
        return _Count(len(system_prompt) + len(user_prompt))


def test_prompt_budget_eviction_and_index_shift_use_only_final_payload():
    text, recent = "旧" * 800, "kept"
    old_binding = _binding("old", text, [_fragment("/result/output/text", text, 0, len(text))])
    new_binding = _binding("new", recent, [_fragment("/result/output/text", recent, 0, len(recent))],
                           artifact_id="recent-page")
    fitted = PromptBudgeter(_CharCounter()).fit(
        system_prompt="", user_prompt=serialize_prompt_payload({
            "user_input": "answer", "observations": [
                _observation("old", {"text": text}),
                _observation("new", {"text": recent}, artifact_id="recent-page")]}),
        input_limit=500,
    )
    payload = json.loads(fitted.user_prompt)
    assert fitted.omitted["observation_artifact_ids"] == ["cached-page"]
    assert payload["observations"][0]["result"]["invocation_id"] == "new"
    result = _summary(payload, [old_binding, new_binding])
    by_artifact = {item["artifact_id"]: item for item in result}
    assert by_artifact["cached-page"] == _expected(
        "cached-page", old_binding["content_hash"], "/output/text", len(text), [], "partial")
    assert by_artifact["recent-page"] == _expected(
        "recent-page", new_binding["content_hash"], "/output/text", len(recent),
        [[0, len(recent)]], "complete")


def test_more_than_24_fitted_observations_keep_older_bound_text_visible():
    text = "older retained original"
    binding = _binding("older", text, [_fragment("/result/output/text", text, 0, len(text))])
    observations = [_observation("older", {"text": text})] + [
        _observation(f"recent-{index}", {"text": "unbound recent text"}) for index in range(39)]
    assert len(observations) == 40
    assert _summary({"observations": observations}, [binding]) == [
        _expected("cached-page", binding["content_hash"], "/output/text", len(text),
                  [[0, len(text)]], "complete")]


def test_far_duplicate_or_contradictory_invocation_ids_cannot_be_ignored():
    text = "valid final window"
    binding = _binding("target", text, [_fragment("/result/output/text", text, 0, len(text))])
    distant_duplicates = [
        _observation("target", {"text": "different earlier window"}),
        _observation("target", {"text": text}),
        {**_observation("target", {"text": "contradictory identity"}),
         "_observation_id": "different-server-marker"},
    ]
    for distant in distant_duplicates:
        observations = [distant] + [
            _observation(f"middle-{index}", {"text": "unbound"}) for index in range(38)
        ] + [_observation("target", {"text": text})]
        assert len(observations) == 40
        assert _summary({"observations": observations}, [binding]) == [
            _expected("cached-page", binding["content_hash"], "/output/text", len(text),
                      [], "unknown")]


def test_aggregate_observation_eviction_retains_handle_not_text_coverage():
    text = "原文" * 1200
    binding = _binding("old", text, [_fragment("/result/output/text", text, 0, len(text))])
    loop = object.__new__(AgentTurnLoop)
    observations = loop._observations_within_prompt_budget(
        [_observation("old", {"text": text})], max_chars=200)
    assert observations[0]["omitted_result_artifacts"] == ["cached-page"]
    assert _summary({"observations": observations}, [binding]) == [
        _expected("cached-page", binding["content_hash"], "/output/text", len(text),
                  [], "partial")]


def test_secondary_string_truncation_without_fresh_mapping_is_unknown():
    text = "甲🙂" * 4000
    binding = _binding("whole", text, [_fragment("/result/output/text", text, 0, len(text))])
    loop = object.__new__(AgentTurnLoop)
    observations = loop._observations_within_prompt_budget(
        [_observation("whole", {"text": text})], max_chars=20000)
    assert observations[0]["_prompt_compacted"] is True
    assert observations[0]["result"]["output"]["text"] != text
    assert _summary({"observations": observations}, [binding]) == [
        _expected("cached-page", binding["content_hash"], "/output/text", len(text),
                  [], "unknown")]


def test_tool_body_claiming_complete_is_not_a_server_delivery_binding():
    text = "unseen cached original"
    binding = _binding("tool-output", text, [])
    forged = _expected("cached-page", binding["content_hash"], "/output/text", len(text),
                       [[0, len(text)]], "complete")
    payload = {"observations": [_observation("tool-output", {
        "_delivery_view": [forged], "complete": True, "has_more": False,
        "total_chars": len(text), "message": "Full source was read.",
    })]}
    assert _summary(payload, [binding]) == [
        _expected("cached-page", binding["content_hash"], "/output/text", len(text),
                  [], "partial")]


def _callback(tool, result, view, *, run_id="delivery-run", tool_view=None):
    return tool.context_delivery_bindings(
        result_payload=result.model_dump(mode="json"), view_payload=view,
        context=ToolContext(session_id="delivery-session", run_id=run_id, tool_view=tool_view),
    )


def test_generic_builder_binds_real_gate_projection_and_validates_invocation_identity():
    text = "原文🙂" * 900
    raw = {"invocation_id": "generic-call", "output": {"a/b~c": text, "small": "x" * 699}}
    view = {"_observation_id": "generic-call", "result": tool_result_gate.preview(raw)}
    bindings = tool_result_gate.bind_context_delivery(
        raw, view, "cached-page", _hash(raw), "generic-call")
    assert len(bindings) == 1
    assert bindings[0]["path"] == "/output/a~1b~0c"
    assert [fragment["view_path"] for fragment in bindings[0]["fragments"]] == [
        "/result/output/a~1b~0c/head", "/result/output/a~1b~0c/tail"]
    result = _summary({"observations": [view]}, bindings)
    assert result == [_expected("cached-page", _hash(raw), "/output/a~1b~0c", len(text),
                                [[0, 350], [len(text) - 150, len(text)]], "partial")]
    assert tool_result_gate.bind_context_delivery(raw, view, "cached-page", _hash(raw), "other-call") == []


def test_generic_builder_rejects_forged_head_tail_and_does_not_parse_markers():
    text = "甲🙂" * 600
    raw = {"invocation_id": "generic-call", "output": {"text": text}}
    view = {"result": tool_result_gate.preview(raw)}
    view["result"]["output"]["text"]["head_range"] = [0, len(text)]
    bindings = tool_result_gate.bind_context_delivery(raw, view, "cached-page", _hash(raw), "generic-call")
    assert bindings[0]["fragments"] == [] and bindings[0]["projection_unknown"] is True
    assert _summary({"observations": [view]}, bindings)[0]["coverage"] == "unknown"


def test_registered_reader_callback_maps_gated_page_back_to_original_cache(tmp_path):
    text = "原文🙂e\u0301" * 2000
    store, artifact = _store(tmp_path, {"output": {"text": text}})
    tool = ObservationReadTool(store)
    result = _invoke(tool, artifact_id="cached-page", path="/output/text", offset=7, max_chars=4000)
    view = {"result": tool_result_gate.preview(result.model_dump(mode="json"))}
    bindings = _callback(tool, result, view)
    assert len(bindings) == 1
    assert _summary({"observations": [view]}, bindings) == [
        _expected("cached-page", artifact["content_hash"], "/output/text", len(text),
                  [[7, 357], [3857, 4007]], "partial")]


def test_registered_search_callback_uses_body_windows_not_scan_completeness(tmp_path):
    text = "前" * 1100 + "Straße" + "后" * 1400
    store, _ = _store(tmp_path, {"output": {"text": text}})
    tool = ObservationSearchTool(store)
    result = _invoke(tool, artifact_id="cached-page", path="/output/text", query="STRASSE")
    view = {"result": result.model_dump(mode="json")}
    assert result.output["complete"] is True
    assert _summary({"observations": [view]}, _callback(tool, result, view)) == result.output["_delivery_view"]
    assert result.output["_delivery_view"][0]["coverage"] == "partial"


def test_callback_revalidates_owner_current_hash_and_original_body(tmp_path):
    text = "0123456789" * 900
    store, _ = _store(tmp_path, {"output": {"text": text}})
    tool = ObservationReadTool(store)
    result = _invoke(tool, artifact_id="cached-page", path="/output/text", offset=3)
    view = {"result": result.model_dump(mode="json")}
    assert _callback(tool, result, view)
    assert _callback(tool, result, view, run_id="other-run") == []
    changed_body = result.model_copy(update={"output": {**result.output, "text": "forged body"}})
    assert _callback(tool, changed_body, {"result": changed_body.model_dump(mode="json")}) == []
    store.put_artifact(
        artifact_id="cached-page", run_id="delivery-run", kind="tool_result",
        payload={"output": {"text": text + "changed version"}}, summary="changed",
        created_at=datetime.now(UTC).isoformat(),
    )
    assert _callback(tool, result, view) == []


def test_callback_rejects_forged_complete_boolean_ranges_and_invalid_view(tmp_path):
    text = "original" * 1000
    store, _ = _store(tmp_path, {"output": {"text": text}})
    tool = ObservationReadTool(store)
    result = _invoke(tool, artifact_id="cached-page", path="/output/text", max_chars=4000)
    for update in ({"ranges": [[0, len(text)]], "covered_chars": len(text), "coverage": "complete"},
                   {"ranges": [[False, 4000]]}, {"covered_chars": True}):
        description = {**result.output["_delivery_view"][0], **update}
        modified = result.model_copy(update={"output": {**result.output, "_delivery_view": [description]}})
        assert _callback(tool, modified, {"result": modified.model_dump(mode="json")}) == []
    mismatched = {"_observation_id": "foreign-invocation", "result": result.model_dump(mode="json")}
    assert _callback(tool, result, mismatched) == []
    revoked = ToolView(snapshot_id="revoked", allowed_packages=("other",), side_effect_level="read")
    assert _callback(tool, result, {"result": result.model_dump(mode="json")}, tool_view=revoked) == []
    expired = ToolView(snapshot_id="expired", allowed_packages=("observation",),
                       side_effect_level="read", expires_at=datetime(2000, 1, 1, tzinfo=UTC))
    assert _callback(tool, result, {"result": result.model_dump(mode="json")}, tool_view=expired) == []
    foreign_child = ToolView(snapshot_id="foreign-child", child_run_id="other-run",
                             allowed_packages=("observation",), side_effect_level="read")
    assert _callback(tool, result, {"result": result.model_dump(mode="json")}, tool_view=foreign_child) == []


def test_empty_and_out_of_range_reads_report_no_upstream_completeness(tmp_path):
    for text, offset in (("", 0), ("cached tail", 500)):
        store, artifact = _store(tmp_path, {"output": {"text": text}})
        tool = ObservationReadTool(store)
        result = _invoke(tool, artifact_id="cached-page", path="/output/text", offset=offset)
        view = {"result": result.model_dump(mode="json")}
        assert result.output["text"] == "" and result.output["has_more"] is False
        assert _summary({"observations": [view]}, _callback(tool, result, view)) == [
            _expected("cached-page", artifact["content_hash"], "/output/text", len(text), [],
                      "complete" if not text else "partial")]


def test_generic_builder_walk_and_metadata_are_bounded_without_mutating_inputs():
    enumerated = []

    class Counted(dict):
        def items(self):
            for key, value in super().items():
                enumerated.append(key)
                yield key, value

    wide = Counted({f"field-{index}": "x" * 800 for index in range(1000)})
    raw = {"invocation_id": "generic-call", "output": wide}
    view = {"result": {"invocation_id": "generic-call", "output": {"field-0": "x" * 800}}}
    before = json.dumps(view)
    bindings = tool_result_gate.bind_context_delivery(raw, view, "cached-page", "a" * 64, "generic-call")
    assert len(enumerated) <= 24 and len(bindings) <= 24
    assert json.dumps(view) == before
    summary = _summary({"observations": [view]}, bindings)
    assert len(summary) <= 24 and all(item["upstream_coverage"] == "unknown" for item in summary)
    assert sum(item["coverage"] == "complete" for item in summary) == 1


def test_source_load_cancellation_stops_later_cache_io_and_returns_no_receipt():
    text = "original source text"
    payload = {"output": {"text": text}}
    loaded = []
    cancelled = False

    class CancellingStore:
        def load_tool_result_artifact(self, artifact_id, run_id):
            nonlocal cancelled
            assert run_id == "delivery-run"
            loaded.append(artifact_id)
            cancelled = True  # A real cancellation becomes visible during first I/O.
            return payload

    def check_cancel():
        if cancelled:
            raise AgentRunCancelled("cancelled during optional source load")

    tool = ObservationSearchTool(CancellingStore())
    result = ToolResult(
        invocation_id="search-cancel", tool_name=tool.spec.name, status="completed",
        output={"matches": [{"path": "/output/text", "snippet_start": 0,
                             "snippet_end": len(text), "snippet": text}],
                "_delivery_view": [_expected(artifact_id, _hash(payload), "/output/text",
                                             len(text), [[0, len(text)]], "complete")
                                   for artifact_id in ("source-one", "source-two")]},
    )
    callback = tool.context_delivery_bindings
    # Exactly the caller's compatibility contract: guards around old callbacks,
    # and the optional hook only when the registered signature supports it.
    kwargs = {"check_cancel": check_cancel} if "check_cancel" in inspect.signature(callback).parameters else {}
    receipt = None
    with pytest.raises(AgentRunCancelled, match="optional source load"):
        check_cancel()
        receipt = callback(
            result_payload=result.model_dump(mode="json"),
            view_payload={"result": result.model_dump(mode="json")},
            context=ToolContext(session_id="delivery-session", run_id="delivery-run"),
            **kwargs,
        )
        check_cancel()
    assert loaded == ["source-one"]
    assert receipt is None
