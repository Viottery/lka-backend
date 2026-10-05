"""Deliver complete summaries when they fit, and fair head/tail views otherwise."""

from copy import deepcopy

import pytest

from app.core.multi_agent import TaskResultStatus
from tests.test_partial_delivery_quality import unresolved


def test_complete_multiline_child_summary_beyond_old_480_prefix_is_delivered_unchanged():
    summary = "Evidence scope and qualifications.\n" * 25 + "\n" + "\n".join(
        f"Source {index}: exact finding Ω-{index}; unknowns remain."
        for index in range(30)
    )
    with unresolved([(TaskResultStatus.PARTIAL, summary),
                     (TaskResultStatus.PARTIAL, "A second bounded finding.")]) as (
        loop, manager, parent, _, entries,
    ):
        before = deepcopy(entries)
        answer = loop._unresolved_multi_agent_answer()
        assert summary in answer
        assert "A second bounded finding." in answer
        assert "摘要截取" not in answer
        assert "未完成" in answer and "未通过独立核验" in answer
        assert len(answer) <= 5500
        assert manager.get_run(parent.run_id).metadata["multi_agent_replan_required"] is True
        assert manager.get_run(parent.run_id).metadata["multi_agent_aggregate"]["task_results"] == before


@pytest.mark.parametrize("count", [2, 8, 12])
def test_overflow_fairly_delivers_each_visible_child_head_tail_and_original_reference(count):
    summaries = [f"HEAD-{index}: " + "汉" * (7000 + index * 10) + f" :TAIL-{index}"
                 for index in range(count)]
    with unresolved([(TaskResultStatus.PARTIAL, summary) for summary in summaries]) as (
        loop, manager, parent, children, entries,
    ):
        before = deepcopy(entries)
        answer = loop._unresolved_multi_agent_answer()
        assert len(answer) <= 5500
        for index in range(min(count, 8)):
            assert f"HEAD-{index}: " in answer and f" :TAIL-{index}" in answer
            assert children[index].run_id in answer
            assert entries[index]["result_id"] in answer
        assert "partial" in answer and "摘要截取" in answer and "原件" in answer
        if count > 8:
            assert "省略" in answer
        assert manager.get_run(parent.run_id).metadata["multi_agent_aggregate"]["task_results"] == before


def test_short_summary_remains_whole_when_another_child_overflows():
    small = "A complete small summary with exact line breaks.\nUnknowns: still partial."
    with unresolved([(TaskResultStatus.PARTIAL, "HEAD " + "a" * 12000 + " TAIL"),
                     (TaskResultStatus.PARTIAL, small)]) as (loop, _, _, _, _):
        answer = loop._unresolved_multi_agent_answer()
        assert small in answer
        assert "HEAD " in answer and " TAIL" in answer
        assert len(answer) <= 5500


def test_conflict_missing_and_duplicate_disclosures_share_the_same_total_cap():
    with unresolved([(TaskResultStatus.PARTIAL, "HEAD " + "x" * 20000 + " TAIL")] * 8) as (
        loop, manager, parent, _, entries,
    ):
        aggregate = deepcopy(manager.get_run(parent.run_id).metadata["multi_agent_aggregate"])
        aggregate["conflicts"] = [{"correlation_id": parent.trace_id,
            "conflict_id": "c" * 200, "summary": "Conflict " + "y" * 500} for _ in range(20)]
        aggregate["missing_step_ids"] = ["missing-" + "m" * 200] * 20
        aggregate["task_results"] = entries + [dict(entries[0], summary="Incompatible result.")]
        manager._update_run(parent.run_id, status=parent.status,
                            metadata_patch={"multi_agent_aggregate": aggregate})
        answer = loop._unresolved_multi_agent_answer()
        assert len(answer) <= 5500
        assert "已知冲突" in answer and "另有 12 项缺组" in answer
        assert "同次尝试有冲突" in answer
        assert "Incompatible result." not in answer
        assert "HEAD " in answer and " TAIL" in answer


@pytest.mark.parametrize("overflow", [0, 1])
def test_exact_total_character_boundary_preserves_full_text_before_truncating(overflow):
    with unresolved([(TaskResultStatus.PARTIAL, "Ω")]) as (
        loop, manager, parent, _, entries,
    ):
        room = 5500 - (len(loop._unresolved_multi_agent_answer()) - 1)
        summary = "H" + "Ω" * (room + overflow - 2) + "T"
        entries[0]["summary"] = summary
        manager._update_run(parent.run_id, status=parent.status, metadata_patch={
            "multi_agent_aggregate": {"plan_id": "plan", "task_results": entries},
        })
        answer = loop._unresolved_multi_agent_answer()
        assert len(answer) == 5500
        assert (summary in answer) == (overflow == 0)
        assert ("摘要截取" in answer) == (overflow == 1)
        assert "H" in answer and "T" in answer


def test_large_metadata_is_accounted_for_and_groups_omitted_only_with_notice():
    with unresolved([(TaskResultStatus.PARTIAL, "HEAD " + "x" * 10000 + " TAIL")] * 8) as (
        loop, manager, parent, _, entries,
    ):
        for index, entry in enumerate(entries):
            entry["result_id"] = f"result-{index}-" + "r" * 190
            entry["missing_requirements"] = ["missing-" + "m" * 500]
        manager._update_run(parent.run_id, status=parent.status, metadata_patch={
            "multi_agent_aggregate": {"plan_id": "plan", "task_results": entries,
                "conflicts": [{"correlation_id": parent.trace_id, "conflict_id": "c" * 200,
                    "summary": "s" * 200} for _ in range(4)],
                "missing_step_ids": ["m" * 200] * 8},
        })
        answer = loop._unresolved_multi_agent_answer()
        assert len(answer) <= 5500
        assert "HEAD " in answer and " TAIL" in answer
        assert "原件" in answer and "省略" in answer
        assert "已知冲突" in answer and "未取得结果" in answer
