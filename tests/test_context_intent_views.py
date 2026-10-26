from app.core.context_intent import conversation_messages, intent_records, render_intent
from app.core.prompt_tokens import PromptTokenCounter


def msg(role, trace=None, content="x"):
    return {"role": role, "trace_id": trace, "content": content}


def record(kind, seq, content, validity="current", record_id=None):
    return {"kind": kind, "last_source_seq": seq, "content": content,
            "validity": validity, "record_id": record_id or f"{kind}-{seq}"}


def test_conversation_messages_excludes_background_and_tool_traces_in_any_order():
    rows = [msg("agent", "shared"), msg("agent", "background"), msg("tool", "shared"),
            msg("user", "shared"), msg("user", "plain"), msg("tool", "only-tool"),
            msg("agent", "only-tool"), msg("agent"), msg("tool", "tool-first"),
            msg("user", "tool-first")]
    assert conversation_messages(rows) == [rows[3], rows[4], rows[9]]


def test_intent_records_excludes_legacy_task_focus_and_noncurrent():
    rows = [record("intent", 1, "direction"), record("dialogue", 2, "answered"),
            record("focus", 3, "legacy"), record("task", 4, "task"),
            record("intent", 5, "old", validity="superseded")]
    assert intent_records(rows) == rows[:2]


def test_render_selects_newest_per_type_and_omits_old_and_oversize_whole():
    rows = [record("intent", 1, "旧方向"), record("intent", 4, "新方向"),
            record("dialogue", 3, "已回应"), record("dialogue", 2, "旧状态"),
            record("intent", 5, "legacy", validity="archived"),
            record("dialogue", 6, "超" * 161)]
    view = render_intent(rows, covered_seq=6, budget=512)
    assert view["selected_ids"] == ["intent-4", "dialogue-3"]
    assert view["omitted_ids"] == ["intent-1", "dialogue-2", "dialogue-6"]
    assert "新方向" in view["text"] and "已回应" in view["text"]
    assert "旧方向" not in view["text"] and "任务" not in view["text"]
    assert "seq:4" in view["text"] and "seq:3" in view["text"]


def test_render_hard_token_and_utf8_budgets_and_empty_result():
    row = record("intent", 1, "方向")
    tiny = render_intent([row], covered_seq=1, budget=0)
    assert tiny["text"] == "" and tiny["tokens"] == 0
    assert tiny["selected_ids"] == []

    class TinyCounter:
        def count_text(self, text):
            return PromptTokenCounter().count_text(text)

    huge = record("dialogue", 2, "界" * 160)
    view = render_intent([huge], covered_seq=2, budget=99999)
    assert view["tokens"] <= 1024
    assert len(view["text"].encode("utf-8")) <= 4096
    assert view["selected_ids"] == ["dialogue-2"]

    class ByteCounter:
        def count_text(self, text):
            return type("Count", (), {"count": 1, "method": "test"})()

    oversized = render_intent([record("intent", 3, "界" * 240),
                                record("dialogue", 4, "界" * 160)],
                              covered_seq=4, budget=512, counter=ByteCounter())
    assert len(oversized["text"].encode("utf-8")) <= 4096
    assert oversized["selected_ids"] == ["intent-3", "dialogue-4"]
    assert "界" * 240 in oversized["text"] and "界" * 160 in oversized["text"]
    assert view["count_method"] == TinyCounter().count_text("").method


def test_render_keeps_records_whole_and_stays_bounded_across_many_turns():
    rows = []
    for seq in range(1, 101):
        rows.extend([record("intent", seq, f"方向{seq}"),
                     record("dialogue", seq, f"回应{seq}")])
    view = render_intent(rows, covered_seq=100, budget=512)
    assert view["selected_ids"] == ["intent-100", "dialogue-100"]
    assert "方向100" in view["text"] and "回应100" in view["text"]
    assert len(view["selected_ids"]) <= 2
    assert view["tokens"] <= 512
    assert len(view["text"].encode("utf-8")) <= 4096
