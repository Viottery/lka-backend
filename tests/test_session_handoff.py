"""Offline structural contracts for source-grounded session handoff replay."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.background_jobs import BackgroundJobStore
from app.core.context_projection import render_handoff
from app.core.context_reconcile import (
    ContextAttributionConflict,
    _check_handoff_repair,
    propose_delta,
    select_prefix,
)
from app.core.context_record_models import HandoffDelta
from app.core.context_sources import source_passages
from app.core.local_config import MemoryConfig
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.prompt_tokens import PromptTokenCounter
from app.core.sessions import SessionService
from app.core.tools import ToolContext, ToolInvocation
from app.storage.context_records import ContextRecordConflict, ContextRecordStore
from app.storage.db import connect, init_db
from app.tool_packages.context_records import ReadContextRecordsTool, ReadContextSourceTool


@pytest.fixture
def system(tmp_path):
    path = tmp_path / "handoff.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    sessions = SessionService(factory, context_token_counter=PromptTokenCounter())
    session = sessions.create_session(title="Handoff test").session.session_id
    sessions.record_context_exchange(session_id=session, user_input="先整理当前工作方向。",
                                     agent_answer="好的。", trace_id="turn-1")
    store = ContextRecordStore(factory)
    store.ensure_schema()
    return SimpleNamespace(db=path, factory=factory, sessions=sessions, session=session, store=store)


def value(system, *, seq, kind, content, role="user", evidence=None, topic="session",
          subject=None, status=None, priority=3):
    snapshot = system.store.snapshot(system.session)
    row = next(item for item in snapshot["messages"] if item["seq"] == seq)
    passage = source_passages(system.session, row)[0]
    evidence = evidence or ("user_statement" if role == "user" else "assistant_report")
    result = {"kind": kind, "subject": subject or (kind if kind in {"focus", "phase"} else "用户"),
              "topic": topic, "content": content, "evidence": evidence,
              "sources": [{"passage_id": passage["passage_id"]}], "priority": priority}
    if kind == "task":
        result["task_status"] = status or "active"
    return result


class Model:
    """Return only prepared offline JSON; it never calls a provider."""

    def __init__(self, *deltas):
        self.deltas = list(deltas)
        self.calls = []

    def complete_text(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=json.dumps(self.deltas.pop(0), ensure_ascii=False), metadata={}, usage={})


def replay(system, changes, *, profile="handoff"):
    snapshot = system.store.snapshot(system.session)
    delta = {"changes": changes}
    model = Model(delta, delta) if profile == "handoff" else Model(delta)
    messages = [row for row in snapshot["messages"] if row["seq"] > snapshot["covered_seq"]]
    validated, _ = propose_delta(model, snapshot, messages, profile=profile)
    system.store.publish(snapshot, validated)
    return system.store.snapshot(system.session)


def append(system, user, answer="收到。", trace="next"):
    system.sessions.record_context_exchange(session_id=system.session, user_input=user,
                                            agent_answer=answer, trace_id=trace)


def test_direction_and_phase_update_replace_singletons(system):
    state = replay(system, [
        {"op": "add", "reason": "initial user direction", "value": value(
            system, seq=1, kind="focus", content="完成课程 A 的复习计划。")},
        {"op": "add", "reason": "initial stage", "value": value(
            system, seq=1, kind="phase", content="先盘点材料。")},
    ])
    focus = next(row for row in state["records"] if row["kind"] == "focus")
    phase = next(row for row in state["records"] if row["kind"] == "phase")
    append(system, "改为先准备课程 B。", trace="turn-2")
    changes = [
        {"op": "update", "record_id": focus["record_id"], "expected_revision": focus["revision"],
         "reason": "user changed direction", "value": value(system, seq=3, kind="focus", content="先准备课程 B。")},
        {"op": "update", "record_id": phase["record_id"], "expected_revision": phase["revision"],
         "reason": "stage advanced", "value": value(system, seq=3, kind="phase", content="整理 B 的重点。")},
    ]
    state = replay(system, changes)
    assert sum(row["kind"] == "focus" and row["validity"] == "current" for row in state["records"]) == 1
    assert sum(row["kind"] == "phase" and row["validity"] == "current" for row in state["records"]) == 1
    assert len([row for row in state["records"] if row["kind"] == "focus"]) == 1
    assert "课程 B" in next(row["content"] for row in state["records"] if row["kind"] == "focus")


def test_topic_switch_keeps_open_task_and_assistant_report_is_not_verified_completion(system):
    state = replay(system, [
        {"op": "add", "reason": "requested direction", "value": value(
            system, seq=1, kind="focus", content="准备课程 A。")},
        {"op": "add", "reason": "unresolved commitment", "value": value(
            system, seq=1, kind="task", topic="课程 A", subject="用户", content="交课程 A 报告。")},
    ])
    focus = next(row for row in state["records"] if row["kind"] == "focus")
    task = next(row for row in state["records"] if row["kind"] == "task")
    append(system, "先转去准备课程 B。", trace="turn-2")
    switched = replay(system, [
        {"op": "update", "record_id": focus["record_id"], "expected_revision": focus["revision"],
         "reason": "new user focus", "value": value(system, seq=3, kind="focus", content="先准备课程 B。")},
    ])
    still_open = next(row for row in switched["records"] if row["record_id"] == task["record_id"])
    assert still_open["task_status"] == "active"
    append(system, "课程 A 报告已经发送。", trace="turn-3")
    state = replay(system, [
        {"op": "update", "record_id": task["record_id"], "expected_revision": task["revision"],
         "reason": "assistant reported an answer", "value": value(
             system, seq=6, role="agent", kind="task", topic="课程 A", subject="用户",
             content="agent 报告课程 A 报告已发送，未独立核实。", evidence="assistant_report", status="completed")},
    ])
    active = next(row for row in state["records"] if row["record_id"] == task["record_id"])
    assert active["task_status"] == "completed"  # Reported conversational state, not external verification.
    view = render_handoff(state["records"], covered_seq=6, budget=2048)
    assert "未独立核实" in view["text"]
    assert "课程 B" in view["text"]


def test_user_correction_replaces_prior_decision_with_current_user_evidence(system):
    state = replay(system, [
        {"op": "add", "reason": "initial decision", "value": value(
            system, seq=1, kind="correction", topic="课程 A", subject="用户", content="按旧提纲复习。")},
    ])
    correction = state["records"][0]
    append(system, "更正：不要用旧提纲，按新提纲复习。", trace="turn-2")
    state = replay(system, [
        {"op": "update", "record_id": correction["record_id"], "expected_revision": correction["revision"],
         "reason": "explicit user correction", "value": value(
             system, seq=3, kind="correction", topic="课程 A", subject="用户", content="不要用旧提纲，按新提纲复习。")},
    ])
    record = state["records"][0]
    assert record["revision"] == 2 and len(record["sources"]) == 2
    assert "不要用旧提纲" in record["content"]


def test_active_session_handoff_keeps_new_raw_tail_visible(system):
    replay(system, [{"op": "add", "reason": "direction", "value": value(
        system, seq=1, kind="focus", content="完成课程 A 的复习计划。")}])
    append(system, "最新要求：只讲新推导，保留完整步骤。", trace="turn-2")
    system.sessions.atomic_context_store = system.store
    system.sessions.atomic_context_mode = "active"
    system.sessions.atomic_context_profile = "handoff"
    prompt = system.sessions.get_prompt_context_window(session_id=system.session)
    assert prompt.summary_metadata["method"] == "session_handoff"
    assert prompt.summary_metadata["covered_seq"] == 2
    assert prompt.recent_messages[0].content.startswith("最新要求")
    assert "新推导" in prompt.recent_messages[0].content


def test_background_queue_publishes_handoff_and_keeps_latest_exchange_raw(system):
    delta = {"changes": [{"op": "add", "reason": "current requested direction", "value": value(
        system, seq=1, kind="focus", content="先整理当前工作方向。")}]}
    model = Model(delta, delta)
    queue = BackgroundJobStore(system.db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(system.db), memory=None, store=queue,
        session_service=system.sessions, llm_client=model, atomic_context_mode="active",
        atomic_context_profile="handoff", atomic_context_debounce_seconds=0)
    append(system, "最新要求仍以这条原文为准。", trace="latest")
    job = queue.claim("handoff-worker", lease_seconds=600, kinds={"context_records"})
    assert job["payload"]["target_seq"] == 2
    coordinator._bounded_atomic_context(job)
    prompt = system.sessions.get_prompt_context_window(session_id=system.session)
    assert prompt.summary_metadata["method"] == "session_handoff"
    assert prompt.summary_metadata["covered_seq"] == 2
    assert prompt.summary_metadata["latest_source_seq"] == 4
    assert prompt.recent_messages[0].content == "最新要求仍以这条原文为准。"
    assert [r["prompt_summary"] for r in model.calls] == ["background_context_records", "background_handoff_review"]


def test_stale_revision_and_withdrawn_source_are_rejected(system):
    snapshot = system.store.snapshot(system.session)
    change = {"op": "add", "reason": "focus", "value": value(
        system, seq=1, kind="focus", content="方向 A")}
    model = Model({"changes": [change]}, {"changes": [change]})
    validated, _ = propose_delta(model, snapshot, snapshot["messages"], profile="handoff")
    system.store.publish(snapshot, validated)
    with pytest.raises(ContextRecordConflict, match="revision"):
        system.store.publish(snapshot, validated)
    with system.factory() as conn:
        conn.execute("UPDATE agent_session_context_messages SET content='withdrawn' WHERE session_id=? AND seq=1",
                     (system.session,))
    with pytest.raises(ContextRecordConflict, match="withdrawn"):
        system.store.snapshot(system.session)


def test_handoff_hard_bound_omissions_are_recoverable_and_open_tasks_survive_age(system):
    state = replay(system, [{"op": "add", "reason": "open commitment", "value": value(
        system, seq=1, kind="task", topic="project", subject="用户", content="尚未完成的交付任务。") }])
    task = state["records"][0]
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    rows = [task]
    for index in range(100):
        rows.append({**task, "record_id": f"milestone-{index}", "revision": 1,
                     "kind": "progress", "topic": "project", "subject": "milestone",
                     "content": f"已完成阶段结果 {index}。", "task_status": None,
                     "evidence": "assistant_report", "recorded_at": old,
                     "last_source_seq": index + 1, "priority": 1})
    view = render_handoff(rows, covered_seq=2, budget=512, now=datetime.now(UTC))
    assert len(view["text"]) < 1200
    assert view["tokens"] <= 512
    assert task["record_id"] not in view["omitted_ids"]
    assert len(view["omitted_ids"]) >= 100
    assert "context.read" in view["text"]
    assert "context.source" in view["text"]


def test_time_omits_old_closed_milestones_but_not_open_task(system):
    state = replay(system, [{"op": "add", "reason": "open commitment", "value": value(
        system, seq=1, kind="task", topic="project", subject="用户", content="仍需完成的任务。") }])
    task = state["records"][0]
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    closed = {**task, "record_id": "old-closed", "task_status": "completed",
              "content": "旧的已完成里程碑。", "recorded_at": old}
    view = render_handoff([task, closed], covered_seq=2, budget=2048)
    assert task["record_id"] in view["selected_ids"]
    assert "old-closed" in view["omitted_ids"]


def test_byte_bound_survives_a_tokenizer_that_underestimates_long_text(system):
    state = replay(system, [{"op": "add", "reason": "open work", "value": value(
        system, seq=1, kind="task", content="仍需继续处理。") }])
    base = state["records"][0]
    records = []
    for kind, count in (("focus", 1), ("phase", 1), ("task", 6), ("decision", 4), ("progress", 2)):
        for index in range(count):
            records.append({**base, "record_id": f"{kind}-{index}", "kind": kind,
                "content": "保留重要约束"*100, "task_status": "active" if kind == "task" else None})

    class UnderCounter:
        def count_text(self, text):
            return SimpleNamespace(count=1 if text else 0, method="deliberately_low_test_counter")

    view = render_handoff(records, covered_seq=2, budget=4096, counter=UnderCounter())
    assert len(view["text"].encode()) <= 16384 and view["omitted_ids"]
    assert view["count_method"] == "deliberately_low_test_counter"


@pytest.mark.parametrize("bad", [
    {"kind": "focus", "subject": "other", "topic": "session", "content": "x",
     "evidence": "user_statement", "sources": [{"passage_id": "p_1_0_1_0000000000000000"}], "priority": 2},
    {"kind": "phase", "subject": "phase", "topic": "other", "content": "x",
     "evidence": "assistant_report", "sources": [{"passage_id": "p_1_0_1_0000000000000000"}], "priority": 2},
    {"kind": "focus", "subject": "focus", "topic": "session", "content": "x",
     "evidence": "assistant_report", "sources": [{"passage_id": "p_1_0_1_0000000000000000"}], "priority": 2},
    {"kind": "phase", "subject": "phase", "topic": "session", "content": "x" * 601,
     "evidence": "user_statement", "sources": [{"passage_id": "p_1_0_1_0000000000000000"}], "priority": 2},
    {"kind": "episode", "subject": "user", "topic": "x", "content": "x",
     "evidence": "user_statement", "sources": [{"passage_id": "p_1_0_1_0000000000000000"}], "priority": 2},
])
def test_handoff_schema_rejects_invalid_singleton_type_and_oversize(bad):
    with pytest.raises(ValidationError):
        HandoffDelta.model_validate({"changes": [{"op": "add", "reason": "test", "value": bad}]})


def test_handoff_rejects_second_current_singleton(system):
    replay(system, [{"op": "add", "reason": "first focus", "value": value(
        system, seq=1, kind="focus", content="方向 A")}])
    append(system, "方向 B。", trace="turn-2")
    second = value(system, seq=3, kind="focus", content="方向 B")
    snapshot = system.store.snapshot(system.session)
    delta = HandoffDelta.model_validate({"changes": [{"op": "add", "reason": "second focus", "value": second}]})
    from app.core.context_reconcile import validate_delta

    with pytest.raises(ValueError, match="one current focus"):
        validate_delta(snapshot, delta, profile="handoff")


def test_general_profile_remains_default_and_accepts_legacy_record(system):
    config = MemoryConfig()
    assert config.atomic_context_mode == "off" and config.atomic_context_profile == "general"
    assert config.atomic_context_handoff_tokens == 2048
    snapshot = system.store.snapshot(system.session)
    generic = {"kind": "fact", "subject": "课程", "topic": "课程 A", "content": "课程 A 有三个单元。",
               "evidence": "user_statement", "sources": [{"passage_id": source_passages(
                   system.session, snapshot["messages"][0])[0]["passage_id"]}], "priority": 2}
    model = Model({"changes": [{"op": "add", "reason": "legacy-compatible fact", "value": generic}]})
    changes, _ = propose_delta(model, snapshot, snapshot["messages"])
    assert model.calls[0]["prompt_summary"] == "background_context_records"
    system.store.publish(snapshot, changes)
    assert system.store.snapshot(system.session)["records"][0]["kind"] == "fact"


@pytest.mark.parametrize("settings", [{"atomic_context_profile": "unknown"}, {"atomic_context_handoff_tokens": 4097}])
def test_handoff_configuration_rejects_invalid_profile_and_oversize(settings):
    with pytest.raises(ValidationError):
        MemoryConfig(**settings)


def test_profiled_prefix_selection_is_bounded_without_advancing_coverage(system):
    snapshot = system.store.snapshot(system.session)
    selected = select_prefix(snapshot, target_seq=2, input_tokens=12000, profile="handoff")
    assert [row["seq"] for row in selected] == [1, 2]
    assert system.store.snapshot(system.session)["covered_seq"] == 0


def test_handoff_review_sees_all_new_messages_and_can_restore_a_missed_commitment(system):
    snapshot = system.store.snapshot(system.session)
    task = {"op": "add", "reason": "missed open user request", "value": value(
        system, seq=1, kind="task", content="先整理当前工作方向。")}
    model = Model({"changes": []}, {"changes": [task]})
    changes, response = propose_delta(model, snapshot, snapshot["messages"], profile="handoff")
    assert len(model.calls) == 2 and response.metadata["handoff_review"]
    payload = json.loads(model.calls[1]["user_prompt"])
    assert [r["seq"] for r in payload["messages"]] == [1, 2]
    assert payload["proposed_changes"] == {"changes": []}
    system.store.publish(snapshot, changes)
    assert system.store.snapshot(system.session)["records"][0]["task_status"] == "active"


def test_handoff_review_protocol_repair_is_bounded_and_revalidated(system):
    snapshot = system.store.snapshot(system.session)
    task = {"op": "add", "reason": "open task", "value": value(
        system, seq=1, kind="task", content="先整理当前工作方向。")}
    bad = {**task, "record_id": "invented-server-id"}
    model = Model({"changes": [task]}, {"changes": [bad]}, {"changes": [task]})
    changes, _ = propose_delta(model, snapshot, snapshot["messages"], profile="handoff")
    assert len(model.calls) == 3
    assert model.calls[-1]["prompt_summary"] == "background_handoff_review_protocol_repair"
    assert changes[0]["record_id"] is None
    assert system.store.snapshot(system.session)["covered_seq"] == 0


def test_handoff_review_repair_cannot_exceed_input_capacity(system):
    snapshot = system.store.snapshot(system.session)
    task = {"op": "add", "reason": "open task", "value": value(
        system, seq=1, kind="task", content="先整理当前工作方向。")}
    bad = {**task, "record_id": "invented-server-id"}
    model = Model({"changes": [task]}, {"changes": [bad]})

    class Counter:
        def count_request(self, system, prompt):
            return SimpleNamespace(count=20000 if "rejected_delta" in prompt else 100)

    with pytest.raises(ValueError, match="review repair exceeds capacity"):
        propose_delta(model, snapshot, snapshot["messages"], profile="handoff", input_tokens=12000, counter=Counter())
    assert len(model.calls) == 2 and system.store.snapshot(system.session)["covered_seq"] == 0


def test_citation_only_patch_preserves_claims_before_whole_interval_review(system):
    snapshot = system.store.snapshot(system.session)
    good = {"op": "add", "reason": "current direction", "value": value(
        system, seq=1, kind="focus", content="先整理当前工作方向。")}
    bad = {**good, "value": {**good["value"], "sources": [{"passage_id": "p_99_0_1_0000000000000000"}]}}
    patch = {"repairs": [{"change_index": 0, "sources": good["value"]["sources"]}]}
    model = Model({"changes": [bad]}, patch, {"changes": [good]})
    changes, _ = propose_delta(model, snapshot, snapshot["messages"], profile="handoff")
    assert len(model.calls) == 3
    assert model.calls[1]["prompt_summary"] == "background_handoff_citation_repair"
    assert model.calls[2]["prompt_summary"] == "background_handoff_review"
    review = json.loads(model.calls[2]["user_prompt"])
    assert review["proposed_changes"]["changes"][0]["value"]["content"] == bad["value"]["content"]
    assert changes[0]["value"]["sources"][0]["seq"] == 1


def test_review_citation_patch_revalidates_complete_reviewed_delta(system):
    snapshot = system.store.snapshot(system.session)
    good = {"op": "add", "reason": "direction", "value": value(
        system, seq=1, kind="focus", content="先整理当前工作方向。")}
    bad = {**good, "value": {**good["value"], "sources": [{"passage_id": "p_99_0_1_0000000000000000"}]}}
    patch = {"repairs": [{"change_index": 0, "sources": good["value"]["sources"]}]}
    model = Model({"changes": [good]}, {"changes": [bad]}, patch)
    changes, response = propose_delta(model, snapshot, snapshot["messages"], profile="handoff")
    assert response.metadata["handoff_review"] is True
    assert model.calls[-1]["prompt_summary"] == "background_handoff_review_citation_repair"
    assert changes[0]["value"]["sources"][0]["role"] == "user"


def test_citation_patch_cannot_launder_assistant_only_evidence_as_user(system):
    snapshot = system.store.snapshot(system.session)
    focus = {"op": "add", "reason": "direction", "value": value(
        system, seq=1, kind="focus", content="先整理当前工作方向。")}
    bad = {**focus, "value": {**focus["value"], "sources": [{"passage_id": "p_99_0_1_0000000000000000"}]}}
    agent_ref = value(system, seq=2, role="agent", kind="phase", content="好的。")["sources"]
    invalid_user_focus = {**focus, "value": {**focus["value"], "sources": agent_ref}}
    model = Model({"changes": [bad]}, {"repairs": [{"change_index": 0, "sources": agent_ref}]},
                  {"changes": [invalid_user_focus]}, {"changes": [invalid_user_focus]})
    with pytest.raises(ValueError, match="Evidence role"):
        propose_delta(model, snapshot, snapshot["messages"], profile="handoff")
    assert len(model.calls) == 4 and system.store.snapshot(system.session)["covered_seq"] == 0


@pytest.mark.parametrize("bad_stage", ["proposal", "review", "both_reviews"])
def test_assistant_only_updates_cannot_take_over_user_focus(system, bad_stage):
    state = replay(system, [{"op": "add", "reason": "user topic", "value": value(
        system, seq=1, kind="focus", content="先整理当前工作方向。") }])
    old = state["records"][0]
    append(system, "", "后台报告另一个任务的状态。")
    bad = {"op": "update", "record_id": old["record_id"], "expected_revision": 1,
        "reason": "background report", "value": value(system, seq=4, role="agent",
            kind="focus", content="把后台任务当作新用户方向。")}
    bad_delta, empty = {"changes": [bad]}, {"changes": []}
    outputs = [bad_delta, empty] if bad_stage == "proposal" else [empty, bad_delta, bad_delta if bad_stage == "both_reviews" else empty]
    model = Model(*outputs)
    snapshot = system.store.snapshot(system.session)
    messages = [r for r in snapshot["messages"] if r["seq"] > 2]
    if bad_stage == "both_reviews":
        with pytest.raises(ValidationError):
            propose_delta(model, snapshot, messages, profile="handoff")
    else:
        changes, _ = propose_delta(model, snapshot, messages, profile="handoff")
        system.store.publish(snapshot, changes)
    assert system.store.snapshot(system.session)["records"][0]["content"] == old["content"]
    assert len(model.calls) == (2 if bad_stage == "proposal" else 3)
    if bad_stage != "proposal":
        assert model.calls[-1]["prompt_summary"] == "background_handoff_review_semantic_repair"


@pytest.mark.parametrize("attack", ["content", "target", "outside_index", "wrong_source"])
def test_citation_patch_cannot_rewrite_or_escape_authorized_sources(system, attack):
    snapshot = system.store.snapshot(system.session)
    good = {"op": "add", "reason": "current direction", "value": value(
        system, seq=1, kind="focus", content="先整理当前工作方向。")}
    bad = {**good, "value": {**good["value"], "sources": [{"passage_id": "p_99_0_1_0000000000000000"}]}}
    patch = {"change_index": 0, "sources": good["value"]["sources"]}
    if attack == "content":
        patch["content"] = "injected claim"
    elif attack == "target":
        patch["record_id"] = "other-record"
    elif attack == "outside_index":
        patch["change_index"] = 1
    else:
        patch["sources"] = [{"passage_id": "p_99_0_1_0000000000000000"}]
    model = Model({"changes": [bad]}, {"repairs": [patch]})
    with pytest.raises(ValueError):
        propose_delta(model, snapshot, snapshot["messages"], profile="handoff")
    assert len(model.calls) == 2 and system.store.snapshot(system.session)["covered_seq"] == 0


@pytest.mark.parametrize("pivot", ["missing_target", "revision", "merge_targets"])
def test_handoff_protocol_repair_cannot_redirect_existing_operations(system, pivot):
    v = value(system, seq=1, kind="task", content="先整理当前工作方向。")
    before = {"op": "update", "record_id": "first", "expected_revision": 1,
              "reason": "current request", "value": v}
    repaired = dict(before)
    if pivot == "missing_target":
        before["record_id"] = None
        repaired["record_id"] = "other"
    elif pivot == "revision":
        repaired["expected_revision"] = 2
    else:
        before.update(op="merge", merge_ids=["second"], merge_revisions=[1])
        repaired.update(op="merge", merge_ids=["third"], merge_revisions=[1])
    after = HandoffDelta.model_validate({"changes": [repaired]})
    with pytest.raises(ValueError, match="Protocol repair cannot"):
        _check_handoff_repair(json.dumps({"changes": [before]}), after)


def test_declared_operation_alias_can_be_repaired_but_not_invented(system):
    v = value(system, seq=1, kind="task", content="先整理当前工作方向。")
    repaired = {"op": "update", "record_id": "first", "expected_revision": 1,
                "reason": "repair syntax", "value": v}
    delta = HandoffDelta.model_validate({"changes": [repaired]})
    old = {k: value for k, value in repaired.items() if k != "op"}
    _check_handoff_repair(json.dumps({"changes": [{**old, "action": "UPDATE"}]}), delta)
    for payload in (old, {**old, "action": "ADD"}, {**old, "action": "INVENT"}):
        with pytest.raises(ValueError, match="Protocol repair cannot"):
            _check_handoff_repair(json.dumps({"changes": [payload]}), delta)


def test_reported_phase_can_advance_without_overwriting_user_authority(system):
    state = replay(system, [{"op": "add", "reason": "requested initial stage", "value": value(
        system, seq=1, kind="phase", content="先盘点资料。") }])
    phase = state["records"][0]
    append(system, "继续。", "资料已盘点，进入测试阶段。")
    state = replay(system, [{"op": "update", "record_id": phase["record_id"], "expected_revision": 1,
        "reason": "new reported stage", "value": value(system, seq=4, kind="phase", role="agent",
            content="agent 报告资料已盘点，进入测试阶段。") }])
    current = state["records"][0]
    assert current["evidence"] == "assistant_report" and len(current["sources"]) == 1
    assert "未独立核实" in render_handoff(state["records"], covered_seq=4, budget=2048)["text"]


@pytest.mark.parametrize("review_corrects_attribution", [True, False])
def test_cancellation_draft_is_reviewed_but_final_user_authority_stays_strict(system, review_corrects_attribution):
    state = replay(system, [{"op": "add", "reason": "user request", "value": value(
        system, seq=1, kind="task", content="先整理当前工作方向。") }])
    old = state["records"][0]
    append(system, "先停下这个任务。", "已停下，不再继续。")
    draft = {"op": "update", "record_id": old["record_id"], "expected_revision": 1,
        "reason": "stop request", "value": value(system, seq=4, role="agent", kind="task",
            content="助手报告任务已停下。", status="cancelled")}
    corrected = {**draft, "value": value(system, seq=3, kind="task", content="用户明确叫停该任务。", status="cancelled")}
    outputs = [{"changes": [draft]}, {"changes": [corrected if review_corrects_attribution else draft]}]
    if not review_corrects_attribution:
        outputs.append({"changes": [draft]})
    model = Model(*outputs)
    snapshot = system.store.snapshot(system.session)
    messages = [r for r in snapshot["messages"] if r["seq"] > 2]
    if review_corrects_attribution:
        changes, _ = propose_delta(model, snapshot, messages, profile="handoff")
        system.store.publish(snapshot, changes)
        assert system.store.snapshot(system.session)["records"][0]["task_status"] == "cancelled"
    else:
        with pytest.raises(ContextAttributionConflict):
            propose_delta(model, snapshot, messages, profile="handoff")
        assert system.store.snapshot(system.session)["records"][0]["task_status"] == "active"
    assert len(model.calls) == (2 if review_corrects_attribution else 3)
    feedback = json.loads(model.calls[1]["user_prompt"])
    assert "Non-user claims" in feedback["draft_validation_error"]


def test_followup_report_prevents_old_unknown_observation_from_standing_alone(system):
    state = replay(system, [{"op": "add", "reason": "initial observation", "value": value(
        system, seq=1, kind="correction", topic="meeting", content="会议时间尚未确定。") }])
    old = state["records"][0]
    append(system, "查一下进展。", "会议时间为14点，还需本人确认。")
    report = value(system, seq=4, kind="progress", role="agent", topic="meeting",
                   content="agent 报告会议时间为14点，还需本人确认。")
    report["related_ids"] = [old["record_id"]]
    state = replay(system, [{"op": "add", "reason": "newer related report", "value": report}])
    text = render_handoff(state["records"], covered_seq=4, budget=2048)["text"]
    assert "14点" in text and "本人确认" in text
    assert "原表述不证明现状" in text and "未独立核实" in text


def test_hundred_turn_handoff_replaces_state_and_bounds_current_sources(system):
    state = replay(system, [{"op": "add", "reason": "initial focus", "value": value(
        system, seq=1, kind="focus", content="先整理当前工作方向。")}])
    sizes = []
    for index in range(100):
        append(system, f"当前改为处理任务 {index}。", trace=f"turn-{index+2}")
        focus = state["records"][0]
        state = replay(system, [{"op": "update", "record_id": focus["record_id"],
            "expected_revision": focus["revision"], "reason": "explicit new focus", "value": value(
                system, seq=3+2*index, kind="focus", content=f"当前改为处理任务 {index}。")}])
        view = render_handoff(state["records"], covered_seq=state["covered_seq"], budget=512)
        sizes.append(view["tokens"])
        assert len(state["records"]) == 1
        assert len(state["records"][0]["sources"]) == 1
    assert max(sizes) <= 512
    assert state["covered_seq"] == 202 and len(state["messages"]) == 202
    assert "任务 99" in view["text"] and "任务 98" not in view["text"]
    with system.factory() as conn:
        assert conn.execute("SELECT count(*) FROM context_record_events WHERE session_id=?",
                            (system.session,)).fetchone()[0] == 101


def test_omitted_open_commitment_is_recoverable_through_registered_tools(system):
    for index in range(8):
        append(system, f"同时保留未完成任务项 {index}。", trace=f"task-{index}")
        replay(system, [{"op": "add", "reason": "new open commitment", "value": value(
            system, seq=3+2*index, kind="task", topic="open work", subject=f"item-{index}",
            content=f"同时保留未完成任务项 {index}。", priority=2)}])
    state = system.store.snapshot(system.session)
    view = render_handoff(state["records"], covered_seq=state["covered_seq"], budget=512)
    assert view["omitted_ids"]
    omitted = next(r for r in state["records"] if r["record_id"] in view["omitted_ids"])
    context = ToolContext(session_id=system.session, trace_id="readback")
    page = ReadContextRecordsTool(system.store).invoke(context=context, invocation=ToolInvocation(
        invocation_id="read", tool=ReadContextRecordsTool.spec, session_id=system.session, context_id="test",
        input={"query": omitted["subject"]}))
    assert page.status == "completed"
    assert page.output["records"][0]["record_id"] == omitted["record_id"]
    source = ReadContextSourceTool(system.store).invoke(context=context, invocation=ToolInvocation(
        invocation_id="source", tool=ReadContextSourceTool.spec, session_id=system.session, context_id="test",
        input={"seq": omitted["last_source_seq"]}))
    assert source.status == "completed" and source.output["content"] == omitted["content"]


def test_handoff_replay_prefix_discards_future_legacy_summary_without_touching_source(tmp_path, system):
    from scripts.eval_atomic_context import copy_session

    append(system, "第二轮内容", trace="turn-2")
    append(system, "第三轮内容", trace="turn-3")
    with system.factory() as conn:
        conn.execute("UPDATE agent_session_context_state SET covered_seq=6 WHERE session_id=?", (system.session,))
        conn.execute("UPDATE agent_session_context_windows SET summary='FUTURE-LEGACY-SUMMARY' WHERE session_id=?",
                     (system.session,))
    destination = tmp_path / "isolated-prefix.sqlite3"
    copy_session(system.db, system.session, destination, through_seq=2, reset_legacy=True)
    with connect(destination) as conn:
        state = conn.execute("SELECT revision,next_seq,covered_seq FROM agent_session_context_state WHERE session_id=?",
                             (system.session,)).fetchone()
        window = conn.execute("SELECT 1 FROM agent_session_context_windows WHERE session_id=?", (system.session,)).fetchone()
        count = conn.execute("SELECT count(*) FROM agent_session_context_messages WHERE session_id=?",
                             (system.session,)).fetchone()[0]
    assert tuple(state) == (0, 3, 0)
    assert window is None and count == 2
    with system.factory() as conn:
        assert conn.execute("SELECT covered_seq FROM agent_session_context_state WHERE session_id=?",
                            (system.session,)).fetchone()[0] == 6
        assert conn.execute("SELECT summary FROM agent_session_context_windows WHERE session_id=?",
                            (system.session,)).fetchone()[0] == "FUTURE-LEGACY-SUMMARY"

