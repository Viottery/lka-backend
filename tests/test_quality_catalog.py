"""Selection statistics/plans must not execute or relabel historical failures."""

import hashlib
import json
from copy import deepcopy

import pytest

from evals.lka_evals.quality_catalog import (
    DEFAULT_CATALOG,
    PROFILES,
    ROOT,
    load_catalog,
    replay_plan,
    repository_inventory,
    select_cases,
    statistics,
    validate_catalog,
    validate_references,
)


def case(case_id, status="open", **updates):
    return {"id": case_id, "title": "Synthetic task", "group": "context_tools",
            "tier": "challenge", "status": status, "symptom": "Observed failure",
            "cause": "Source analysis", "optimization": "Bounded correction",
            "acceptance": ["Evidence must support the assigned contract"],
            "evidence": ["docs/quality_iteration_2026-10-05.md#r1"],
            "regression_tests": [], "tags": [], "exposure": "seen",
            "data_class": "synthetic", "replay": None, **updates}


def catalog(*cases):
    return {"schema_version": 1, "catalog_id": "test", "as_of": "2026-10-05",
            "description": "Curated cases, not model accuracy", "cases": list(cases)}


def test_focus_skips_foundation_and_confirmed_repairs_without_laundering_offline():
    payload = validate_catalog(catalog(
        case("base", "baseline_passed", tier="foundation"),
        case("fixed", "fixed_live", tier="regression"),
        case("pending", "fixed_offline", tier="regression"),
        case("open"), case("unclear", "inconclusive"),
    ))
    assert [c["id"] for c in select_cases(payload)] == ["open", "pending", "unclear"]
    assert [c["id"] for c in select_cases(payload, profile="foundation")] == ["base"]
    assert [c["id"] for c in select_cases(payload, profile="regression")] == ["fixed", "pending"]
    assert statistics(payload["cases"])["by_status"] == {
        "baseline_passed": 1, "fixed_live": 1, "fixed_offline": 1, "open": 1, "inconclusive": 1,
    }


@pytest.mark.parametrize("field,value", [("groups", {"typo"}), ("statuses", {"completed"}),
                                        ("case_ids", {"missing"}), ("profile", "quick")])
def test_unknown_selection_is_rejected(field, value):
    with pytest.raises(ValueError):
        select_cases(catalog(case("one")), **{field: value})


def test_group_and_status_filters_intersect_and_sort_stably():
    payload = catalog(case("z", group="web"), case("a", group="web"), case("b", group="rag"))
    assert [c["id"] for c in select_cases(payload, groups={"web"}, statuses={"open"})] == ["a", "z"]
    assert select_cases(payload, groups={"web"}, statuses={"fixed_live"}) == []


def test_old_holdouts_are_not_unseen_and_labels_do_not_change_on_selection():
    payload = catalog(case("old", tags=["heldout"]), case("other"))
    before = deepcopy(payload)
    chosen = select_cases(payload, profile="historical_holdout")
    assert [c["id"] for c in chosen] == ["old"]
    assert statistics(chosen)["unseen_cases"] == 0 and payload == before


@pytest.mark.parametrize("mutation", [
    lambda p: p["cases"].append(deepcopy(p["cases"][0])),
    lambda p: p.update(schema_version=True),
    lambda p: p["cases"][0].update(status="completed"),
    lambda p: p["cases"][0].update(tier="foundation"),
    lambda p: p["cases"][0].update(evidence=[]),
    lambda p: p["cases"][0].update(regression_tests=["/tmp/test_foreign.py"]),
    lambda p: p["cases"][0].update(regression_tests=["tests/../private.py"]),
    lambda p: p["cases"][0].update(replay={"runner": [], "case": None, "args": []}),
    lambda p: p["cases"][0].update(replay={"runner": "parallel", "case": None, "args": ["--remote"]}),
    lambda p: p["cases"][0].update(replay={"runner": "realworld", "case": None, "args": []}),
])
def test_malformed_duplicate_or_unsafe_catalog_is_rejected(mutation):
    payload = catalog(case("one"))
    mutation(payload)
    with pytest.raises(ValueError):
        validate_catalog(payload)


