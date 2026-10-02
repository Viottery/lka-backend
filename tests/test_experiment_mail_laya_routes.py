from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from experiment_mail_laya_routes import (
    AXIS_STRESS,
    CHALLENGE,
    classify_facets,
    load_cases,
    selective_replay,
)


def test_challenge_fixtures_are_balanced_and_stress_is_pairwise() -> None:
    challenge = load_cases(CHALLENGE)
    assert len(challenge) == 24
    assert {label: sum(case["label"] == label for case in challenge) for label in
            {case["label"] for case in challenge}} == {
        "search": 4, "list": 4, "review": 4, "group_sender": 4, "align_matter": 4, "unsupported": 4,
    }
    stress = load_cases(AXIS_STRESS)
    assert len(stress) == 12
    assert {case["label"] for case in stress} == {"group_sender", "align_matter"}


def test_facet_policy_requires_read_and_a_known_axis() -> None:
    def answer(safety: str, goal: str, axis: str) -> dict:
        def field(choice: str) -> dict:
            return {"choice": choice, "probabilities": {choice: .9, "other": .1}}
        return {"answers": {"safety": field(safety), "goal": field(goal), "axis": field(axis)}}

    assert classify_facets(answer("read", "organize", "sender"))[0] == "group_sender"
    assert classify_facets(answer("read", "organize", "none"))[0] is None
    assert classify_facets(answer("write", "find", "none"))[0] == "unsupported"
    assert classify_facets(answer("unclear", "find", "none"))[0] is None


def test_selective_replay_counts_fallback_usage_and_rejects_mismatched_labels() -> None:
    laya = [{"case_id": "a", "expected": "search", "choice": "search", "margin": .95, "latency_ms": 100,
             "correct": True},
            {"case_id": "b", "expected": "list", "choice": "search", "margin": .2, "latency_ms": 110,
             "correct": False}]
    llm = [{"case_id": "a", "expected": "search", "choice": "search", "latency_ms": 1300,
            "token_usage": {"total_tokens": 200}},
           {"case_id": "b", "expected": "list", "choice": "list", "latency_ms": 1400,
            "token_usage": {"total_tokens": 220}}]
    replay = selective_replay(laya, llm, direct_labels={"search"})
    assert replay["summary"]["correct"] == 2
    assert replay["summary"]["direct"] == 1
    assert replay["summary"]["fallback"] == 1
    assert replay["summary"]["known_llm_tokens"] == 220
    assert replay["cases"][1]["latency_ms"] == 1510
    llm[1]["expected"] = "review"
    with pytest.raises(ValueError, match="same cases"):
        selective_replay(laya, llm, direct_labels={"search"})
