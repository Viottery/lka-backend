from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.agent_storage import SqliteAgentRunStore
from app.core.context_driver import ToolView
from app.core.tools import ToolContext, ToolInvocation
from app.tool_packages.observation import ObservationSearchTool


def _store(tmp_path, payload=None):
    store = SqliteAgentRunStore(tmp_path / "agent.sqlite3")
    now = datetime.now(UTC).isoformat()
    store.put_artifact(
        artifact_id="large-page", run_id="run-1", kind="tool_result",
        payload=payload if payload is not None else {
            "output": {"text": "before " + "x" * 7000 + " NEEDLE " + "y" * 7000 + " after"}
        },
        summary="cached page", created_at=now,
    )
    store.put_artifact(
        artifact_id="foreign", run_id="run-2", kind="tool_result",
        payload={"output": {"secret": "needle"}}, summary="foreign", created_at=now,
    )
    return store


def _invoke(tool, *, run_id="run-1", tool_view=None, **tool_input):
    return tool.invoke(
        invocation=ToolInvocation(
            invocation_id="search-call", tool=tool.spec, session_id="session-1",
            context_id="context-1", input=tool_input,
        ),
        context=ToolContext(session_id="session-1", run_id=run_id, tool_view=tool_view),
    )


def test_search_finds_exact_text_in_large_cached_page_and_observation_read_can_retrieve(tmp_path):
    tool = ObservationSearchTool(_store(tmp_path))
    result = _invoke(tool, artifact_id="large-page", query="needle")
    assert result.status == "completed"
    match = result.output["matches"][0]
    assert match["path"] == "/output/text"
    assert match["snippet"][match["match_start"] - match["snippet_start"]:
                            match["match_end"] - match["snippet_start"]].lower() == "needle"
    from app.tool_packages.observation import ObservationReadTool
    read = _invoke(ObservationReadTool(tool.store), artifact_id="large-page",
                   path=match["path"], offset=match["match_start"], limit=1)
    assert read.output["text"].lower().startswith("needle")
    assert len(json.dumps(result.output, ensure_ascii=False)) < 6000


def test_distinct_contexts_do_not_spend_page_on_repeated_navigation_anchors(tmp_path):
    # Cached plain text often repeats a term in labels and their adjacent URLs.
    navigation = "anchor index https://example.invalid/#anchor " * 12
    near = "anchor settled choice: optional deployment, not the default."
    far = "anchor mechanism: incompatible components enable the compatibility lock."
    text = navigation + "x" * 700 + near + "y" * 700 + far
    tool = ObservationSearchTool(_store(tmp_path, {"output": {"text": text}}))
    ordinary = _invoke(tool, artifact_id="large-page", query="anchor", limit=5)
    assert not any("settled choice" in hit["snippet"] for hit in ordinary.output["matches"])
    diverse = _invoke(tool, artifact_id="large-page", query="anchor", limit=5,
                      distinct_contexts=True)
    snippets = [hit["snippet"] for hit in diverse.output["matches"]]
    assert any(near in snippet for snippet in snippets)
    assert any(far in snippet for snippet in snippets)
    assert diverse.output["distinct_contexts"] is True
    assert diverse.output["complete"] is True
    from app.tool_packages.observation import ObservationReadTool
    hit = diverse.output["matches"][-1]
    read = _invoke(ObservationReadTool(tool.store), artifact_id="large-page",
                   path=hit["path"], offset=hit["snippet_start"])
    assert far in read.output["text"]


def test_distinct_context_paging_and_output_bounds_remain_explicit(tmp_path):
    text = ("anchor anchor " + "x" * 800) * 14
    tool = ObservationSearchTool(_store(tmp_path, {"output": {"text": text}}))
    first = _invoke(tool, artifact_id="large-page", query="anchor", limit=5,
                    distinct_contexts=True)
    second = _invoke(tool, artifact_id="large-page", query="anchor", limit=5,
                     offset=first.output["next_offset"], distinct_contexts=True)
    assert first.output["has_more"] is True and first.output["complete"] is False
    assert second.output["matches"][0]["match_start"] > first.output["matches"][-1]["match_start"]
    assert len(json.dumps(second.output, ensure_ascii=False)) <= 5500
    assert _invoke(tool, artifact_id="large-page", query="anchor",
                   distinct_contexts="yes").status == "rejected"


