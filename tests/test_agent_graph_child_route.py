from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.core.agent_graph import AgentGraphRunner
from app.core.agent_turn import (
    AgentTurnDecisionEvent,
    AgentTurnProgressEvent,
    AgentTurnWorkingSet,
)
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel


class _TurnLoop:
    def __init__(self, *, tool_view: ToolView, catalog: list[dict], route_result: dict):
        self.tool_view = tool_view
        self.catalog = catalog
        self.route_result = route_result
        self.route_calls = 0
        self.catalog_views = []
        self.route_decisions = []

    def _raise_if_cancel_requested(self):
        return None

    def _tool_view_for_run(self, _run_id: str):
        return self.tool_view

    def _package_catalog(self, view):
        self.catalog_views.append(view)
        return self.catalog

    def _route(self, **_kwargs):
        self.route_calls += 1
        return self.route_result

    def _record_route_decision(self, decisions, *, source, route, raw_output):
        self.route_decisions.append((source, route, raw_output))
        decisions.append(
            AgentTurnDecisionEvent(
                step_index=len(decisions) + 1,
                decided_at=datetime.now(UTC).isoformat(),
                source=source,
                action="select_package",
                selected_package=route.get("selected_package"),
                reason=route.get("reason"),
            )
        )

    @staticmethod
    def _append_progress(progress, **values):
        progress.append(
            AgentTurnProgressEvent(
                event_index=len(progress) + 1,
                created_at=datetime.now(UTC).isoformat(),
                **values,
            )
        )


def _invoke_route(*, catalog: list[dict]):
    child_run_id = "child_route_test"
    tool_view = ToolView(
        snapshot_id="snapshot_route_test",
        child_run_id=child_run_id,
        allowed_packages=tuple(item["name"] for item in catalog),
        side_effect_level=SideEffectLevel.READ,
    )
    loop = _TurnLoop(
        tool_view=tool_view,
        catalog=catalog,
        route_result={"selected_package": catalog[-1]["name"], "reason": "LLM route"},
    )
    runner = AgentGraphRunner.__new__(AgentGraphRunner)
    runner.turn_loop = loop
    runner._run = lambda _run_id: SimpleNamespace(
        parent_run_id="parent_route_test",
        metadata={
            "context_snapshot": {
                "child_run_id": child_run_id,
                "snapshot_id": tool_view.snapshot_id,
            }
        },
    )
    ws = AgentTurnWorkingSet(
        run_id=child_run_id,
        session_id="session_route_test",
        trace_id="trace_route_test",
        user_input="Read the assigned material.",
    )
    data = {
        "llm_events": [],
        "decision_events": [],
        "progress_events": [],
        "package_catalog": catalog,
        "context_window": {},
    }
    runner._data = lambda _state: (ws, data)
    runner._save = lambda _state, _ws, saved_data, phase: {
        "working_set": ws.model_dump(mode="json"),
        "data": saved_data,
        "phase": phase,
    }
    return runner._route_package({"run_id": child_run_id}), loop


def test_child_route_skips_llm_for_one_server_authorized_package():
    routed, loop = _invoke_route(catalog=[{"name": "workspace"}])

    assert loop.route_calls == 0
    assert loop.catalog_views == [loop.tool_view]
    assert routed["working_set"]["route"]["selected_package"] == "workspace"
    assert routed["working_set"]["initial_package"] == "workspace"
    assert routed["data"]["llm_events"] == []
    assert loop.route_decisions[0][0] == "local"


def test_child_route_keeps_llm_routing_for_multiple_authorized_packages():
    routed, loop = _invoke_route(catalog=[{"name": "workspace"}, {"name": "knowledge"}])

    assert loop.route_calls == 1
    assert routed["working_set"]["route"]["selected_package"] == "knowledge"
    assert routed["working_set"]["initial_package"] == "knowledge"
