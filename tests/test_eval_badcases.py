import json

import pytest

from evals.lka_evals.badcases import (
    BadcaseManifest,
    load_badcase_manifest,
    select_badcases,
    validate_badcase_manifest,
)


def _case(case_id: str, *, tags=None, agent_id="general", executor_type="react"):
    return {
        "case_id": case_id,
        "task": "Summarize the sanitized fixture without changing data.",
        "agent_id": agent_id,
        "executor_type": executor_type,
        "severity": "medium",
        "tags": tags or [],
        "fixture_ids": [],
        "privacy_reviewed": True,
        "checks": [{"metric": "tool_success_rate", "operator": "gte", "value": 1}],
    }


def _manifest(*cases):
    return {
        "manifest_version": 1,
        "manifest_id": "reviewed-regressions",
        "description": "Curated synthetic regression examples",
        "cases": list(cases),
    }


def test_badcase_manifest_loads_and_selection_is_stable(tmp_path):
    path = tmp_path / "badcases.json"
    path.write_text(
        json.dumps(_manifest(_case("case-z", tags=["approval"]), _case("case-a", tags=["approval"]))),
        encoding="utf-8",
    )

    loaded = load_badcase_manifest(path)
    selected = select_badcases(loaded, tags={"approval"}, limit=1)

    assert isinstance(loaded, BadcaseManifest)
    assert [case.case_id for case in selected] == ["case-a"]
    assert selected[0].checks[0].value == 1


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda data: data.update(manifest_version=2), "Unsupported"),
        (lambda data: data["cases"][0].update(privacy_reviewed=False), "privacy_reviewed"),
        (lambda data: data["cases"][0].update(raw_log="secret"), "prohibited raw-data"),
        (lambda data: data["cases"][0].update(tool_plan=[{"tool": "write"}]), "unsupported field"),
    ],
)
def test_badcase_manifest_rejects_unsafe_or_unknown_data(mutate, message):
    data = _manifest(_case("case-a"))
    mutate(data)

    with pytest.raises(ValueError, match=message):
        validate_badcase_manifest(data)


def test_badcase_selector_rejects_unknown_ids_and_filters_by_executor():
    loaded = validate_badcase_manifest(
        _manifest(
            _case("case-a", executor_type="react"),
            _case("case-b", executor_type="workflow"),
        )
    )

    assert [case.case_id for case in select_badcases(loaded, executor_type="workflow")] == ["case-b"]
    with pytest.raises(ValueError, match="Unknown badcase"):
        select_badcases(loaded, case_ids={"missing"})
