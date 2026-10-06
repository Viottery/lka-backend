from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domains.message_participant_profiles import ParticipantProfileIndex
from app.domains.message_profile_documents import MessageProfileDocumentStore
from app.tool_packages.message_reading_analysis import ScopedProjection, verify_intelligence
from app.tool_packages.message_replay_analysis import ReplayAnalysis, validate_evidence


def row(alias, text, time, *, reply=None, sender="person"):
    return {"id": alias, "sender": sender, "text": text, "kind": "text", "sent_at": time,
            "received_at": time, "seq": time + 1, "reply": reply, "parts": []}


def observation():
    return {"sender": "person", "kind": "communication_style", "text": "本批回复中先解释原因再给建议",
            "source_ids": ["m1", "m2"], "quote": "先看错误原因",
            "evidence_quotes": {"m1": "先看错误原因", "m2": "原因是配置没有加载"},
            "basis": "observed", "valid_until": None}


def evidence():
    return [row("m1", "先看错误原因，再重试", 0, reply="other"),
            row("m2", "原因是配置没有加载，建议重新配置", 1800, reply="another")]


def test_behavior_uses_distinct_authored_reply_quotes_and_neutral_summary():
    index = ParticipantProfileIndex("group")
    assert index.add_claim(observation(), evidence(), 1800)
    index.pin("person", True, expected_revision=1)
    assert index.profile("person", 1800)["summary"] == observation()["text"]
    assert not index.add_claim(observation(), [evidence()[0], row("m2", evidence()[1]["text"], 1)], 1800)
    assert index.last_rejection_reason == "observed_requires_independent_windows"
    assert not index.add_claim({**observation(), "text": "这个人性格很好"}, evidence(), 1800)
    assert index.last_rejection_reason == "sensitive_or_personality_claim"


def test_self_report_role_experience_context_and_unknown_are_separate():
    index = ParticipantProfileIndex("group")
    for kind, text in [("role", "我是项目维护者"), ("experience", "我曾维护过这个项目"),
                       ("context_event", "我刚换了一台电脑")]:
        claim = {"sender": "person", "kind": kind, "text": text, "source_ids": ["m1"],
                 "quote": text, "basis": "explicit", "valid_until": None}
        assert index.add_claim(claim, [row("m1", text, 0)], 0)
        assert not index.add_claim({**claim, "basis": "observed", "source_ids": ["m1", "m2"]},
                                   [row("m1", text, 0), row("m2", text, 1800)], 1800)
        assert index.last_rejection_reason == "self_report_requires_explicit_basis"


def test_durable_store_revisions_sources_notes_sparse_authors_and_epochs(tmp_path):
    store = MessageProfileDocumentStore(tmp_path)
    rows = evidence() + [row("sparse", "你好", 1800, sender="sparse")]
    assert store.ingest("group", [observation()], rows, 1800, capture_epoch=1)["accepted"] == 1
    snapshot = store.snapshot("group", capture_epoch=1)
    person = next(value for value in snapshot["participants"] if value["sender"] == "person")
    assert person["source_range"] == {"first_time": 0, "last_time": 1800, "first_seq": 1, "last_seq": 1801}
    assert person["evidence"]["m2"]["text"] == rows[1]["text"]
    assert snapshot["participants"][1]["status"] == "insufficient_information"
    paths = store.export("group", capture_epoch=1, display_names={"person": "Nickname"})
    notes = Path(paths["participants"]["person"]["notes"])
    notes.write_text("Human-maintained context", encoding="utf-8")
    assert "Nickname" in Path(paths["markdown"]).read_text(encoding="utf-8")
    reopened = MessageProfileDocumentStore(tmp_path)
    assert reopened.ingest("group", [observation()], rows, 1800, capture_epoch=1)["accepted"] == 0
    second = reopened.export("group", capture_epoch=1)
    assert notes.read_text(encoding="utf-8") == "Human-maintained context"
    assert Path(paths["json"]).exists() and paths["json"] != second["json"]
    reopened.suppress("group", "person", capture_epoch=1)
    assert reopened.ingest("group", [observation()], rows, 1800, capture_epoch=2)["accepted"] == 0
    assert all(person["sender"] != "person" for person in reopened.snapshot("group", capture_epoch=2)["participants"])
    with pytest.raises(PermissionError, match="stale_profile_capture_epoch"):
        reopened.export("group", capture_epoch=1)
    assert notes.read_text(encoding="utf-8") == "Human-maintained context"
    current = next(tmp_path.glob("*/current.json"))
    assert json.loads(current.read_text())["capture_epoch"] == 1


