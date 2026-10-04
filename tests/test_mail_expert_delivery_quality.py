from app.experts.mail import MailExpertExecutor


def _analysis(index, *, action=True, priority="low"):
    return {"message_id": f"message-{index}", "summary": f"Renew document REQUEST-{index} before next month.",
            "priority": priority, "reason": "Required but not urgent.", "action_required": action}


def test_low_urgency_does_not_hide_required_action_from_parent():
    coverage = {"local_total": 1, "body_loaded": 1}
    missing = []
    summary = MailExpertExecutor._review_summary([{}], [_analysis(1)], coverage, missing)
    assert "REQUEST-1" in summary
    assert coverage["action_required_total"] == 1
    assert coverage["action_items_shown"] == 1
    assert missing == []


def test_bounded_handoff_reports_omitted_actions_instead_of_complete_delivery():
    coverage = {"local_total": 18, "body_loaded": 18}
    missing = []
    summary = MailExpertExecutor._review_summary([{}] * 18, [_analysis(i) for i in range(18)], coverage, missing)
    assert coverage["action_required_total"] == 18
    assert coverage["action_items_shown"] == 12
    assert len(summary) <= 3500
    assert any("6" in reason and "handoff" in reason for reason in missing)
    assert "未完成范围" in summary
