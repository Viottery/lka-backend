import threading

import pytest

from app.core.codex_trace import CodexTraceJournal, CodexTracePayloadError


def test_codex_trace_journal_persists_full_payload_and_paginates(tmp_path):
    db_path = tmp_path / "codex.sqlite3"
    journal = CodexTraceJournal(db_path)
    first = journal.append(
        "child_1",
        "item/completed",
        {"item": {"type": "commandExecution", "output": "完整的输出"}},
        thread_id="thread_1",
        turn_id="turn_1",
        item_id="item_1",
        request_id=13,
    )
    second = journal.append("child_1", "turn/completed", {"status": "completed"})
    journal.append("child_2", "turn/completed", {"status": "other child"})

    restored = CodexTraceJournal(db_path)
    page = restored.list("child_1", after_sequence=first.sequence, limit=1)

    assert first.sequence == 1
    assert first.request_id == "13"
    assert first.created_at.endswith("+00:00")
    assert second.sequence == 2
    assert page == [second]
    assert restored.list("child_1", limit=1)[0] == first
    assert restored.list("child_2")[0].sequence == 1


def test_codex_trace_journal_marks_known_completeness_gap(tmp_path):
    journal = CodexTraceJournal(tmp_path / "codex.sqlite3")

    assert journal.has_gap("child_1") is False
    gap = journal.mark_gap(
        "child_1",
        reason="app-server disconnected before terminal event",
        details={"last_seen_item_id": "item_7"},
        thread_id="thread_1",
        turn_id="turn_1",
    )

    assert gap.method == "journal/completeness_gap"
    assert gap.payload["completeness"] == "incomplete"
    assert journal.has_gap("child_1") is True


def test_codex_trace_journal_rejects_non_json_and_oversized_payloads(tmp_path):
    journal = CodexTraceJournal(tmp_path / "codex.sqlite3", max_payload_bytes=24)

    with pytest.raises(CodexTracePayloadError, match="non-JSON"):
        journal.append("child_1", "method", {"unsupported": object()})
    with pytest.raises(CodexTracePayloadError, match="exceeds"):
        journal.append("child_1", "method", {"output": "x" * 100})

    assert journal.list("child_1") == []


def test_codex_trace_journal_serializes_concurrent_appends(tmp_path):
    journal = CodexTraceJournal(tmp_path / "codex.sqlite3")
    barrier = threading.Barrier(8)
    errors = []

    def append(index: int) -> None:
        try:
            barrier.wait()
            journal.append("child_1", "item/completed", {"index": index})
        except Exception as exc:  # noqa: BLE001 - surface failures from worker threads
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    events = journal.list("child_1", limit=8)
    assert [event.sequence for event in events] == list(range(1, 9))
