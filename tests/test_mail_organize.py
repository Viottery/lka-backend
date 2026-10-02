from app.experts.mail_organize import group_by_sender, shortlist_matters


def test_group_by_sender_canonicalizes_and_orders_groups() -> None:
    cards = [
        {"message_id": "b", "sender": "Alice <ALICE@example.com>", "received_at": "2025-01-02T00:00:00Z", "subject": "Z"},
        {"message_id": "a", "sender": "alice@example.com", "received_at": "2025-01-01T00:00:00Z", "subject": "A"},
        {"message_id": "c", "sender": "bob@example.com", "subject": "Other"},
    ]

    groups = group_by_sender(cards)

    assert [group["sender_key"] for group in groups] == ["alice@example.com", "bob@example.com"]
    assert groups[0]["sender_label"] == "Alice"
    assert groups[0]["count"] == 2
    assert groups[0]["message_ids"] == ["a", "b"]
    assert groups[0]["earliest_received_at"] == "2025-01-01T00:00:00Z"
    assert groups[0]["latest_received_at"] == "2025-01-02T00:00:00Z"
    assert groups[0]["subject_examples"] == ["A", "Z"]


def test_invalid_senders_are_isolated_and_subjects_are_bounded() -> None:
    cards = [
        {"message_id": str(i), "sender": "", "subject": f"Subject {i}"}
        for i in range(7)
    ]
    groups = group_by_sender(cards)

    assert len(groups) == 7
    assert len({group["sender_key"] for group in groups}) == 7
    repeated = group_by_sender(
        [{"message_id": str(i), "sender": "same@example.com", "subject": f"Subject {i}"} for i in range(7)]
    )
    assert len(repeated[0]["subject_examples"]) == 5


def test_shortlist_ranks_exact_links_then_conservative_text_matches() -> None:
    cards = [
        {
            "message_id": "mail-1",
            "subject": "Project Aurora launch schedule",
        }
    ]
    matters = [
        {
            "matter_id": "linked",
            "title": "Unrelated",
            "summary": "",
            "source_links": [{"source_type": "mail_message", "source_id": "mail-1"}],
        },
        {"matter_id": "lexical", "title": "Aurora launch", "summary": "Project timeline"},
        {"matter_id": "other", "title": "Garden plans", "summary": ""},
    ]

    result = shortlist_matters(cards, matters)

    assert [item["matter_id"] for item in result["mail-1"]] == ["linked", "lexical"]
    assert result["mail-1"][0]["score"] == 1.0
    assert set(result["mail-1"][0]) == {"matter_id", "title", "summary", "reason", "score"}


def test_shortlist_handles_chinese_overlap_and_candidate_limit() -> None:
    cards = [{"message_id": "m", "subject": "年度预算审批安排"}]
    matters = [
        {"matter_id": "a", "title": "年度预算审批", "summary": ""},
        {"matter_id": "b", "title": "年度预算审批流程", "summary": ""},
        {"matter_id": "c", "title": "旅行计划", "summary": ""},
    ]

    result = shortlist_matters(cards, matters, max_candidates=1)

    assert len(result["m"]) == 1
    assert result["m"][0]["matter_id"] in {"a", "b"}
    assert shortlist_matters([{"message_id": "x", "subject": "预算"}], matters)["x"] == []


def test_shortlist_only_returns_ids_from_input_matters_and_obeys_zero_limit() -> None:
    cards = [{"message_id": "m", "subject": "invoice payment"}]
    matters = [{"matter_id": "known", "title": "invoice payment", "summary": ""}]

    assert shortlist_matters(cards, matters)["m"][0]["matter_id"] == "known"
    assert shortlist_matters(cards, matters, max_candidates=0)["m"] == []