def test_native_controls_hide_reversibly_and_suppression_cannot_be_replayed(tmp_path):
    store = MessageProfileDocumentStore(tmp_path)
    claim = {"sender": "person", "kind": "preference", "text": "我喜欢工具",
             "source_ids": ["m1"], "quote": "我喜欢工具", "basis": "explicit",
             "valid_until": None}
    messages = [row("m1", "我喜欢工具", 10)]
    store.ingest("group", [claim], messages, 10, capture_epoch=1)
    store.export("group", capture_epoch=1)
    store.apply_controls("group", hidden_senders={"person"}, suppressed_senders=set(), capture_epoch=1)
    store.export("group", capture_epoch=1)
    assert store.snapshot("group", capture_epoch=1)["participants"] == []
    store.apply_controls("group", hidden_senders=set(), suppressed_senders=set(), capture_epoch=1)
    assert store.snapshot("group", capture_epoch=1)["participants"][0]["claims"]
    store.apply_controls("group", hidden_senders=set(), suppressed_senders={"person"}, capture_epoch=1)
    replay = store.ingest("group", [claim], messages, 20, capture_epoch=1)
    assert replay["accepted"] == 0
    assert store.snapshot("group", capture_epoch=1)["participants"] == []


def test_replay_schema_keeps_v3_and_validates_each_source_quote():
    result = ReplayAnalysis.model_validate({"schema_version": 3, "topics": [], "highlights": [],
        "importance": [], "participant_claim_candidates": [observation()], "focus_candidates": [], "warnings": []})
    assert validate_evidence(result, evidence()) == []
    result.participant_claim_candidates[0].evidence_quotes["m2"] = "invented"
    assert validate_evidence(result, evidence()) == ["quote_not_in_evidence"]


def test_production_per_source_quotes_are_restored_to_private_stable_ids():
    originals = [{"message_id": alias, "sender_id": "person", "seq": time + 1,
                  "sent_at": time, "received_at": time, "text": text, "content_kind": "text"}
                 for alias, text, time in [("m1", "先看错误原因", 0),
                                            ("m2", "原因是配置没有加载", 1800)]]
    projection = ScopedProjection("group", originals)
    first, second = projection.messages
    claim = {**observation(), "sender": first["sender"], "source_ids": [first["id"], second["id"]],
             "evidence_quotes": {first["id"]: first["text"], second["id"]: second["text"]}}
    result = verify_intelligence({"participant_claim_candidates": [claim], "focus_candidates": []},
                                 projection.messages, projection)
    assert result["participant_claim_candidates"][0]["sender"] == "person"
    assert result["participant_claim_candidates"][0]["evidence_quotes"] == observation()["evidence_quotes"]


def test_rolling_live_claims_facets_and_bounded_history(tmp_path):
    store = MessageProfileDocumentStore(tmp_path, history_limit=14, revision_limit=2)
    for number in range(16):
        text = f"我喜欢工具{number}"
        claim = {"sender": "person", "kind": "preference", "facet": f"tool-{number}",
            "text": text, "quote": text, "source_ids": [f"m{number}"], "basis": "explicit",
            "valid_until": 2000}
        assert store.ingest("group", [claim], [row(f"m{number}", text, number)], number)["accepted"] == 1
        store.export("group")
    person = store.snapshot("group")["participants"][0]
    assert len(person["claims"]) == 14
    assert person["history_pruned_count"] == 2
    assert sum(value["status"] == "archived" for value in person["claims"]) == 2
    assert sum(value["status"] == "candidate" for value in person["claims"]) == 12
    assert len(person["evidence"]) == 14
    assert len(list(tmp_path.glob("*/revisions/*"))) == 2
    assert all(value["status"] in ("archived", "stale")
               for value in store.snapshot("group", now=2000)["participants"][0]["claims"])


def test_tentative_behavior_and_escaped_immutable_exports(tmp_path):
    index = ParticipantProfileIndex("group")
    rows = [evidence()[0], {**evidence()[1], "sent_at": 1, "received_at": 1}]
    claim = {**observation(), "basis": "uncertain", "text": "[link](https://host) <img src=x>"}
    assert index.add_claim(claim, rows, 1)
    assert not index.add_claim({**claim, "source_ids": ["m1"], "evidence_quotes": {"m1": claim["quote"]}}, rows, 1)
    store = MessageProfileDocumentStore(tmp_path)
    store.ingest("group", [claim], rows, 1)
    first = store.export("group", display_names={"person": "First"})
    second = store.export("group", display_names={"person": "Second"})
    assert first["json"] != second["json"]
    markdown = Path(first["markdown"]).read_text()
    assert "\\[link\\]\\(https://host\\)" in markdown
    assert "<img" not in markdown and "&lt;img" in markdown