def test_distinct_context_keeps_boundary_hit_followed_by_new_body_evidence(tmp_path):
    text = "anchor " + "x" * 470 + "anchor mechanism is explicitly documented here." + "y" * 600
    tool = ObservationSearchTool(_store(tmp_path, {"output": {"text": text}}))
    result = _invoke(tool, artifact_id="large-page", query="anchor", distinct_contexts=True)
    assert len(result.output["matches"]) == 2
    assert "mechanism is explicitly documented here" in result.output["matches"][1]["snippet"]


def test_distinct_context_budget_pages_full_windows_without_losing_suppressed_neighbor_facts(tmp_path):
    rows = {}
    for i in range(10):
        # Second hit and its fact are inside the original 500-char window,
        # but outside the shorter window produced by the legacy budget shrink.
        rows[f"long-{i}-" + "k" * 180] = (
            "x" * 250 + "anchor" + "y" * 74 + "anchor" + "y" * 104 + f"FACT-{i}" + "z" * 54
        )
    tool = ObservationSearchTool(_store(tmp_path, {"output": rows}))
    offset = 0
    visible_facts = set()
    pages = 0
    while True:
        page = _invoke(tool, artifact_id="large-page", query="anchor", limit=10,
                       offset=offset, distinct_contexts=True).output
        pages += 1
        assert len(json.dumps(page, ensure_ascii=False)) <= 5500
        for hit in page["matches"]:
            source = rows[hit["path"].split("/")[-1]]
            assert hit["snippet"] == source[hit["snippet_start"]:hit["snippet_end"]]
            assert len(hit["snippet"]) == 500
            visible_facts.update(i for i in range(10) if f"FACT-{i}" in hit["snippet"])
        if page["has_more"] is False:
            assert page["complete"] is True and page["next_offset"] is None
            break
        assert page["has_more"] is True and page["complete"] is False
        assert page["next_offset"] == offset + len(page["matches"])
        offset = page["next_offset"]
        assert pages <= 10
    assert pages > 1 and visible_facts == set(range(10))


def test_legacy_default_search_still_shrinks_dense_output(tmp_path):
    rows = {f"long-{i}-" + "k" * 180: "x" * 250 + "anchor" + "y" * 250 for i in range(10)}
    tool = ObservationSearchTool(_store(tmp_path, {"output": rows}))
    default = _invoke(tool, artifact_id="large-page", query="anchor", limit=10).output
    explicit = _invoke(tool, artifact_id="large-page", query="anchor", limit=10,
                       distinct_contexts=False).output
    assert default == explicit
    assert len(default["matches"]) == 10
    assert all(len(hit["snippet"]) < 500 for hit in default["matches"])


def test_search_no_match_and_nested_json_pointer_escaping(tmp_path):
    tool = ObservationSearchTool(_store(tmp_path, {
        "output": {"a/b~c": [{"body": "prefix TARGET suffix and a.* literal"}]}
    }))
    result = _invoke(tool, artifact_id="large-page", query="target")
    assert result.output["matches"][0]["path"] == "/output/a~1b~0c/0/body"
    assert result.output["matches"][0]["match_start"] == 7
    literal = _invoke(tool, artifact_id="large-page", query="a.*")
    assert len(literal.output["matches"]) == 1
    assert literal.output["matches"][0]["snippet"][
        literal.output["matches"][0]["match_start"] - literal.output["matches"][0]["snippet_start"]:
        literal.output["matches"][0]["match_end"] - literal.output["matches"][0]["snippet_start"]
    ] == "a.*"
    assert _invoke(tool, artifact_id="large-page", query="absent").output["matches"] == []


