"""Unresolved plans retain useful results without claiming task completion."""

from contextlib import contextmanager

from app.core import agent_turn as turn
from app.core.agent_runs import InMemoryAgentRunManager
from app.core.multi_agent import EvidenceRef, Plan, PlanStep, TaskResult, TaskResultStatus
from app.core.multi_agent_aggregation import aggregate_task_results


@contextmanager
def unresolved(results):
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Independent audits.")
    manager.mark_running(parent.run_id)
    children = []
    entries = []
    for index, (status, summary) in enumerate(results):
        child = manager.create_child_run(parent_run_id=parent.run_id, plan_id="plan",
            step_id=f"part-{index}", attempt=1, user_input="Audit authorized source.")
        children.append(child)
        entries.append(TaskResult(correlation_id=parent.trace_id, result_id=f"result-{index}",
            plan_id="plan", step_id=child.step_id, child_run_id=child.run_id, attempt=1,
            snapshot_id=f"snapshot-{index}",
            status=status, summary=summary,
            missing_requirements=("unread_source",) if status == TaskResultStatus.PARTIAL else (),
        ).model_dump(mode="json"))
    manager._update_run(parent.run_id, status=manager.get_run(parent.run_id).status,
        metadata_patch={"multi_agent_replan_required": True,
            "multi_agent_plan": {"plan_id": "plan"},
            "multi_agent_aggregate": {"plan_id": "plan", "task_results": entries}})
    loop = turn.AgentTurnLoop.__new__(turn.AgentTurnLoop)
    loop.run_manager = manager
    tokens = [(variable, variable.set(value)) for variable, value in (
        (turn._turn_run_manager, manager), (turn._turn_run_id, parent.run_id),
    )]
    try:
        yield loop, manager, parent, children, entries
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def test_partial_evidence_survives_unresolved_delivery_without_clearing_failure():
    with unresolved([(TaskResultStatus.PARTIAL, "Source A: checkpoint X-739; other source unread.")]) as (
        loop, manager, parent, _, _,
    ):
        answer = loop._unresolved_multi_agent_answer()
        assert "X-739" in answer and "unread_source" in answer
        assert "未完成" in answer and "未通过独立核验" in answer
        assert manager.get_run(parent.run_id).metadata["multi_agent_replan_required"] is True
        assert manager.get_run(parent.run_id).status.value == "running"


def test_foreign_child_or_plan_cannot_inject_partial_report():
    with unresolved([(TaskResultStatus.PARTIAL, "FOREIGN-REJECT")]) as (
        loop, manager, parent, children, entries,
    ):
        other = manager.create_run(session_id="other", user_input="Private task.")
        foreign = manager.create_child_run(parent_run_id=other.run_id, plan_id="plan",
            step_id="part-0", attempt=1, user_input="Private source.")
        entries[0]["child_run_id"] = foreign.run_id
        manager._update_run(parent.run_id, status=parent.status, metadata_patch={
            "multi_agent_aggregate": {"plan_id": "plan", "task_results": entries},
        })
        assert "FOREIGN-REJECT" not in loop._unresolved_multi_agent_answer()
        entries[0]["child_run_id"] = children[0].run_id
        entries[0]["plan_id"] = "unrelated-plan"
        manager._update_run(parent.run_id, status=parent.status, metadata_patch={
            "multi_agent_aggregate": {"plan_id": "plan", "task_results": entries},
        })
        assert "FOREIGN-REJECT" not in loop._unresolved_multi_agent_answer()


def test_bounded_handoff_reports_omission_without_losing_original_results():
    with unresolved([(TaskResultStatus.PARTIAL, "SOURCE " + "x" * 2000)] * 12) as (
        loop, manager, parent, _, _,
    ):
        answer = loop._unresolved_multi_agent_answer()
        assert len(answer) <= 6000
        assert "省略" in answer
        persisted = manager.get_run(parent.run_id).metadata["multi_agent_aggregate"]["task_results"]
        assert len(persisted) == 12 and all(len(item["summary"]) > 2000 for item in persisted)


