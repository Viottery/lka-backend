"""Recoverable child-only control views cannot shrink the answer reservation."""

import copy
import json
from types import SimpleNamespace

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tool_result_gate import preview_text_fields
from app.core.tools import ToolResult
from app.tool_packages.observation import ObservationReadTool
from tests.test_child_budget_quality import child_loop


def configure(loop, child, *, reader_allowed=True):
    artifacts = {}
    reads = []
    def load(identity, owner):
        reads.append((identity, owner))
        return artifacts.get(identity) if owner == child.run_id else None
    store = SimpleNamespace(load_tool_result_artifact=load)
    reader = ObservationReadTool(store)
    loop.tool_invocation_store = store
    loop.tool_executor = SimpleNamespace(registry=SimpleNamespace(
        get_tool_or_none=lambda name: reader if name == reader.spec.name else None))
    view = ToolView(snapshot_id="fixed-view", child_run_id=child.run_id,
        allowed_packages=(reader.spec.package,) if reader_allowed else ("unrelated",),
        side_effect_level=SideEffectLevel.READ)
    loop._tool_view_for_run = lambda _: view
    def observed(identity, *, status="completed", size=2700):
        raw = ToolResult(invocation_id=identity, tool_name="renamed.inspect", status=status,
            output={"text": "否定：尚未批准。" + "x" * size + "末尾：仍缺独立核验。",
                    "missing_requirements": ["independent verification"], "partial": True,
                    **{f"marker{i}": i for i in range(14)}}, execution_started=True)
        artifacts[f"tool_result_{identity}"] = raw.model_dump(mode="json")
        return loop._observation_for_decision_prompt(
            tool_name=raw.tool_name, tool_input={"path": identity}, tool_result=raw,
            feedback={"status": "accepted" if status == "completed" else "failed",
                      "protocol_status": "valid"}, run_id=child.run_id)
    return observed, artifacts, reads


def test_only_older_accepted_child_results_project_without_losing_structural_gaps(tmp_path):
    with child_loop(tmp_path, max_depth=2) as (loop, _, child, _):
        observed, artifacts, _ = configure(loop, child)
        observations = [observed("earlier"), observed("failed", status="failed"),
                        {"action": "fork_subtasks", "replan_required": True,
                         "historical_task_results": [{"status": "partial", "missing_requirements": ["gap"]}]},
                        observed("latest", size=1000)]
        original = copy.deepcopy(observations)
        projected = loop._observations_for_child_control(observations)
        assert observations == original
        assert projected[1:] == original[1:]
        old = projected[0]
        assert old["_result_cache"]["artifact_id"] == "tool_result_earlier"
        assert old["result"]["output"]["text"]["_partial"] is True
        assert old["result"]["output"]["missing_requirements"] == ["independent verification"]
        assert old["result"]["output"]["partial"] is True
        assert old["result"]["output"]["marker13"] == 13
        assert loop._observations_for_answer_prompt(observations) == original
        assert artifacts["tool_result_earlier"]["output"]["text"] == original[0]["result"]["output"]["text"]
        assert len(json.dumps(projected)) < len(json.dumps(original))


@pytest.mark.parametrize("denial", ["scope", "missing", "changed", "history", "reader_missing"])
def test_unrecoverable_or_changed_evidence_is_not_projected(tmp_path, denial):
    with child_loop(tmp_path) as (loop, _, child, _):
        observed, artifacts, _ = configure(loop, child, reader_allowed=denial != "scope")
        observations = [observed("earlier"), observed("latest", size=1000)]
        if denial == "missing":
            artifacts.clear()
        elif denial == "changed":
            artifacts["tool_result_earlier"]["output"]["text"] = "changed version"
        elif denial == "history":
            observations[0]["_cache"] = {"historical_only": True}
        elif denial == "reader_missing":
            loop.tool_executor.registry.get_tool_or_none = lambda _: None
        assert loop._observations_for_child_control(observations) == observations


def test_native_decision_uses_compact_control_but_full_original_answer_estimate(tmp_path):
    with child_loop(tmp_path, max_depth=2) as (loop, _, child, _calls):
        observed, _, _ = configure(loop, child)
        observations = [observed("earlier"), observed("latest", size=1000)]
        loop._supports_function_calling = lambda: True
        captures = []
        loop._complete_control_generation = lambda **kw: (captures.append(kw), None)
        loop._decide_next_action_once(user_input="Inspect evidence.", route={}, context_window={},
            package_catalog=[], expanded_package_names=[], expanded_tools=[], observations=observations,
            llm_events=[])
        # Native returned None so JSON fallback was also captured. Both reserve
        # the original, not the smaller control view, before dispatch.
        assert len(captures) == 2
        for request in captures:
            assert request["observations"] is observations
            assert json.loads(request["user_prompt"])["observations"][0]["_result_cache"]
        fitted = []
        def fit(**kw):
            fitted.append(json.loads(kw["user_prompt"]))
            return SimpleNamespace(input_tokens=123, conservative=False)
        loop._budget_llm_prompt = fit
        loop._child_answer_input_estimate(user_prompt=captures[0]["user_prompt"],
                                         observations=captures[0]["observations"], fallback=999)
        assert fitted[0]["observations"] == observations


def test_control_text_projection_preserves_all_structure_or_fails_closed():
    raw = {"rows": [{"note": "n" * 1500, "negated": True} for _ in range(10)], "missing": ["gap"]}
    result = preview_text_fields(raw)
    assert len(result["rows"]) == 10 and all(r["negated"] for r in result["rows"])
    assert result["missing"] == ["gap"]
    with pytest.raises(ValueError, match="bounded walk"):
        preview_text_fields(list(range(513)))
