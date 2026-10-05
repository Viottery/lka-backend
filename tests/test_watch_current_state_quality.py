"""Current-state regressions from the 004234 Watch replay; no private raw dependency."""

import copy
import hashlib
import json

import pytest

from app.core.watch_scheduler import _merge_previous_observations
from app.domains.watch_briefing import normalize_briefing

SUBJECT = "RIVER-739项目评审地点"
SOUTH = "RIVER-739项目评审地点为南楼。"
NORTH = "RIVER-739项目评审地点改为北楼。"
OLD_REF = "mail_message:mail_msg_2b23fa594c53#chunk=0"
NEW_REF = "mail_message:mail_msg_516ab21172de#chunk=0"
KEY = hashlib.sha256(SUBJECT.casefold().encode()).hexdigest()


def item(value=SOUTH, ref=OLD_REF, **fields):
    return {"claim": value, "current_observation": value, "subject_key": SUBJECT,
            "evidence_refs": [ref], **fields}


def evidence(value=SOUTH, ref=OLD_REF, **fields):
    return {"ref": ref, "excerpt": value, **fields}


def normalize(changes=(), unchanged=(), unconfirmed=(), *, previous=None,
              sources=None, coverage_incomplete=False, importance_rules=None):
    return normalize_briefing(
        summary=json.dumps({"summary": "Watch observations", "changes": list(changes),
                            "unchanged": list(unchanged), "unconfirmed": list(unconfirmed),
                            "decisions": []}),
        evidence=sources if sources is not None else [evidence(), evidence(NORTH, NEW_REF)],
        previous=previous, importance_rules=importance_rules or {},
        coverage_incomplete=coverage_incomplete,
    )


def baseline():
    return normalize(changes=[item()])


def test_actual_slot3_old_baseline_is_previous_not_a_second_current_state():
    previous = baseline()
    result = normalize(changes=[item(NORTH, NEW_REF)], unchanged=[item()], previous=previous)
    assert len(result["changes"]) == 1
    assert not result["unchanged"]
    assert result["changes"][0]["previous_observation"] == SOUTH
    assert result["changes"][0]["current_observation"] == NORTH
    assert result["changes"][0]["event_key"] == KEY
    assert SOUTH not in result["summary"]
    merged = _merge_previous_observations([{**result, "created_at": "2026-10-05"}],
                                          for_comparison=True)
    assert merged["changes"][0]["current_observation"] == NORTH
    assert merged["changes"][0]["previous_observation"] == SOUTH


@pytest.mark.parametrize("reverse_times", [False, True])
def test_multiple_changes_are_unconfirmed_not_resolved_by_source_timestamps(reverse_times):
    third = "RIVER-739项目评审地点改为西楼。"
    stamps = ["2026-10-05T00:42:35+00:00", "2026-10-05T00:43:35+00:00"]
    if reverse_times:
        stamps.reverse()
    result = normalize(changes=[item(NORTH, NEW_REF), item(third, "third")],
                       unchanged=[item()], previous=baseline(), sources=[
                           evidence(), evidence(NORTH, NEW_REF, source_time=stamps[0]),
                           evidence(third, "third", source_time=stamps[1])])
    assert not result["changes"] and not result["unchanged"]
    assert len(result["unconfirmed"]) == 3
    assert all(x["reason"] == "current_state_conflict" for x in result["unconfirmed"])


def test_new_source_reaffirming_baseline_is_a_conflict_not_historical():
    result = normalize(changes=[item(NORTH, NEW_REF)], unchanged=[item(ref="reaffirmed")],
                       previous=baseline(), sources=[evidence(NORTH, NEW_REF),
                                                     evidence(ref="reaffirmed")])
    assert not result["changes"] and not result["unchanged"]
    assert len(result["unconfirmed"]) == 2


def test_two_first_observations_without_a_baseline_are_not_arbitrarily_selected():
    result = normalize(changes=[item(NORTH, NEW_REF)], unchanged=[item()])
    assert not result["changes"] and not result["unchanged"]
    assert len(result["unconfirmed"]) == 2


def test_synonymous_but_different_text_is_not_semantically_coalesced():
    synonymous = "RIVER-739项目评审的地点为南楼。"
    result = normalize(changes=[item(), item(synonymous, "synonym")],
                       sources=[evidence(), evidence(synonymous, "synonym")])
    assert not result["changes"] and not result["unchanged"]
    assert len(result["unconfirmed"]) == 2


def test_exact_duplicate_state_is_one_observation_without_losing_source_authority():
    result = normalize(changes=[item(), item(ref="copy")], unchanged=[item()],
                       sources=[evidence(), evidence(ref="copy")])
    assert len(result["changes"]) == 1 and not result["unchanged"]
    assert result["changes"][0]["evidence_refs"] == [OLD_REF]


