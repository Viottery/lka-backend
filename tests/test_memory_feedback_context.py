"""Bounded feedback guides memory decisions without becoming source evidence."""

import json

from test_contextual_memory import Extractor, candidate, user
from test_contextual_memory import runtime as runtime  # noqa: PLC0414
from test_memory_feedback import create

from app.core.memory_context import MemoryPreTurnGate
from app.core.memory_learning import CONTEXTUAL_MEMORY_PROMPT
from app.domains.memory_feedback import MemoryFeedbackInput


def test_preference_feedback_enters_existing_call_without_becoming_evidence(runtime):
    service = runtime.memory_service
    old = create(service, "old", "偏好中文简短回答。")
    service.correct(old.memory_id, content="偏好中文简短回答，保留必要细节。",
                    expected_version=1, user_feedback=MemoryFeedbackInput(reason="wording"))
    anchor = user(runtime, "以后我希望中文回答简短一点，但保留必要细节")
    client = Extractor(lambda p: [])
    records, _ = runtime.memory_background.learning.organize(anchor, client=client)
    assert records == [] and len(client.prompts) == 1
    feedback = client.prompts[0]["memory_feedback"]
    assert len(feedback) == 1
    assert feedback[0]["reason"] == "wording" and feedback[0]["scope"] == "global"
    assert "message_id" not in feedback[0] and "source_id" not in feedback[0]
    assert "cannot by itself justify a candidate" in CONTEXTUAL_MEMORY_PROMPT

    # Even a model that treats historical text as evidence cannot publish it.
    client.build = lambda p: [candidate(p, "偏好中文简短回答。", evidence=[
        {"message_id": "historical-feedback", "quote": p["memory_feedback"][0]["after"]},
    ])]
    assert runtime.memory_background.learning.organize(anchor, client=client)[0] == []
    assert len(service.feedbacks(scope="global")) == 1


def test_feedback_context_has_shared_project_and_character_budget(runtime, monkeypatch):
    calls = []
    def related(query, *, scope, project_id):
        calls.append((scope, project_id))
        return [{"action": "correct", "reason": "wording", "before": "偏好" * 130,
                 "after": f"中文{index}" * 90} for index in range(3)]
    monkeypatch.setattr(runtime.memory_service, "relevant_feedbacks", related)
    feedback = runtime.memory_background.learning._feedback_context(["偏好中文回答"], "project-a")
    assert calls == [("project", "project-a"), ("global", None)]
    assert 0 < len(feedback) <= 3
    assert len(json.dumps(feedback, ensure_ascii=False)) <= 2000
    assert {f["scope"] for f in feedback} <= {"project", "global"}


def test_sensitive_and_do_not_store_feedback_never_enters_model_context(runtime):
    service = runtime.memory_service
    secret = create(service, "personal", "偏好中文简短回答。", sensitivity="personal")
    service.correct(secret.memory_id, content="偏好中文简短回答，个人私事。", expected_version=1,
                    user_feedback=MemoryFeedbackInput(reason="wording"))
    old = create(service, "normal", "偏好中文回答精简。")
    changed = service.correct(old.memory_id, content="偏好中文简短回答，保留细节。", expected_version=1,
                              user_feedback=MemoryFeedbackInput(reason="wording"))
    service.retract(changed.memory_id, expected_version=1,
                    user_feedback=MemoryFeedbackInput(reason="do_not_store"))
    assert runtime.memory_background.learning._feedback_context(["偏好中文简短回答"], None) == []


def test_explicit_user_message_and_file_edits_record_their_origins(runtime):
    service = runtime.memory_service
    old = create(service, "message", "偏好很长的中文回答。")
    assert MemoryPreTurnGate(service)(None, f"忘记 {old.memory_id}") == [old.memory_id]
    message_feedback = service.feedbacks(scope="global")[0]
    assert (message_feedback.action, message_feedback.origin, message_feedback.reason) == (
        "discard", "user_message", "unspecified",
    )
    file_memory = create(service, "file", "偏好回答先提供结论。")
    path = runtime.memory_files.generate(scope="global")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(file_memory.content, "偏好回答先提供结论，再说明依据。"), encoding="utf-8")
    runtime.memory_files.import_edits(scope="global")
    file_feedback = next(f for f in service.feedbacks(scope="global") if f.origin == "user_memory_file")
    assert (file_feedback.action, file_feedback.old_memory_id, file_feedback.reason) == (
        "correct", file_memory.memory_id, "unspecified",
    )
