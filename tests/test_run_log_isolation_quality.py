from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_turn import (
    AgentTurnDecisionEvent,
    AgentTurnLoop,
    AgentTurnResult,
    _turn_run_id,
    _turn_run_manager,
)


def make_loop(tmp_path, manager=None):
    return AgentTurnLoop(
        session_service=None,
        tool_executor=None,
        llm_client=None,
        log_dir=tmp_path / "logs",
        run_manager=manager,
    )


def result_for(run):
    return AgentTurnResult(
        run_id=run.run_id,
        session_id=run.session_id,
        trace_id=run.trace_id,
        answer=f"Evidence delivered by {run.run_id}.",
        decision_events=[
            AgentTurnDecisionEvent(
                step_index=1,
                decided_at="2026-10-05T00:00:00+00:00",
                source="local",
                action="final_answer",
                raw_output=f"Decision evidence for {run.run_id}.",
            )
        ],
    )


def test_parallel_children_and_parent_keep_distinct_logs_with_shared_trace(tmp_path):
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Parent objective.")
    children = [
        manager.create_child_run(
            parent_run_id=parent.run_id,
            plan_id="plan",
            step_id=f"part_{index}",
            attempt=1,
            user_input=f"Independent objective {index}.",
        )
        for index in range(3)
    ]
    loop = make_loop(tmp_path, manager)
    barrier = Barrier(len(children))

    def write_child(child):
        barrier.wait(timeout=5)
        return loop._write_log(result=result_for(child), user_input=child.user_input)

    with ThreadPoolExecutor(max_workers=len(children)) as pool:
        paths = list(pool.map(write_child, children))
    parent_path = loop._write_log(result=result_for(parent), user_input=parent.user_input)

    assert len({parent_path, *paths}) == 4
    assert parent_path.name == f"{parent.trace_id}.md"
    for child, path in zip(children, paths, strict=True):
        assert child.trace_id == parent.trace_id
        assert path.name == f"{child.trace_id}_{child.run_id}.md"
        text = path.read_text(encoding="utf-8")
        assert f"- run_id: `{child.run_id}`" in text
        assert f"- trace_id: `{parent.trace_id}`" in text
        assert child.user_input in text
        assert result_for(child).answer in text
        assert f"Decision evidence for {child.run_id}." in text
        assert parent.user_input not in text
    assert parent.user_input in parent_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("managed", [False, True])
def test_root_log_keeps_legacy_trace_filename(tmp_path, managed):
    manager = InMemoryAgentRunManager()
    root = manager.create_run(session_id="root", user_input="Root objective.")
    loop = make_loop(tmp_path, manager if managed else None)

    path = loop._write_log(result=result_for(root), user_input=root.user_input)

    assert path == loop.log_dir / f"{root.trace_id}.md"
    assert f"- run_id: `{root.run_id}`" in path.read_text(encoding="utf-8")


def test_log_owner_uses_result_run_not_ambient_parent_run(tmp_path):
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Parent objective.")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan",
        step_id="part",
        attempt=1,
        user_input="Child objective.",
    )
    loop = make_loop(tmp_path)
    manager_token = _turn_run_manager.set(manager)
    run_token = _turn_run_id.set(parent.run_id)
    try:
        path = loop._write_log(result=result_for(child), user_input=child.user_input)
    finally:
        _turn_run_id.reset(run_token)
        _turn_run_manager.reset(manager_token)

    assert path.name == f"{child.trace_id}_{child.run_id}.md"
    assert child.user_input in path.read_text(encoding="utf-8")
