"""Offline integration contracts for the opt-in conversation intent profile."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.background_jobs import BackgroundJobStore
from app.core.context_projection import render_context
from app.core.context_reconcile import delta_request, propose_delta, select_prefix, validate_delta
from app.core.context_record_models import IntentDelta
from app.core.context_sources import source_passages
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.prompt_tokens import PromptTokenCounter
from app.core.sessions import SessionService
from app.storage.context_records import ContextRecordStore
from app.storage.db import connect, init_db


@pytest.fixture
def system(tmp_path):
    db = tmp_path / "intent.sqlite3"
    init_db(db)
    factory = lambda: connect(db)
    sessions = SessionService(factory, context_token_counter=PromptTokenCounter())
    session = sessions.create_session(title="Intent test").session.session_id
    sessions.record_context_exchange(session_id=session, user_input="先聊课程 A。",
                                     agent_answer="可以。", trace_id="first")
    store = ContextRecordStore(factory)
    store.ensure_schema()
    return SimpleNamespace(db=db, factory=factory, sessions=sessions, session=session, store=store)


class Model:
    """Fixed offline model contract; no provider is called."""
    def __init__(self, *deltas):
        self.deltas = list(deltas)
        self.calls = []

    def complete_text(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=json.dumps(self.deltas.pop(0), ensure_ascii=False),
                               metadata={}, usage={})


def value(system, row, kind, content, *, evidence="user_statement"):
    passage = source_passages(system.session, row)[0]
    return {"kind": kind, "subject": kind, "topic": "session", "content": content,
            "evidence": evidence, "validity": "current", "priority": 3,
            "sources": [{"passage_id": passage["passage_id"]}]}


def change(system, row, kind, content, *, evidence="user_statement", record_id=None, revision=None):
    op = "update" if record_id else "add"
    result = {"op": op, "reason": "offline contract", "value": value(system, row, kind, content, evidence=evidence)}
    if record_id:
        result.update(record_id=record_id, expected_revision=revision)
    return result


def publish_changes(system, snapshot, changes):
    validated = validate_delta(snapshot, IntentDelta.model_validate({"changes": changes}), profile="intent")
    system.store.publish(snapshot, validated)


def seed_intent(system):
    snapshot = system.store.snapshot(system.session)
    publish_changes(system, snapshot, [change(system, snapshot["messages"][0], "intent", "课程 A 对话方向")])


def test_intent_schema_allows_only_two_current_slots_and_rejects_legacy_shapes():
    base = {"subject": "intent", "topic": "session", "content": "x", "evidence": "user_statement",
            "sources": [{"passage_id": "p_1_0_1_0000000000000000"}], "priority": 2}
    assert IntentDelta.model_validate({"changes": [{"op": "add", "reason": "x", "value": {**base, "kind": "intent"}}]})
    for invalid in (
        {**base, "kind": "task", "task_status": "active"},
        {**base, "kind": "intent", "content": "x" * 241},
        {**base, "kind": "dialogue", "content": "x" * 161, "evidence": "assistant_report"},
        {**base, "kind": "intent", "event_at": "2026-10-26"},
    ):
        with pytest.raises(ValidationError):
            IntentDelta.model_validate({"changes": [{"op": "add", "reason": "x", "value": invalid}]})
    two = [{"op": "add", "reason": "x", "value": {**base, "kind": kind}}
           for kind in ("intent", "dialogue")]
    with pytest.raises(ValidationError):
        IntentDelta.model_validate({"changes": two + two[:1]})


def test_proposal_is_conversation_only_and_review_sees_whole_batch(system):
    seed_intent(system)
    snapshot = system.store.snapshot(system.session)
    # Add legacy records to prove candidate filtering is type based, not validity based.
    rows = snapshot["messages"]
    legacy = {"kind": "task", "subject": "user", "topic": "old", "content": "OLD_TASK_SECRET",
              "evidence": "user_statement", "sources": [source_passages(system.session, rows[0])[0]],
              "priority": 3, "task_status": "active"}
    legacy["sources"] = [{"passage_id": source_passages(system.session, rows[0])[0]["passage_id"]}]
    # Stored records are local test setup; this intentionally models a migrated old profile.
    with system.factory() as conn:
        conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) VALUES(?,?,?,?,?,?)",
                     (system.session, 3, "tool", "BACKGROUND_INPUT_SECRET", "2026-01-01T00:00:00+00:00", "bg"))
        conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) VALUES(?,?,?,?,?,?)",
                     (system.session, 4, "agent", "BACKGROUND_ANSWER_SECRET", "2026-01-01T00:00:01+00:00", "bg"))
        conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) VALUES(?,?,?,?,?,?)",
                     (system.session, 5, "user", "REAL_NEW_USER", "2026-01-01T00:00:02+00:00", "real"))
        conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) VALUES(?,?,?,?,?,?)",
                     (system.session, 6, "agent", "REAL_NEW_ANSWER", "2026-01-01T00:00:03+00:00", "real"))
    snapshot = system.store.snapshot(system.session, target_seq=6)
    snapshot["records"].append({**legacy, "record_id": "legacy-task", "revision": 1,
                                "validity": "current", "last_source_seq": 1})
    snapshot["records"].append({**legacy, "kind": "progress", "record_id": "legacy-progress",
                                "content": "OLD_PROGRESS_SECRET", "revision": 1, "last_source_seq": 1})
    rows = [r for r in snapshot["messages"] if r["seq"] > snapshot["covered_seq"]]
    proposal = {"changes": []}
    model = Model(proposal, proposal)
    changes, response = propose_delta(model, snapshot, rows, input_tokens=12000,
                                      counter=PromptTokenCounter(), output_tokens=100, recovery_tokens=100,
                                      profile="intent")
    assert changes == [] and response is not None and len(model.calls) == 2
    for call in model.calls:
        prompt = call["user_prompt"]
        assert "BACKGROUND_INPUT_SECRET" not in prompt and "BACKGROUND_ANSWER_SECRET" not in prompt
        assert "OLD_TASK_SECRET" not in prompt and "OLD_PROGRESS_SECRET" not in prompt
        assert "REAL_NEW_USER" in prompt and "REAL_NEW_ANSWER" in prompt
    reviewed = json.loads(model.calls[1]["user_prompt"])
    assert reviewed["proposal"] == json.dumps(proposal, ensure_ascii=False)


def test_background_only_prefix_skips_model_and_publishes_watermark_without_changing_slot(system):
    seed_intent(system)
    before = system.store.snapshot(system.session)
    original = before["records"][0]
    with system.factory() as conn:
        for seq, role, content in ((3, "tool", "work input"), (4, "agent", "work answer")):
            conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) VALUES(?,?,?,?,?,?)",
                         (system.session, seq, role, content, f"2026-01-01T00:00:0{seq-3}+00:00", "work"))
    snapshot = system.store.snapshot(system.session, target_seq=4)
    messages = select_prefix(snapshot, target_seq=4, input_tokens=12000, profile="intent")
    model = Model()
    changes, response = propose_delta(model, snapshot, messages, input_tokens=12000,
                                      counter=PromptTokenCounter(), output_tokens=100, recovery_tokens=100,
                                      profile="intent")
    assert changes == [] and response is None and model.calls == []
    system.store.publish(snapshot, changes)
    after = system.store.snapshot(system.session)
    assert after["covered_seq"] == 4
    assert after["records"] == [original]


def test_validate_rejects_background_passage_and_wrong_latest_answer_trace(system):
    snapshot = system.store.snapshot(system.session)
    with system.factory() as conn:
        conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) VALUES(?,?,?,?,?,?)",
                     (system.session, 3, "user", "最新用户要求", "2026-01-02T00:00:00+00:00", "latest"))
        conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) VALUES(?,?,?,?,?,?)",
                     (system.session, 4, "agent", "旧trace回答", "2026-01-02T00:00:01+00:00", "first"))
    snapshot = system.store.snapshot(system.session, target_seq=4)
    old_answer = snapshot["messages"][1]
    wrong = change(system, old_answer, "dialogue", "已回应", evidence="assistant_report")
    with pytest.raises(ValueError, match="latest user's corresponding reply"):
        validate_delta(snapshot, IntentDelta.model_validate({"changes": [wrong]}), profile="intent")

    background = {"session_id": system.session, "seq": 6, "role": "agent", "content": "background",
                  "created_at": "2026-01-02T00:00:02+00:00", "trace_id": "background"}
    snapshot["messages"].append({**background, "seq": 5, "role": "tool"})
    snapshot["messages"].append(background)
    bad = change(system, background, "dialogue", "答复已完成", evidence="assistant_report")
    with pytest.raises(ValueError, match="authorized supplied input interval"):
        validate_delta(snapshot, IntentDelta.model_validate({"changes": [bad]}), profile="intent")


def test_update_keeps_only_current_slot_sources_and_preserves_old_revision_in_event(system):
    seed_intent(system)
    first = system.store.snapshot(system.session)
    current = first["records"][0]
    system.sessions.record_context_exchange(session_id=system.session, user_input="现在改聊课程 B。",
                                            agent_answer="明白，课程 B。", trace_id="second")
    second = system.store.snapshot(system.session)
    update = change(system, second["messages"][2], "intent", "改聊课程 B", record_id=current["record_id"], revision=current["revision"])
    publish_changes(system, second, [update])
    final = system.store.snapshot(system.session)
    updated = final["records"][0]
    assert updated["revision"] == 2 and [s["seq"] for s in updated["sources"]] == [3]
    with system.factory() as conn:
        event = json.loads(conn.execute("SELECT change_json FROM context_record_events WHERE session_id=? ORDER BY head_revision DESC LIMIT 1",
                                        (system.session,)).fetchone()[0])
    assert [s["seq"] for s in event["before"]["sources"]] == [1]


def test_prompt_context_backfills_real_exchange_and_work_lane_keeps_background_past_512_rows(system):
    seed_intent(system)
    system.sessions.atomic_context_store = system.store
    system.sessions.atomic_context_mode = "active"
    system.sessions.atomic_context_profile = "intent"
    # Put more than 512 later background rows between a conversation and the read.
    for i in range(260):
        system.sessions.record_context_exchange(session_id=system.session, user_input=f"BG_INPUT_{i}",
            agent_answer=f"BG_ANSWER_{i}", trace_id=f"bg-{i}", input_role="tool")
    system.sessions.record_context_exchange(session_id=system.session, user_input="仍在讨论课程 A 的例子。",
        agent_answer="我会解释这个例子。", trace_id="latest-user")
    front = system.sessions.get_prompt_context_window(session_id=system.session, token_budget=12000,
                                                       history_lane="conversation")
    assert {m.content for m in front.recent_messages} >= {"先聊课程 A。", "可以。", "仍在讨论课程 A 的例子。", "我会解释这个例子。"}
    assert all("BG_" not in m.content for m in front.recent_messages)
    assert front.summary_metadata["backfilled_messages"] == 2
    assert front.summary_metadata["raw_tail_messages"] == 2
    work = system.sessions.get_prompt_context_window(session_id=system.session, token_budget=12000,
                                                       history_lane="work")
    assert any("BG_" in m.content for m in work.recent_messages)
    assert len(work.recent_messages) <= 512


@pytest.mark.parametrize("withdrawn", [False, True])
def test_pending_or_withdrawn_intent_never_falls_back_to_legacy_summary(system, withdrawn):
    if withdrawn:
        seed_intent(system)
    with system.factory() as conn:
        conn.execute("UPDATE agent_session_context_windows SET summary='LEGACY_TASK_SUMMARY' WHERE session_id=?",
                     (system.session,))
    system.sessions.atomic_context_store = system.store
    system.sessions.atomic_context_mode = "active"
    system.sessions.atomic_context_profile = "intent"
    if withdrawn:
        with system.factory() as conn:
            conn.execute("UPDATE agent_session_context_messages SET content='edited source' WHERE session_id=? AND seq=1",
                         (system.session,))
    prompt = system.sessions.get_prompt_context_window(session_id=system.session, token_budget=12000)
    assert "LEGACY_TASK_SUMMARY" not in prompt.summary
    assert ("edited source" if withdrawn else "先聊课程 A。") in "\n".join(m.content for m in prompt.recent_messages)
    assert prompt.summary_metadata["method"] == "conversation_intent"
    assert prompt.summary_metadata["state_error"] == ("ContextRecordConflict" if withdrawn else "intent_state_pending")


def test_small_budget_keeps_only_real_history_suffix_without_generated_summary(system):
    seed_intent(system)
    system.sessions.atomic_context_store = system.store
    system.sessions.atomic_context_mode = "active"
    system.sessions.atomic_context_profile = "intent"
    system.sessions.record_context_exchange(session_id=system.session, user_input="较早" + "甲" * 800,
        agent_answer="较早答复" + "乙" * 800, trace_id="long-1")
    system.sessions.record_context_exchange(session_id=system.session, user_input="最新要求" + "丙" * 800,
        agent_answer="最新回答" + "丁" * 800, trace_id="long-2")
    prompt = system.sessions.get_prompt_context_window(session_id=system.session, token_budget=1000)
    assert prompt.token_estimate <= 1000
    assert prompt.summary_metadata["history_rows_omitted_by_budget"] > 0
    assert prompt.summary == "" or "当前对话方向" in prompt.summary
    assert all(message.content.startswith(("较早", "较早答复", "最新要求", "最新回答", "先聊课程 A。", "可以。"))
               for message in prompt.recent_messages)
    assert not any(message.content.startswith("user:") or message.content.startswith("agent:")
                   for message in prompt.recent_messages)


def test_background_coordinator_wires_configured_intent_budget(system):
    queue = BackgroundJobStore(system.db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(system.db), memory=None, store=queue,
        session_service=system.sessions, atomic_context_mode="active", atomic_context_profile="intent",
        atomic_context_intent_tokens=512)
    assert coordinator.atomic_context_profile == "intent"
    assert coordinator.session_service.atomic_context_intent_tokens == 512


def test_intent_slots_do_not_become_general_facts_on_profile_switch(system):
    seed_intent(system)
    snapshot = system.store.snapshot(system.session)
    prompt = json.loads(delta_request(snapshot, snapshot["messages"], profile="general"))
    assert prompt["existing_records"] == []
    projected = render_context(snapshot["records"], covered_seq=2, budget=4096)
    assert "课程 A 对话方向" not in projected["text"]