def test_search_match_pagination_and_scan_budget_are_explicit(tmp_path):
    tool = ObservationSearchTool(_store(tmp_path, {
        "output": {"a": "hit hit hit", "b": "hit " + "x" * 300_000 + " hit"}
    }))
    page = _invoke(tool, artifact_id="large-page", query="hit", limit=2)
    assert len(page.output["matches"]) == 2
    assert page.output["has_more"] is True
    assert page.output["complete"] is False
    bounded = _invoke(tool, artifact_id="large-page", query="hit", limit=10, offset=0)
    assert bounded.output["scan_limited"] is True
    assert bounded.output["complete"] is False
    assert bounded.output["has_more"] is None
    next_page = _invoke(tool, artifact_id="large-page", query="hit", limit=2, offset=2)
    assert next_page.output["matches"][0]["match_start"] > page.output["matches"][-1]["match_start"]


def test_search_output_stays_below_observation_gate_for_many_long_paths(tmp_path):
    long_key = "k" * 170
    tool = ObservationSearchTool(_store(tmp_path, {
        "output": {long_key: "hit " * 100}
    }))
    result = _invoke(tool, artifact_id="large-page", query="hit", limit=10)
    assert len(json.dumps(result.output, ensure_ascii=False)) < 6000
    assert result.output["has_more"] is True


def test_search_bounds_queued_nodes_for_wide_nested_objects():
    enumerated = [0]

    class CountingDict(dict):
        def items(self):
            for key, value in super().items():
                enumerated[0] += 1
                yield key, value

    class GeneratedLeaves(dict):
        def __len__(self):
            return 10_000

        def items(self):
            for leaf in range(10_000):
                enumerated[0] += 1
                yield f"leaf-{leaf}", "value"

    wide = CountingDict({f"branch-{branch}": GeneratedLeaves() for branch in range(10_000)})

    class Store:
        def load_tool_result_artifact(self, artifact_id, run_id):
            if artifact_id == "wide" and run_id == "run-1":
                return {"output": wide}
            return None

    result = _invoke(ObservationSearchTool(Store()), artifact_id="wide", query="absent")
    assert result.status == "completed"
    assert result.output["nodes_scanned"] <= 10_000
    assert result.output["scan_limited"] is True
    assert result.output["complete"] is False
    assert enumerated[0] < 15_000


def test_search_enforces_current_run_and_child_view_scope(tmp_path):
    tool = ObservationSearchTool(_store(tmp_path))
    assert _invoke(tool, run_id="run-2", artifact_id="large-page", query="needle").status == "rejected"
    assert _invoke(tool, artifact_id="foreign", query="needle").status == "rejected"
    mismatched = ToolView(snapshot_id="snapshot", child_run_id="run-2", side_effect_level="read")
    assert _invoke(tool, artifact_id="large-page", query="needle", tool_view=mismatched).status == "rejected"
    inherited = ToolView(snapshot_id="snapshot", child_run_id=None, side_effect_level="read")
    assert _invoke(tool, artifact_id="foreign", query="needle", tool_view=inherited).status == "rejected"


def test_search_rejects_invalid_query_bounds_path_and_limit(tmp_path):
    tool = ObservationSearchTool(_store(tmp_path))
    for args in (
        {"artifact_id": "large-page"},
        {"artifact_id": "large-page", "query": ""},
        {"artifact_id": "large-page", "query": "x" * 201},
        {"artifact_id": "large-page", "query": 3},
        {"artifact_id": "large-page", "query": "x", "path": 3},
        {"artifact_id": "large-page", "query": "x", "path": "/bad~2pointer"},
        {"artifact_id": "large-page", "query": "x", "limit": 0},
        {"artifact_id": "large-page", "query": "x", "limit": 11},
        {"artifact_id": "large-page", "query": "x", "limit": True},
        {"artifact_id": "large-page", "query": "x", "offset": -1},
        {"artifact_id": "large-page", "query": "x", "offset": True},
    ):
        assert _invoke(tool, **args).status == "rejected"
