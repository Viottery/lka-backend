"""Bounded user-decision handoffs expose neither prompts nor approval authority."""
from app.domains.managed_tasks import ManagedTaskService


def record(status="waiting_user", **metadata):
    return {"run_id": "run_waiting", "session_id": "session_task", "status": status,
            "metadata": {"full_prompt": "PRIVATE_PROMPT", "tool_input": {"secret": "PRIVATE_TOOL"}, **metadata},
            "user_input": "PRIVATE_USER_INPUT", "result_snapshot": {"answer": "PRIVATE_RESULT"}}


def test_waiting_user_exposes_bounded_question_and_original_continue_entry():
    view = ManagedTaskService._run_view(record(pending_user_question={
        "question_id": "q-current", "patch_id": "patch-current", "question": "决定" * 3000,
        "asked_at": "t" * 100, "raw_prompt": "PRIVATE_NESTED_PROMPT", "tool_input": {"secret": "PRIVATE_TOOL"},
    }))
    assert view["pending_user_question"] == {"question_id": "q-current", "patch_id": "patch-current",
                                             "question": "决定" * 2000, "asked_at": "t" * 80}
    assert view["user_control"] == {"session_id": "session_task", "run_id": "run_waiting",
                                    "continue_path": "/agent/runs/run_waiting/continue"}
    assert "metadata" not in view and "user_input" not in view and "result_snapshot" not in view
    assert "PRIVATE" not in repr(view)


def test_invalid_question_is_never_promoted_to_actionable_user_decision():
    for question in (None, "bad", {}, {"question_id": " ", "question": "Choose"},
                     {"question_id": "q", "question": " \n "}, {"question_id": 3, "question": "Choose"},
                     {"question_id": "q", "question": {"prompt": "PRIVATE"}},
                     {"question_id": "q" * 257, "question": "Choose"}):
        view = ManagedTaskService._run_view(record(pending_user_question=question))
        assert "pending_user_question" not in view
        assert "user_control" not in view
    view = ManagedTaskService._run_view(record(pending_user_question={
        "question_id": "q", "question": "Choose", "patch_id": "p" * 257, "asked_at": {"private": "prompt"},
    }))
    assert view["pending_user_question"] == {"question_id": "q", "question": "Choose"}


def test_normal_and_terminal_states_do_not_expose_stale_decisions():
    for status in ("queued", "running", "paused", "completed", "failed", "cancelled", "timed_out"):
        view = ManagedTaskService._run_view(record(status, pending_user_question={"question_id": "old", "question": "Obsolete"}))
        assert "pending_user_question" not in view and "user_control" not in view
    assert "user_control" not in ManagedTaskService._run_view({"status": "waiting_user", "metadata": None})


def test_confirmation_handoff_requires_original_approval_without_tool_inputs():
    view = ManagedTaskService._run_view(record("waiting_confirmation", pending_user_question={
        "question_id": "stale", "question": "Obsolete user question", "approval_id": "PRIVATE_APPROVAL",
    }, pending_approval={"tool_input": "PRIVATE_APPROVAL_BODY"}))
    assert view["user_control"] == {"session_id": "session_task", "run_id": "run_waiting", "requires_original_approval": True}
    assert "pending_user_question" not in view
    assert "PRIVATE" not in repr(view)
    assert "continue_path" not in view["user_control"]