def test_resume_is_idempotent_and_does_not_rewind_or_reorder(tmp_path):
    store = MessageProfileDocumentStore(tmp_path)
    first = store.ingest("group", [observation()], evidence(), 3600)
    repeated = store.ingest("group", [observation()], evidence(), 1800)
    assert repeated["revision"] == first["revision"]
    assert store.snapshot("group")["updated_at"] == 3600
    text = "我是项目维护者"
    older = {"sender": "person", "kind": "role", "text": text, "source_ids": ["older"],
             "quote": text, "basis": "explicit", "valid_until": None}
    store.ingest("group", [older], [row("older", text, 0)], 0)
    snapshot = store.snapshot("group")
    assert snapshot["updated_at"] == 3600
    assert [claim["created_at"] for claim in snapshot["participants"][0]["claims"]] == [0, 3600]
    paths = store.export("group", display_names={"person": "群友"}, now=40 * 86400)
    markdown = Path(paths["markdown"]).read_text()
    assert "群友（本地真实 ID：person）" in markdown
    assert "### 沟通方式" in markdown and "### 本人自述角色" in markdown
    assert "原文 m2" in markdown and "原因是配置没有加载" in markdown
    assert "1970-01-01 08:30:00（上海时间）" in markdown
    assert "已过期（stale）" in markdown


def test_unverified_machine_notes_are_opt_in_and_never_accepted_claims(tmp_path):
    text = "我喜欢 Python"
    claim = {"sender": "person", "kind": "preference", "text": "偏好 Python 编程",
             "quote": text, "source_ids": ["source"], "basis": "explicit", "valid_until": None}
    messages = [row("source", text, 0)]
    strict = MessageProfileDocumentStore(tmp_path / "strict")
    assert strict.ingest("group", [claim], messages, 1)["retained_notes"] == 0
    assert strict.snapshot("group")["participants"][0]["status"] == "insufficient_information"
    store = MessageProfileDocumentStore(tmp_path / "opted-in", retain_unverified_notes=True)
    result = store.ingest("group", [claim], messages, 1)
    assert result["accepted"] == 0 and result["retained_notes"] == 1
    person = store.snapshot("group")["participants"][0]
    assert person["claims"] == [] and person["status"] == "needs_review"
    note = person["machine_notes"][0]
    assert note["proposed_basis"] == "explicit" and "basis" not in note
    assert note["proposed_kind"] == "preference" and note["rejection_reason"] == "unverified_paraphrase"
    again = store.ingest("group", [claim], messages, 0)
    assert again["revision"] == result["revision"] and again["retained_notes"] == 0
    paths = store.export("group")
    markdown = Path(paths["markdown"]).read_text()
    assert "### 待核实机器摘记" in markdown and "绝非已接受画像" in markdown
    assert "我喜欢 Python" in markdown
    store.suppress("group", "person")
    assert store.ingest("group", [claim], messages, 1)["retained_notes"] == 0
    assert store.snapshot("group")["participants"] == []


@pytest.mark.parametrize("mutate", [
    {"sender": "other"}, {"quote": "invented quote"}, {"text": "这个人性格很好"},
])
def test_machine_notes_cannot_bypass_source_or_sensitive_guards(tmp_path, mutate):
    claim = {"sender": "person", "kind": "preference", "text": "偏好 Python 编程",
             "quote": "我喜欢 Python", "source_ids": ["source"], "basis": "explicit", "valid_until": None}
    store = MessageProfileDocumentStore(tmp_path, retain_unverified_notes=True)
    result = store.ingest("group", [{**claim, **mutate}], [row("source", "我喜欢 Python", 0)], 1)
    assert result["accepted"] == result["retained_notes"] == 0
    assert not any(value.get("machine_notes") for value in store.snapshot("group")["participants"])