def test_plan_deduplicates_jobs_and_tests_without_executing_or_granting_remote():
    shared = {"runner": "parallel", "case": None, "args": []}
    chosen = [case("a", replay=shared, regression_tests=["tests/test_runtime_parallel_quality.py"]),
              case("b", replay=shared, regression_tests=["tests/test_runtime_parallel_quality.py::test_x"]),
              case("manual")]
    plan = replay_plan(chosen)
    assert plan["executed"] is False and plan["paid_calls"] == 0
    assert plan["unique_regression_targets"] == 1 and len(plan["replay_jobs"]) == 1
    job = plan["replay_jobs"][0]
    assert job["logical_case_ids"] == ["a", "b"]
    assert "--remote" not in job["command_without_authorization"]
    assert job["required_explicit_flags"] == ["--remote", "--root-go"]
    assert plan["manual_replay_cases"] == ["manual"]


def test_private_mail_plan_never_guesses_production_database():
    plan = replay_plan([case("private", data_class="private_snapshot",
                            replay={"runner": "realworld", "case": "real_mail_overview", "args": []})])
    job = plan["replay_jobs"][0]
    assert job["requires_private_snapshot"] is True
    assert "--mail-db" not in job["command_without_authorization"]
    assert "explicit" in plan["note"]


def test_missing_regression_target_is_not_silently_ignored():
    payload = catalog(case("one", regression_tests=["tests/test_not_present_quality.py"]))
    with pytest.raises(ValueError, match="regression target"):
        validate_references(payload)


@pytest.mark.parametrize("flags", [["--rem"], ["--root-g"], ["--remote=true"], ["--r"]])
def test_abbreviated_or_equals_remote_authorization_cannot_enter_plan(flags):
    payload = catalog(case("one", replay={"runner": "parallel", "case": None, "args": flags}))
    with pytest.raises(ValueError, match="dispatch"):
        validate_catalog(payload)


def test_web_replay_case_must_exist_in_bounded_runner():
    payload = catalog(case("one", replay={"runner": "web", "case": "does_not_exist", "args": []}))
    with pytest.raises(ValueError, match="web"):
        validate_references(payload)


def test_two_top_level_functions_cannot_be_composed_into_a_pytest_nodeid():
    target = ("tests/test_quality_catalog.py::test_group_and_status_filters_intersect_and_sort_stably"
              "::test_old_holdouts_are_not_unseen_and_labels_do_not_change_on_selection")
    with pytest.raises(ValueError, match="node"):
        validate_references(catalog(case("one", regression_tests=[target])))


def test_checked_in_catalog_has_real_references_and_consistent_statistics():
    payload = load_catalog()
    validate_references(payload)
    result = statistics(payload["cases"])
    assert result["logical_cases"] == sum(result["by_status"].values())
    assert result["logical_cases"] == sum(g["total"] for g in result["groups"].values())
    assert not any(c["tier"] == "foundation" for c in select_cases(payload))
    assert result["unseen_cases"] == 0


def test_repository_inventory_counts_definitions_not_provider_dispatches():
    result = repository_inventory()
    assert result["suite_case_definitions"] == sum(s["cases"] for s in result["suites"])
    assert result["unique_suite_case_ids"] <= result["suite_case_definitions"]
    assert result["goal_only_scenarios"] == len(set(result["goal_only_case_ids"]))
    assert result["public_queries"] == sum(d["queries"] for d in result["public_datasets"])
    assert all(d["queries"] > 0 and d["documents"] > 0 and len(d["sha256"]) == 64
               for d in result["public_datasets"])


def test_checked_in_statistics_snapshot_matches_catalog_not_a_manual_total():
    payload = load_catalog()
    snapshot = json.loads((ROOT / "evals/fixtures/linux_quality_catalog_statistics_20261005.json").read_text())
    assert snapshot["catalog_sha256"] == hashlib.sha256(DEFAULT_CATALOG.read_bytes()).hexdigest()
    assert snapshot["catalog_statistics"] == statistics(payload["cases"])
    assert snapshot["profile_counts"] == {p: len(select_cases(payload, profile=p)) for p in PROFILES}
    plan = replay_plan(payload["cases"])
    assert snapshot["plan_statistics"]["unique_replay_jobs"] == len(plan["replay_jobs"])
    assert snapshot["plan_statistics"]["unique_regression_targets"] == plan["unique_regression_targets"]
    assert snapshot["repository_inventory"] == repository_inventory()
