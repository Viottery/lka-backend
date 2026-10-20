from __future__ import annotations

import json
import sqlite3

import pytest

from app.domains.memory import MemoryConflictError, MemoryInput, MemoryService, MemorySourceInput
from app.domains.memory_feedback import MemoryFeedbackInput


def service(tmp_path):
    result = MemoryService(tmp_path / "memory.sqlite3")
    result.ensure_schema()
    return result


def source(service, name):
    return service.register_source(MemorySourceInput(
        source_type="test", source_ref=name, trusted_source=True,
    ))


def create(service, name, content="偏好简短回答。", *, memory_type="preference",
           scope="global", project_id=None, sensitivity="normal", metadata=None):
    return service.create(MemoryInput(
        content=content, source_id=source(service, name), memory_type=memory_type,
        scope=scope, project_id=project_id, sensitivity=sensitivity,
        confidence=0.9, extraction_model="test-model", metadata=metadata or {},
    ))


def test_feedback_is_explicit_and_correct_is_atomic(tmp_path):
    store = service(tmp_path)
    old = create(store, "one")
    changed = store.correct(
        old.memory_id, content="偏好简短中文回答。", expected_version=old.version,
        user_feedback=MemoryFeedbackInput(origin="user_message", reason="wording"),
    )
    feedback = store.feedbacks(scope="global")
    assert len(feedback) == 1
    assert (feedback[0].action, feedback[0].old_memory_id, feedback[0].old_version) == (
        "correct", old.memory_id, 1,
    )
    assert (feedback[0].new_memory_id, feedback[0].new_version) == (changed.memory_id, 1)
    assert feedback[0].old_content == old.content
    assert feedback[0].new_content == changed.content
    assert feedback[0].origin == "user_message"

    plain = create(store, "two", "日常偏好代码示例简洁。")
    store.correct(plain.memory_id, content="日常偏好简洁代码示例。", expected_version=1)
    assert len(store.feedbacks(scope="global")) == 1


def test_conflict_and_transaction_failure_do_not_write_feedback(tmp_path):
    store = service(tmp_path)
    old = create(store, "one")
    feedback = MemoryFeedbackInput(reason="factual_error")
    with pytest.raises(MemoryConflictError):
        store.correct(old.memory_id, content="修正", expected_version=2, user_feedback=feedback)
    assert store.feedbacks(scope="global") == []

    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("""CREATE TRIGGER fail_feedback BEFORE INSERT ON memory_feedback
            BEGIN SELECT RAISE(ABORT,'feedback failure'); END""")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        store.correct(old.memory_id, content="修正", expected_version=1, user_feedback=feedback)
    assert store.get(old.memory_id).status == "active"
    assert store.get(old.memory_id).version == 1


def test_do_not_store_redacts_entire_correction_chain(tmp_path):
    store = service(tmp_path)
    first = create(store, "one", "敏感偏好原话", sensitivity="normal")
    second = store.correct(
        first.memory_id, content="替换后的偏好", expected_version=1,
        user_feedback=MemoryFeedbackInput(reason="wording"),
    )
    store.retract(
        second.memory_id, expected_version=1,
        user_feedback=MemoryFeedbackInput(reason="do_not_store", origin="user_api"),
    )
    records = store.feedbacks(scope="global")
    assert all(row.redacted and row.old_content is None and row.new_content is None for row in records)
    assert store.relevant_feedbacks("偏好", scope="global") == []
    assert store.get(second.memory_id).status == "retracted"


def test_personal_content_and_do_not_store_correct_are_not_retained(tmp_path):
    store = service(tmp_path)
    old = create(store, "one", "我的家庭地址", sensitivity="personal")
    store.retract(old.memory_id, expected_version=1,
                  user_feedback=MemoryFeedbackInput(reason="unspecified"))
    feedback = store.feedbacks(scope="global")[0]
    assert feedback.redacted and feedback.old_content is None
    with pytest.raises(ValueError, match="only be used when retracting"):
        store.correct(old.memory_id, content="错误版本", expected_version=2,
                      user_feedback=MemoryFeedbackInput(reason="do_not_store"))


