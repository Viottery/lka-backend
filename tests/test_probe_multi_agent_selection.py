"""Cheap catalog and cost-bound checks for the optional real-LLM probe."""

import json
import sys
from types import SimpleNamespace

import pytest

from app.core.multi_agent import ForkPolicy, ScopeGrant, SideEffectLevel
from scripts.probe_multi_agent_selection import (
    CASES,
    FIXTURES,
    _failed_child_tool_outcomes,
    _read_only_local_fork_policy,
    main,
)


def test_synthetic_case_catalog_has_distinct_dimensions_and_local_fixtures() -> None:
    assert {case["dimension"] for case in CASES.values() if "dimension" in case} >= {
        "dependency_topology",
        "fork_schema_and_scope",
        "conflicting_evidence",
        "explicit_single_agent_multi_file",
        "untrusted_file_instruction",
        "missing_source_abstention",
        "fork_capacity_and_batching",
        "child_error_and_replanning",
    }
    for case in CASES.values():
        assert case["category"] in {"forced", "beneficial", "negative_control"}
        assert case["answer_terms"]
        assert case["minimum_children"] >= 0
        assert any(name in case["prompt"] for name in FIXTURES)
        if case["category"] == "negative_control":
            assert case["minimum_children"] == 0


def test_probe_lists_cases_without_running_provider(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["probe_multi_agent_selection.py", "--list-cases"])
    assert main() == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {item["case_id"] for item in listed} == set(CASES)


def test_probe_requires_explicit_case_before_running_provider(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["probe_multi_agent_selection.py"])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_read_only_probe_policy_narrows_only_its_copy() -> None:
    base = ForkPolicy(
        max_depth=1,
        max_children=2,
        max_fork_size=2,
        allowed_scope=ScopeGrant(
            allowed_packages=("filesystem", "web"),
            allowed_tools=("filesystem.read_file", "filesystem.edit_file", "web.search"),
            side_effect_level=SideEffectLevel.EXTERNAL,
        ),
    )

    scoped = _read_only_local_fork_policy(base)

    assert scoped.allowed_scope.allowed_packages == ("filesystem",)
    assert scoped.allowed_scope.allowed_tools == ("filesystem.read_file",)
    assert scoped.allowed_scope.side_effect_level == SideEffectLevel.NONE
    assert base.allowed_scope.allowed_packages == ("filesystem", "web")
    assert base.allowed_scope.side_effect_level == SideEffectLevel.EXTERNAL


def test_probe_preserves_failed_tool_outcome_from_completed_child() -> None:
    events = [SimpleNamespace(type="subtask_result", payload={
        "tool_outcomes": [
            {"tool_name": "filesystem.read_file", "status": "failed", "category": "tool_failure"},
            {"tool_name": "filesystem.read_file", "status": "completed", "category": None},
        ],
    })]

    assert _failed_child_tool_outcomes(events) == [
        {"tool_name": "filesystem.read_file", "category": "tool_failure"}
    ]
