import json

import pytest

from scripts.eval_memory_release import DEFAULT_FIXTURE, evaluate, load_cases


def test_offline_memory_release_fixture_checks_source_scope_retraction_and_injection():
    report = evaluate(DEFAULT_FIXTURE)

    assert report["offline"] is True
    assert report["samples"] == 5
    assert report["passed"] == 5
    assert report["failed"] == 0
    assert report["metrics"]["provenance_source_accuracy"] == {
        "numerator": 2, "denominator": 2, "rate": 1.0,
    }
    assert report["metrics"]["scope_isolation_pass_rate"]["rate"] == 1.0
    assert report["metrics"]["retraction_non_injection_pass_rate"]["rate"] == 1.0
    assert report["metrics"]["external_injection_promotion_count"] == 0
    assert report["metrics"]["candidate_injection_count"] == 0
    assert report["release_gate"]["production_gate_eligible"] is False
    assert report["release_gate"]["status"] == "synthetic_regression_only"


def test_fixture_loader_rejects_duplicate_case_ids(tmp_path):
    fixture = tmp_path / "cases.jsonl"
    fixture.write_text(
        json.dumps({"id": "duplicate", "kind": "unknown"}) + "\n"
        + json.dumps({"id": "duplicate", "kind": "unknown"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unique"):
        load_cases(fixture)


def test_fixture_is_anonymous_and_contains_only_synthetic_labels():
    raw = DEFAULT_FIXTURE.read_text(encoding="utf-8").lower()
    assert "@" not in raw
    assert "/home/" not in raw
    assert "api_key" not in raw
