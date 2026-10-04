"""Full watch comparison stays local; prompt previews and optional fields are bounded."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.multi_agent import TaskResultStatus
from app.core.sessions import SessionService
from app.core.watch_execution import WatchExecutionResult
from app.core.watch_scheduler import WatchScheduler, _merge_previous_observations
from app.domains.watch import WatchInput, WatchService
from app.domains.watch_briefing import is_complete_briefing_payload, normalize_briefing
from app.storage.db import connect, get_db_path, init_db

LONG_PREFIX = "The review remains pending because " + "required evidence " * 16
ORIGINAL = LONG_PREFIX + "has not been received."
UPDATED = LONG_PREFIX + "has now been received."


def payload(claim=ORIGINAL, **fields):
    return json.dumps({"summary": "Observation", "changes": [{
        "claim": claim, "subject_key": "review/state", "evidence_refs": ["source"], **fields}],
        "unchanged": [], "unconfirmed": [], "decisions": []})


def normalize(summary, excerpt=ORIGINAL, previous=None):
    return normalize_briefing(summary=summary, evidence=[{"ref": "source", "excerpt": excerpt}],
                             previous=previous, importance_rules={})


def test_full_comparison_is_separate_from_legacy_bounded_prompt_preview():
    first = normalize(payload(subject_key="x" * 200))
    first["changes"][0]["previous_observation"] = UPDATED
    history = [{**first, "created_at": "2026-10-01"}]
    before = json.dumps(history, sort_keys=True)
    preview = _merge_previous_observations(history)
    comparison = _merge_previous_observations(history, for_comparison=True)
    for field, full in [("claim", ORIGINAL), ("current_observation", ORIGINAL),
                        ("previous_observation", UPDATED)]:
        assert preview["changes"][0][field] == full[:240]
        assert comparison["changes"][0][field] == full
    assert preview["changes"][0]["subject_key"] == comparison["changes"][0]["subject_key"] == "x" * 200
    assert json.dumps(history, sort_keys=True) == before


def test_same_long_observation_is_unchanged_but_a_tail_change_is_a_change():
    first = normalize(payload())
    previous = _merge_previous_observations([{**first, "created_at": "2026-10-01"}], for_comparison=True)
    same = normalize(payload(), previous=previous)
    assert not same["changes"] and len(same["unchanged"]) == 1
    assert same["unchanged"][0]["previous_observation"] == ORIGINAL
    changed = normalize(payload(UPDATED), excerpt=UPDATED, previous=previous)
    assert ORIGINAL[:240] == UPDATED[:240]
    assert len(changed["changes"]) == 1 and not changed["unchanged"]
    assert changed["changes"][0]["previous_observation"] == ORIGINAL


@pytest.mark.parametrize("fields", [
    {"event_id": {}}, {"event_id": 12}, {"event_id": None},
    {"title": []}, {"title": False}, {"status": {}}, {"status": 1},
    {"note": []}, {"note": None},
    {"importance": True}, {"importance": "1"}, {"importance": float("nan")},
    {"importance": float("inf")}, {"importance": float("-inf")},
])
def test_partial_admission_rejects_broken_optional_types_and_nonfinite_numbers(fields):
    assert not is_complete_briefing_payload(payload(**fields))
    result = normalize(payload(**fields))
    assert not result["changes"] and not result["unchanged"]
    assert result["unconfirmed"][0]["evidence_check"] == "invalid_metadata"


@pytest.mark.parametrize("fields", [
    {"event_id": "legacy-id", "title": "Review", "status": "pending", "note": "Optional note"},
    {"importance": 0}, {"importance": -1}, {"importance": 2.5},
    {"extra_metadata": {"not": "display authority"}},
])
def test_optional_typed_metadata_is_not_a_grounding_or_full_schema_verdict(fields):
    assert is_complete_briefing_payload(payload(**fields))
    assert not normalize(payload("Unsupported inference", **fields))["changes"]


def test_unsupported_title_is_metadata_not_a_verified_fact_prefix():
    title = "The review has been approved."
    claim = "The review remains pending."
    result = normalize(payload(claim, title=title), excerpt=claim)
    assert len(result["changes"]) == 1  # Preserve the independently grounded claim.
    assert result["changes"][0]["title"] == title  # Legacy metadata remains available.
    verified_section = result["summary"].split("无法确认：")[0]
    assert title not in verified_section
    assert claim in verified_section


def test_supported_title_must_match_this_items_cited_source_not_a_foreign_source():
    title = "The venue is North."
    summary = payload(title=title)
    own = normalize(summary, excerpt=ORIGINAL + "\n" + title)
    assert title in own["summary"]
    foreign = normalize_briefing(summary=summary, evidence=[
        {"ref": "source", "excerpt": ORIGINAL}, {"ref": "foreign", "excerpt": title}],
        previous=None, importance_rules={})
    assert title not in foreign["summary"].split("无法确认：")[0]


def test_model_supplied_server_display_flags_do_not_gain_authority():
    result = normalize(payload(reason="MODEL-UNSUPPORTED-REASON", importance_filtered=True,
        freshness={"status": "MODEL-VERIFIED"}, previous_observation="MODEL-OLD-FACT"))
    item = result["changes"][0]
    assert "MODEL-UNSUPPORTED-REASON" not in result["summary"]
    assert "importance_filtered" not in item and "previous_observation" not in item
    assert item["freshness"]["status"] == "unknown"


@pytest.mark.parametrize("refs", [
    ["https://unread.example/forged"], {"ref": "https://unread.example/forged"},
    None, 1,
])
def test_invalid_metadata_does_not_display_uncollected_refs_as_sources(refs):
    result = normalize(payload(importance=True, evidence_refs=refs))
    assert not result["changes"] and not result["unchanged"]
    assert result["unconfirmed"][0]["evidence_check"] == "invalid_metadata"
    assert "https://unread.example/forged" not in result["summary"]
    assert "声明引用未通过来源/摘录核验" in result["summary"]


def test_fake_adapter_scheduler_long_replay_keeps_full_comparison_out_of_model_goal(tmp_path):
    # Real scheduler/SQLite/session publication, fake adapter: no model or external API.
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    service = WatchService(lambda: connect(db_path))
    service.initialize()
    runtime = SimpleNamespace(agent_run_manager=InMemoryAgentRunManager(),
                              session_service=SessionService(lambda: connect(db_path)))
    goals = []

    class Adapter:
        async def run_async(self, **kwargs):
            index = len(goals)
            goals.append(kwargs["goal"])
            claim = [ORIGINAL, ORIGINAL, UPDATED][index]
            return WatchExecutionResult(status=TaskResultStatus.PARTIAL, summary=payload(claim),
                run_id=kwargs["parent_run_id"], child_run_id=f"fake-child-{index}", snapshot_id="fake-snapshot",
                evidence_refs=("source",), evidence_sources=("source",),
                evidence_metadata=({"excerpt": claim},), missing_requirements=("child_budget_finish",))

    watch = service.create(WatchInput(title="Review", goal="Follow review state", timezone="UTC",
        daily_time="08:00", categories=["mail"], scope={"web_enabled": False,
            "source_ids": ["source"], "account_ids": ["account"]}))
    scheduler = WatchScheduler(runtime, service, adapter=Adapter(), worker_count=0)
    rows = []
    for index in range(3):
        slot = datetime.now(UTC) - timedelta(minutes=3 - index)
        occurrence = service.create_occurrence(watch["watch_id"], slot)
        assert scheduler.run_one(owner="test-worker")
        rows.append(next(row for row in service.list_briefings(watch_id=watch["watch_id"])
                         if row["occurrence_id"] == occurrence["occurrence_id"]))
    assert len(rows[0]["changes"]) == len(rows[1]["unchanged"]) == len(rows[2]["changes"]) == 1
    assert not rows[1]["changes"] and not rows[2]["unchanged"]
    assert rows[2]["changes"][0]["previous_observation"] == ORIGINAL
    for goal in goals[1:]:
        preview = json.loads(goal.split("Previous structured observations (bounded and possibly stale JSON): ")[1].split("\n", 1)[0])
        assert all(len(item["current_observation"]) <= 240
                   for section in ("changes", "unchanged") for item in preview[section])
        assert "has not been received" not in goal
    for row in rows:
        assert any(item.get("reason") == "child_budget_finish" for item in row["unconfirmed"])
