"""Known failing R27-D reproducer; opt in explicitly, outside default tests.

Accepted degradation must not erase a child's already recorded partial evidence.

R26 reproducer: one partial step is skipped/degraded while another remains
failed. Historical evidence is delivery-only, never current contract success.
"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy

import pytest

from app.core import agent_turn as turn
from app.core.multi_agent import (
    ForkPolicy,
    Plan,
    PlanPatch,
    PlanPatchOperation,
    PlanStepStatus,
    TaskResultStatus,
)
from app.core.multi_agent_scheduler import MultiAgentScheduler
from tests.test_multi_agent_scheduler import FakeChildExecutor, _active_parent, _step

NOTE = "DEGRADE-NOTE: source lookup was empty; independent coverage remains incomplete."
HISTORY = "HISTORY-HEAD: authorized lookup returned no rows, workspace unread. HISTORY-TAIL"
CURRENT = "CURRENT-HEAD: a different partial investigation remains unresolved. CURRENT-TAIL"


class PartialExecutor(FakeChildExecutor):
    def __init__(self, manager, summaries):
        super().__init__(manager)
        self.summaries = summaries

    async def execute(self, **kwargs):
        result = await super().execute(**kwargs)
        child = self.manager.get_run(kwargs["child_run_id"])
        return result.model_copy(update={
            "attempt": child.attempt,
            "status": TaskResultStatus.PARTIAL,
            "summary": self.summaries[child.step_id],
            "missing_requirements": ("child_budget_finish",),
        })


@contextmanager
def degraded_run(history=HISTORY, current=CURRENT):
    manager, parent, _, context = _active_parent(_step("skipped-part"), _step("failed-part"))
    executor = PartialExecutor(manager, {"skipped-part": history, "failed-part": current})
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor, max_retries=2)
    first = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))
    assert first.status == "failed" and len(first.task_results) == 2
    assert all(result.status == TaskResultStatus.PARTIAL for result in first.task_results)
    loop = turn.AgentTurnLoop.__new__(turn.AgentTurnLoop)
    loop.run_manager = manager
    loop.fork_policy = ForkPolicy(
        max_depth=2, max_children=5, max_fork_size=3, allowed_scope=context.policy_scope,
    )
    loop.fork_scope_resolver = lambda _: (
        context.parent_effective_scope, context.session_scope, context.workspace_scope,
    )
    loop.multi_agent_max_retries = 2
    loop.fork_execution = lambda run_id: asyncio.run(
        scheduler.execute_plan_async(run_id, context=context)
    )
    plan = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    patch = PlanPatch(
        patch_id="accepted-skip", plan_id=plan.plan_id,
        expected_revision=plan.patch_revision, operation=PlanPatchOperation.SKIP_AND_DEGRADE,
        target_step_id="skipped-part", reason="Disclose incomplete independent coverage.",
        degradation_note=NOTE,
    )
    applied = loop._handle_plan_patch_decision(
        run_id=parent.run_id, operation=patch.model_dump(mode="json"),
    )
    assert applied["status"] == "applied" and applied["execution_status"] == "failed"
    saved = manager.get_run(parent.run_id)
    canonical = Plan.model_validate(saved.metadata["multi_agent_plan"])
    assert canonical.patch_history[-1].patch == patch
    assert next(step for step in canonical.steps if step.step_id == "skipped-part").status == PlanStepStatus.SKIPPED
    assert saved.metadata["multi_agent_verification"]["status"] == "inconclusive"
    tokens = [(variable, variable.set(value)) for variable, value in (
        (turn._turn_run_manager, manager), (turn._turn_run_id, parent.run_id),
    )]
    try:
        yield loop, manager, parent, first.task_results[0]
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def test_accepted_skip_note_is_delivered_while_failed_plan_stays_failed():
    with degraded_run() as (loop, manager, parent, _):
        before = deepcopy(manager.get_run(parent.run_id))
        answer = loop._unresolved_multi_agent_answer()
        assert NOTE in answer
        assert "降级" in answer and "未完成" in answer
        assert manager.get_run(parent.run_id) == before


def test_skipped_partial_is_historical_not_a_never_executed_or_verified_result():
    with degraded_run() as (loop, manager, parent, result):
        before = deepcopy(manager.get_run(parent.run_id))
        answer = loop._unresolved_multi_agent_answer()
        assert HISTORY in answer
        assert "历史" in answer and "未通过独立核验" in answer
        assert "child_budget_finish" in answer
        assert not any("未取得结果" in line and "skipped-part" in line for line in answer.splitlines())
        assert result.status == TaskResultStatus.PARTIAL
        assert manager.get_run(parent.run_id) == before


def test_historical_and_current_partial_share_existing_fair_delivery_cap():
    history = "HISTORY-HEAD " + "史" * 18000 + " HISTORY-TAIL"
    current = "CURRENT-HEAD " + "现" * 18000 + " CURRENT-TAIL"
    with degraded_run(history, current) as (loop, manager, parent, result):
        before = deepcopy(manager.get_run(parent.run_id))
        answer = loop._unresolved_multi_agent_answer()
        assert len(answer) <= 5500
        assert NOTE in answer and "历史" in answer
        assert "HISTORY-HEAD" in answer and "HISTORY-TAIL" in answer
        assert "CURRENT-HEAD" in answer and "CURRENT-TAIL" in answer
        assert result.child_run_id in answer and result.result_id in answer
        assert "摘要截取" in answer and "原件" in answer
        assert manager.get_run(parent.run_id) == before


@pytest.mark.parametrize("field,value", [
    ("correlation_id", "foreign-trace"),
    ("plan_id", "foreign-plan"),
    ("step_id", "foreign-step"),
    ("child_run_id", "missing-child"),
    ("attempt", 2),
])
def test_historical_result_identity_mismatch_cannot_inject_delivery(monkeypatch, field, value):
    with degraded_run() as (loop, manager, parent, _):
        list_events = manager.list_events

        def filtered_events(run_id):
            events = list_events(run_id)
            if run_id != parent.run_id:
                return events
            output = []
            for event in events:
                payload = deepcopy(event.payload)
                result = payload.get("task_result")
                if event.type == "subtask_result" and result and result["step_id"] == "skipped-part":
                    result.update({field: value, "summary": "FOREIGN-HISTORICAL-INJECTION"})
                    event = event.model_copy(update={"payload": payload})
                output.append(event)
            return output

        monkeypatch.setattr(manager, "list_events", filtered_events)
        assert "FOREIGN-HISTORICAL-INJECTION" not in loop._unresolved_multi_agent_answer()


def test_forged_skip_event_without_accepted_history_cannot_authorize_history_or_note():
    with degraded_run() as (loop, manager, parent, _):
        saved = manager.get_run(parent.run_id)
        plan = deepcopy(saved.metadata["multi_agent_plan"])
        plan["patch_history"] = []
        plan["patch_revision"] = 0
        manager._update_run(parent.run_id, status=saved.status,
                            metadata_patch={"multi_agent_plan": plan})
        answer = loop._unresolved_multi_agent_answer()
        assert NOTE not in answer and HISTORY not in answer


def test_other_parent_child_cannot_supply_skipped_history(monkeypatch):
    with degraded_run() as (loop, manager, parent, _):
        other = manager.create_run(session_id="other", user_input="Other private task.")
        foreign = manager.create_child_run(
            parent_run_id=other.run_id, plan_id="plan_scheduler", step_id="skipped-part",
            attempt=1, user_input="Other authorized source.",
        )
        list_events = manager.list_events

        def filtered_events(run_id):
            output = []
            for event in list_events(run_id):
                payload = deepcopy(event.payload)
                result = payload.get("task_result")
                if run_id == parent.run_id and event.type == "subtask_result" and result and result["step_id"] == "skipped-part":
                    result.update({"child_run_id": foreign.run_id, "summary": "OTHER-PARENT-INJECTION"})
                    event = event.model_copy(update={"payload": payload})
                output.append(event)
            return output

        monkeypatch.setattr(manager, "list_events", filtered_events)
        assert "OTHER-PARENT-INJECTION" not in loop._unresolved_multi_agent_answer()
