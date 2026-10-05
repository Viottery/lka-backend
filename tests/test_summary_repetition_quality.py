"""Exact, reversible input projection; never a language-specific summary rule."""

import hashlib

from app.core.memory_background import _summary_message_view
from app.core.sessions import SessionRecentMessage


def message(content):
    return SessionRecentMessage(role="user", content=content, trace_id="actual-source",
                                created_at="2026-10-05T00:00:00+00:00")


def test_repeated_unicode_negation_and_partial_suffix_are_losslessly_represented():
    pattern = "不可执行；尚未批准。🙂需要复核。 "
    raw = "评审改为明天，仅本项目。\r\n" + pattern * 100 + pattern[:7] + "\r\n最终更正：撤回前述安排。"
    source = message(raw)
    view = _summary_message_view(source)
    assert source.content == raw
    assert view["trace_id"] == source.trace_id and view["created_at"] == source.created_at
    assert view["content"] == "评审改为明天，仅本项目。\r\n" + pattern + pattern[:7] + "\r\n最终更正：撤回前述安排。"
    projection = view["content_projection"]
    assert projection["original_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    run = projection["runs"][0]
    assert run == {"line_index": 1, "period_chars": len(pattern),
                   "complete_repetitions": 100, "trailing_chars": 7}
    compact_line = view["content"].splitlines()[1]
    assert compact_line[:run["period_chars"]] * run["complete_repetitions"] + compact_line[-7:] == raw.splitlines()[1]


def test_distinct_repetition_correction_and_prose_remain_exact():
    # A late differing requirement must not be absorbed into an earlier repeat.
    raw = "No approval. " * 100 + "APPROVED ONLY FOR A DIFFERENT PROJECT."
    source = message(raw)
    assert _summary_message_view(source) == source.model_dump(mode="json")
    short = message("不要重复执行。" * 3)
    assert _summary_message_view(short) == short.model_dump(mode="json")


def test_repetition_projection_has_bounded_metadata_and_oversize_work():
    raw = ("x" * 1100 + "\n") * 12
    view = _summary_message_view(message(raw))
    assert len(view["content_projection"]["runs"]) == 8
    assert view["content"].splitlines()[8:] == raw.splitlines()[8:]
    huge = message("x" * 65537)
    assert _summary_message_view(huge) == huge.model_dump(mode="json")