def test_machine_notes_roll_archive_and_epoch_clears_them(tmp_path):
    store = MessageProfileDocumentStore(tmp_path, retain_unverified_notes=True, history_limit=14)
    for number in range(16):
        text = f"我喜欢工具{number}"
        claim = {"sender": "person", "kind": "preference", "text": f"可能偏好工具{number}",
                 "quote": text, "source_ids": [str(number)], "basis": "explicit", "valid_until": None}
        assert store.ingest("group", [claim], [row(str(number), text, number)], number)["retained_notes"] == 1
    person = store.snapshot("group")["participants"][0]
    assert len(person["machine_notes"]) == 14 and len(person["evidence"]) == 14
    assert sum(note["status"] == "needs_review" for note in person["machine_notes"]) == 12
    assert person["machine_notes_pruned_count"] == 2
    store.ingest("group", [], [row("new", "你好", 20)], 20, capture_epoch=1)
    assert store.snapshot("group", capture_epoch=1)["participants"][0]["machine_notes"] == []


def test_machine_note_expiry_preserves_historical_context_not_current_need(tmp_path):
    store = MessageProfileDocumentStore(tmp_path, retain_unverified_notes=True)
    text = "我需要新电脑"
    claim = {"sender": "person", "kind": "need", "text": "正在寻找电脑",
             "quote": text, "source_ids": ["source"], "basis": "explicit", "valid_until": 10}
    assert store.ingest("group", [claim], [row("source", text, 0)], 20)["retained_notes"] == 1
    note = store.snapshot("group", now=20)["participants"][0]["machine_notes"][0]
    assert note["status"] == "stale" and note["expires_at"] == 10
    rendered = Path(store.export("group", now=20)["markdown"]).read_text()
    assert "已过期机器摘记（stale）" in rendered and "有效至：" in rendered
    assert store.ingest("group", [{**claim, "valid_until": -1}], [row("source", text, 0)], 20)["retained_notes"] == 0


def test_sensitive_sexual_characterizations_blocked_without_blocking_technical_xp(tmp_path):
    store = MessageProfileDocumentStore(tmp_path, retain_unverified_notes=True)
    for index, term in enumerate(["性癖", "性偏好", "恋童", "萝莉控", "正太控", "性欲", "性幻想"]):
        text = f"我喜欢讨论{term}"
        claim = {"sender": "person", "kind": "preference", "text": text, "quote": text,
                 "source_ids": [str(index)], "basis": "explicit", "valid_until": None}
        result = store.ingest("group", [claim], [row(str(index), text, index)], index)
        assert result["accepted"] == result["retained_notes"] == 0
        assert result["rejected"][0]["reason"] == "sensitive_or_personality_claim"
    text = "我喜欢 Windows XP"
    claim = {"sender": "person", "kind": "preference", "text": text, "quote": text,
             "source_ids": ["xp"], "basis": "explicit", "valid_until": None}
    assert store.ingest("group", [claim], [row("xp", text, 10)], 10)["accepted"] == 1


def test_preexisting_forbidden_inferences_filtered_from_current_dossier(tmp_path):
    store = MessageProfileDocumentStore(tmp_path, retain_unverified_notes=True)
    text = "我喜欢 Python"
    claim = {"sender": "person", "kind": "preference", "text": text, "quote": text,
             "source_ids": ["safe"], "basis": "explicit", "valid_until": None}
    store.ingest("group", [claim], [row("safe", text, 0)], 0)
    paths = store.export("group")
    notes = Path(paths["participants"]["person"]["notes"])
    notes.write_text("Human text must remain", encoding="utf-8")
    state_path = next(tmp_path.glob("*/state.json"))
    state = json.loads(state_path.read_text())
    person = state["participants"]["person"]
    unsafe = {**person["claims"][0], "text": "自称萝莉控", "quote": "我是萝莉控",
              "source_ids": ["unsafe"], "claim_id": "unsafe_claim"}
    person["claims"].append(unsafe)
    person["machine_notes"] = [{"text": unsafe["text"], "quote": unsafe["quote"],
        "evidence_quotes": {}, "source_ids": ["unsafe"], "note_id": "unsafe_note"}]
    person["evidence"]["unsafe"] = row("unsafe", unsafe["quote"], 0)
    state_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    snapshot = store.snapshot("group")
    assert snapshot["privacy_filtered_count"] == 2
    assert len(snapshot["participants"][0]["claims"]) == 1
    assert snapshot["participants"][0]["machine_notes"] == []
    assert set(snapshot["participants"][0]["evidence"]) == {"safe"}
    refreshed = store.export("group")
    assert "萝莉控" not in Path(refreshed["markdown"]).read_text()
    assert "萝莉控" not in Path(refreshed["json"]).read_text()
    store.ingest("group", [], [], 1)
    assert json.loads(state_path.read_text())["privacy_filtered_count"] == 2
    assert notes.read_text() == "Human text must remain"