def test_relevant_examples_recheck_live_sources_and_exclude_sensitive_types(tmp_path):
    store = service(tmp_path)
    entry = create(store, "source", "偏好中文简洁回答。")
    store.correct(entry.memory_id, content="中文回答保持简洁。", expected_version=1,
                  user_feedback=MemoryFeedbackInput(reason="wording"))
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE memory_sources SET source_type='user_api'")
        conn.execute("UPDATE memory_entries SET sensitivity='secret'")
        conn.commit()
    finally:
        conn.close()
    assert store.relevant_feedbacks("中文简洁回答", scope="global") == []

    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE memory_entries SET sensitivity='normal'")
        conn.execute("UPDATE memory_sources SET status='revoked'")
        conn.commit()
    finally:
        conn.close()
    assert store.relevant_feedbacks("中文简洁回答", scope="global") == []


def test_relevant_feedback_is_scoped_ranked_and_complete_json_bounded(tmp_path):
    store = service(tmp_path)
    project_a, project_b = "project-a", "project-b"
    conn = sqlite3.connect(store.db_path)
    try:
        conn.executemany(
            "INSERT INTO memory_projects(project_id,created_at) VALUES(?,?)",
            [(project_a, "2026-01-01"), (project_b, "2026-01-01")],
        )
        conn.commit()
    finally:
        conn.close()
    a = create(store, "a", "回答尽量简短，使用中文要点。", scope="project", project_id=project_a)
    store.correct(a.memory_id, content="回答中文时简短列出要点。", expected_version=1,
                  user_feedback=MemoryFeedbackInput(reason="wording"))
    b = create(store, "b", "回答尽量简短，使用英文要点。", scope="project", project_id=project_b)
    store.correct(b.memory_id, content="回答英文时简短列出要点。", expected_version=1,
                  user_feedback=MemoryFeedbackInput(reason="wording"))

    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE memory_sources SET source_type='user_api' WHERE source_ref='a'")
        conn.execute("UPDATE memory_sources SET source_type='user_api' WHERE source_ref='b'")
        conn.commit()
    finally:
        conn.close()
    selected = store.relevant_feedbacks("请用中文简短回答", scope="project", project_id=project_a)
    assert len(selected) == 1
    assert selected[0]["after"] == "回答中文时简短列出要点。"
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(encoded) <= 2000
    assert store.relevant_feedbacks("请用中文简短回答", scope="project", project_id=project_b)[0]["after"].startswith("回答英文")
    with pytest.raises(ValueError):
        store.relevant_feedbacks("x", scope="project", project_id=project_a, max_json_chars=2001)


def test_grounding_keeps_only_small_structured_references(tmp_path):
    store = service(tmp_path)
    old = create(store, "one", "喜欢使用表格", metadata={
        "grounding": {"source_ids": ["src1"], "claim_id": "c1", "quote": "private source text"},
    })
    store.correct(old.memory_id, content="需要对比时喜欢使用表格", expected_version=1,
                  user_feedback=MemoryFeedbackInput(reason="factual_error"))
    record = store.feedbacks(scope="global")[0]
    assert record.grounding == {"source_ids": ["src1"], "claim_id": "c1"}
    assert "quote" not in json.dumps(record.grounding)


def test_schema_is_idempotent_and_feedback_pages_validate(tmp_path):
    store = service(tmp_path)
    store.ensure_schema()
    store.ensure_schema()
    with pytest.raises(ValueError):
        store.feedbacks(scope="project", limit=0, project_id="p")
    with pytest.raises(ValueError):
        store.feedbacks(scope="project", project_id=None)


def test_contextual_grounding_mode_is_recorded_without_source_body(tmp_path):
    store = service(tmp_path)
    old = create(store, "contextual", "喜欢简短回答", metadata={
        "grounding": "confirmed", "source_body": "不应复制的会话原文",
    })
    store.correct(old.memory_id, content="喜欢先给结论再按需展开", expected_version=1,
                  user_feedback=MemoryFeedbackInput(reason="wording"))
    assert store.feedbacks(scope="global")[0].grounding == {"mode": "confirmed"}