def _canonical_aggregate(manager, parent, children, entries, **kwargs):
    plan = Plan(correlation_id=parent.trace_id, plan_id="plan", parent_run_id=parent.run_id,
        session_id=parent.session_id, objective="Independent audits.", steps=tuple(
            PlanStep(correlation_id=parent.trace_id, step_id=child.step_id,
                objective="Read authorized source.", output_contract="Evidence and gaps.")
            for child in children))
    aggregate = aggregate_task_results(plan, [TaskResult.model_validate(item) for item in entries],
        expected_step_ids=[child.step_id for child in children], **kwargs)
    manager._update_run(parent.run_id, status=parent.status, metadata_patch={
        "multi_agent_aggregate": aggregate.model_dump(mode="json")})
    return aggregate


def test_canonical_source_conflict_is_disclosed_even_with_completed_child_summaries():
    with unresolved([(TaskResultStatus.COMPLETED, "Source A says approved."),
                     (TaskResultStatus.COMPLETED, "Source B says not approved.")]) as (
        loop, manager, parent, children, entries,
    ):
        _canonical_aggregate(manager, parent, children, entries, evidence_refs=[
            EvidenceRef(evidence_id="same-source", source_ref="source/a"),
            EvidenceRef(evidence_id="same-source", source_ref="source/b"),
        ])
        answer = loop._unresolved_multi_agent_answer()
        assert "已知冲突" in answer and "evidence:same-source" in answer
        assert "Source A" in answer and "Source B" in answer
        assert manager.get_run(parent.run_id).metadata["multi_agent_replan_required"] is True


def test_canonical_duplicate_conflict_and_missing_group_are_not_silently_hidden():
    with unresolved([(TaskResultStatus.COMPLETED, "Untrusted winner."),
                     (TaskResultStatus.PARTIAL, "Unavailable group.")]) as (
        loop, manager, parent, children, entries,
    ):
        alternative = dict(entries[0], result_id="different", summary="Other incompatible winner.")
        aggregate = _canonical_aggregate(manager, parent, children, [entries[0], alternative])
        assert not aggregate.task_results
        answer = loop._unresolved_multi_agent_answer()
        assert "duplicate-step:part-0" in answer
        assert "未取得结果" in answer and "part-1" in answer
        assert "Untrusted winner" not in answer and "Other incompatible winner" not in answer


def test_conflict_details_are_bounded_and_foreign_correlation_is_rejected():
    with unresolved([(TaskResultStatus.PARTIAL, "Useful fact.")]) as (
        loop, manager, parent, _, entries,
    ):
        manager._update_run(parent.run_id, status=parent.status, metadata_patch={
            "multi_agent_aggregate": {"plan_id": "plan", "task_results": entries,
                "conflicts": [{"correlation_id": parent.trace_id, "conflict_id": str(index),
                               "summary": "q" * 2000} for index in range(20)] + [
                    {"correlation_id": "foreign", "conflict_id": "FOREIGN",
                     "summary": "FOREIGN-CONFLICT"}],
                "missing_step_ids": ["missing-" + "z" * 2000] * 20}})
        answer = loop._unresolved_multi_agent_answer()
        assert "Useful fact" in answer and "已知冲突" in answer and len(answer) < 6000
        assert "FOREIGN-CONFLICT" not in answer
        assert "另有 12 项缺组未展示" in answer


def test_foreign_aggregate_correlation_cannot_supply_missing_groups():
    with unresolved([(TaskResultStatus.PARTIAL, "Valid child but invalid aggregate.")]) as (
        loop, manager, parent, _, entries,
    ):
        manager._update_run(parent.run_id, status=parent.status, metadata_patch={
            "multi_agent_aggregate": {"plan_id": "plan", "correlation_id": "foreign",
                "task_results": entries, "missing_step_ids": ["FOREIGN-PRIVATE"]}})
        answer = loop._unresolved_multi_agent_answer()
        assert "FOREIGN-PRIVATE" not in answer and "Valid child" not in answer
