import asyncio
from types import SimpleNamespace

import pytest

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_turn import AgentTurnDecisionEvent, AgentTurnResult
from app.core.child_agent import ChildAgentExecutor
from app.core.context_driver import ContextDriver, ContextRequest
from app.core.multi_agent import PlanStep, RuntimeBudget, ScopeGrant, TaskResultStatus


def child_context():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Inspect authorized evidence.")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan",
        step_id="part",
        attempt=1,
        user_input="Inspect one source.",
    )
    scope = ScopeGrant()
    derived = asyncio.run(ContextDriver().derive(ContextRequest(
        snapshot_id="snapshot",
        child_run_id=child.run_id,
        parent_run_id=parent.run_id,
        session_id=child.session_id,
        plan_id="plan",
        plan_step=PlanStep(
            correlation_id=parent.trace_id,
            step_id="part",
            objective=child.user_input,
            output_contract="Cited facts and explicit unknowns.",
        ),
        parent_effective_scope=scope,
        session_scope=scope,
        workspace_scope=scope,
        policy_scope=scope,
        budget=RuntimeBudget(max_tokens=32768),
        policy_version="v1",
        workspace_version="v1",
        permission_version="v1",
    )))
    assert derived.snapshot is not None and derived.views is not None
    return manager, child, derived


def decisions(*actions):
    return [
        AgentTurnDecisionEvent(
            step_index=index,
            decided_at="2026-10-05T00:00:00+00:00",
            source="local",
            action=action,
            operation={"type": action},
        )
        for index, action in enumerate(actions, 1)
    ]


@pytest.mark.parametrize("termination", [
    "invalid_empty_decision", "invalid_structured_decision", "child_budget_finish",
])
def test_explicit_incomplete_termination_is_partial_despite_completed_run_and_success_prose(
    termination,
):
    manager, child, context = child_context()

    class Runner:
        calls = 0

        async def run_async(self, **kwargs):
            self.calls += 1
            if termination == "child_budget_finish":
                manager.append_event(child.run_id, termination, "Finish reserve reached.")
            manager.complete_child_run(child.run_id, result_snapshot={"answer": "All verified."})
            return AgentTurnResult(
                run_id=child.run_id,
                session_id=child.session_id,
                trace_id=child.trace_id,
                answer="All verified.",
                decision_events=decisions(
                    "final_answer" if termination == "child_budget_finish" else termination
                ),
            )

    runner = Runner()
    executor = ChildAgentExecutor(runner=runner, run_manager=manager)
    kwargs = {
        "child_run_id": child.run_id, "snapshot": context.snapshot, "views": context.views,
    }
    result = asyncio.run(executor.execute(**kwargs))

    assert manager.get_run(child.run_id).status == AgentRunStatus.COMPLETED
    assert result.status == TaskResultStatus.PARTIAL
    assert termination in result.missing_requirements
    assert result.summary == "All verified."
    assert result.verification is None and result.failure is None
    assert context.snapshot.budget.max_tokens == 32768
    event = next(e for e in reversed(manager.list_events(child.run_id)) if e.type == "subtask_result")
    assert event.payload["task_status"] == "partial"
    assert termination in event.payload["missing_requirements"]

    replay = asyncio.run(executor.execute(**kwargs))
    assert replay.status == result.status
    assert replay.missing_requirements == result.missing_requirements
    assert replay.summary == result.summary
    assert runner.calls == 1


@pytest.mark.parametrize("actions", [
    ("final_answer",), ("invalid_empty_decision", "final_answer"), (),
])
def test_valid_finish_or_legacy_runner_not_classified_from_partial_sounding_prose(actions):
    manager, child, context = child_context()

    class Runner:
        async def run_async(self, **kwargs):
            manager.complete_child_run(child.run_id, result_snapshot={"answer": "Some unknowns."})
            return AgentTurnResult(
                run_id=child.run_id,
                session_id=child.session_id,
                trace_id=child.trace_id,
                answer="Some unknowns.",
                decision_events=decisions(*actions),
            )

    result = asyncio.run(ChildAgentExecutor(runner=Runner(), run_manager=manager).execute(
        child_run_id=child.run_id, snapshot=context.snapshot, views=context.views,
    ))
    assert result.status == TaskResultStatus.COMPLETED
    assert result.missing_requirements == ()
    assert result.verification is None


@pytest.mark.parametrize("termination", ["invalid_empty_decision", "invalid_structured_decision"])
def test_completed_replay_recovers_termination_from_per_run_result_artifact(termination):
    manager, child, context = child_context()
    manager.mark_child_running(child.run_id)
    ref = {"artifact_id": f"result_{child.run_id}"}
    payload = AgentTurnResult(
        run_id=child.run_id, session_id=child.session_id, trace_id=child.trace_id,
        answer="Supported partial findings.", decision_events=decisions(termination),
    ).model_dump(mode="json")
    manager.complete_child_run(child.run_id, result_snapshot={
        "answer": "Stale cached answer.", "result_artifact_ref": ref,
    })
    loaded = []

    def load_artifact(artifact_id):
        loaded.append(artifact_id)
        return payload

    runner = SimpleNamespace(artifact_store=SimpleNamespace(load_artifact=load_artifact))
    result = asyncio.run(ChildAgentExecutor(runner=runner, run_manager=manager).execute(
        child_run_id=child.run_id, snapshot=context.snapshot, views=context.views,
    ))
    assert result.status == TaskResultStatus.PARTIAL
    assert result.missing_requirements == (termination,)
    assert result.summary == payload["answer"]
    assert loaded == [ref["artifact_id"]]


@pytest.mark.parametrize("answer", ["", " \n", None])
def test_completed_replay_without_wrapper_event_reports_missing_answer(answer):
    manager, child, context = child_context()
    manager.mark_child_running(child.run_id)
    manager.complete_child_run(child.run_id, result_snapshot={"answer": answer})
    assert not any(e.type == "subtask_result" for e in manager.list_events(child.run_id))

    result = asyncio.run(ChildAgentExecutor(runner=SimpleNamespace(), run_manager=manager).execute(
        child_run_id=child.run_id, snapshot=context.snapshot, views=context.views,
    ))

    assert result.status == TaskResultStatus.PARTIAL
    assert result.missing_requirements == ("child_answer_missing",)
    assert result.summary.strip() and result.summary != "None"
    assert result.verification is None


@pytest.mark.parametrize("recovered_answer", ["Recovered facts.", ""])
def test_recovered_artifact_owns_both_summary_and_completion_classification(recovered_answer):
    manager, child, context = child_context()
    manager.mark_child_running(child.run_id)
    payload = AgentTurnResult(
        run_id=child.run_id, session_id=child.session_id, trace_id=child.trace_id,
        answer=recovered_answer, decision_events=decisions("final_answer"),
    ).model_dump(mode="json")
    manager.complete_child_run(child.run_id, result_snapshot={
        "answer": "Stale cached answer.",
        "decision_events": [e.model_dump(mode="json") for e in decisions("invalid_empty_decision")],
        "result_artifact_ref": {"payload": payload},
    })

    result = asyncio.run(ChildAgentExecutor(runner=SimpleNamespace(), run_manager=manager).execute(
        child_run_id=child.run_id, snapshot=context.snapshot, views=context.views,
    ))

    if recovered_answer:
        assert result.status == TaskResultStatus.COMPLETED
        assert result.summary == recovered_answer
        assert result.missing_requirements == ()
    else:
        assert result.status == TaskResultStatus.PARTIAL
        assert result.missing_requirements == ("child_answer_missing",)
        assert result.summary.strip() and result.summary != "Stale cached answer."