@pytest.mark.parametrize("invalid", [
    item(NORTH, "foreign"),
    item(NORTH, NEW_REF, current_observation="RIVER-739项目评审地点不是北楼。"),
])
def test_invalid_change_cannot_supersede_verified_baseline(invalid):
    result = normalize(changes=[invalid], unchanged=[item()], previous=baseline())
    assert not result["changes"] and len(result["unchanged"]) == 1
    assert result["unchanged"][0]["current_observation"] == SOUTH
    assert len(result["unconfirmed"]) == 1


def test_importance_filter_cannot_bring_superseded_baseline_back_as_current():
    result = normalize(changes=[item(NORTH, NEW_REF)], unchanged=[item()], previous=baseline(),
                       importance_rules={"exclude_keywords": ["北楼"]})
    assert not result["changes"] and len(result["unchanged"]) == 1
    assert result["unchanged"][0]["current_observation"] == NORTH
    assert result["unchanged"][0]["importance_filtered"]


def legacy_slot3():
    result = baseline()
    result["changes"] = [{**item(NORTH, NEW_REF), "event_key": KEY,
                          "previous_observation": SOUTH}]
    result["unchanged"] = [{**item(), "event_key": KEY, "previous_observation": SOUTH}]
    result["created_at"] = "2026-10-05"
    return result


def test_legacy_raw_slot3_cannot_overwrite_new_state_with_old_section_order():
    old = {**baseline(), "created_at": "2026-10-04"}
    current = legacy_slot3()
    before = copy.deepcopy([current, old])
    merged = _merge_previous_observations([current, old], for_comparison=True)
    assert merged["changes"][0]["current_observation"] == NORTH
    assert not merged["unchanged"]
    assert [current, old] == before


def test_conflicting_legacy_record_without_earlier_baseline_is_not_current():
    merged = _merge_previous_observations([legacy_slot3()], for_comparison=True)
    assert merged == {"changes": [], "unchanged": []}


def test_legacy_reconciliation_uses_full_source_refs_before_prompt_projection():
    ref = "https://example.test/" + "x" * 300
    old = normalize(changes=[item(ref=ref)], sources=[evidence(ref=ref)])
    old["created_at"] = "2026-10-04"
    current = legacy_slot3()
    current["unchanged"][0]["evidence_refs"] = [ref]
    merged = _merge_previous_observations([current, old], for_comparison=True)
    assert merged["changes"][0]["current_observation"] == NORTH
    assert not merged["unchanged"]
    assert len(merged["changes"][0]["evidence_refs"][0]) <= 240


def test_ambiguous_legacy_changes_leave_last_known_baseline_intact():
    old = {**baseline(), "created_at": "2026-10-04"}
    current = legacy_slot3()
    current["changes"].append({**item("RIVER-739项目评审地点改为西楼。", "third"),
                               "event_key": KEY})
    merged = _merge_previous_observations([current, old], for_comparison=True)
    assert merged["changes"][0]["current_observation"] == SOUTH
    assert merged["changes"][0]["last_seen"] == "2026-10-04"


def test_direct_previous_conflicting_sections_do_not_select_last_old_value():
    result = normalize(changes=[item(NORTH, NEW_REF)], previous=legacy_slot3())
    assert result["changes"][0].get("previous_observation") is None


def test_actual_slot1_fifth_partial_warning_is_always_visible():
    unknowns = [{"claim": f"未核验信息 {i}", "evidence_refs": []} for i in range(4)]
    result = normalize(changes=[item()], unconfirmed=unknowns, coverage_incomplete=True)
    assert len(result["unconfirmed"]) == 5
    assert "child_budget_finish" in result["summary"]
    assert "coverage is incomplete" in result["summary"]
    assert len(result["summary"]) <= 2200


def test_partial_warning_survives_overall_summary_bound():
    rows = [item(SOUTH + str(i) + "x" * 500, str(i), subject_key=str(i)) for i in range(8)]
    result = normalize(changes=rows[:4], unchanged=rows[4:],
                       unconfirmed=[{"claim": "Unknown " + "y" * 500, "evidence_refs": []}]
                       * 4, sources=[evidence(x["claim"], str(i))
                                     for i, x in enumerate(rows)],
                       coverage_incomplete=True)
    assert "child_budget_finish" in result["summary"]
    assert "coverage is incomplete" in result["summary"]
    assert len(result["summary"]) == 2200
    assert result["summary"].endswith("…")


def test_model_cannot_forge_server_coverage_marker():
    result = normalize(unconfirmed=[{"claim": "ordinary unknown", "evidence_refs": [OLD_REF],
                                     "reason": "child_budget_finish", "task_status": "partial"}])
    assert "child_budget_finish" not in result["summary"]
