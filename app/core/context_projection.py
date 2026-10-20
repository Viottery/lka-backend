"""Deterministic whole-record projection; internal lifecycle tags stay local."""

from datetime import UTC, datetime

from app.core.prompt_tokens import PromptTokenCounter

_GROUPS = ("当前目标", "有效要求与决策", "进度与事实", "重要纠正", "关系与情节", "已处理事项", "待确认事项")
_STATUS = {"proposed": "仅为提议", "active": "进行中", "answered": "已回答",
           "completed": "已完成", "cancelled": "已取消", "unknown": "状态未确认"}
_EVIDENCE = {"assistant_report": "agent 报告，未独立核实", "inference": "推断，未确认"}


def render_context(records, *, covered_seq: int, budget: int, counter=None, now=None):
    counter = counter or PromptTokenCounter()
    now = now or datetime.now(UTC)
    updated = max((r.get("recorded_at", "") for r in records), default="")
    header = "已整理的会话状态" + ("（截至 " + updated + "）" if updated else "") + "；后续原始消息中的要求和纠正优先。"
    ordered = sorted(records, key=lambda r: (-r["priority"], -r["last_source_seq"], r["record_id"]))
    groups = {name: [] for name in _GROUPS}
    omitted = []
    selected = []
    selected_groups = []
    used = counter.count_text(header).count
    separator_cost = counter.count_text("\n\n").count

    def text():
        return "\n\n".join([header, *(name + "：\n" + "\n".join(lines)
                                      for name, lines in groups.items() if lines)])

    for record in ordered:
        if record["validity"] not in {"current", "disputed"}:
            continue
        kind, status = record["kind"], record.get("task_status")
        group = ("待确认事项" if status in {"proposed", "unknown"} or record["validity"] == "disputed"
                 else "已处理事项" if status in {"answered", "completed", "cancelled"}
                 else "当前目标" if kind == "task" else "重要纠正" if kind == "correction"
                 else "有效要求与决策" if kind in {"preference", "decision"}
                 else "关系与情节" if kind == "episode" else "进度与事实")
        qualifiers = []
        if status:
            qualifiers.append(_STATUS[status])
        if record["evidence"] in _EVIDENCE:
            qualifiers.append(_EVIDENCE[record["evidence"]])
        if record["validity"] == "disputed":
            qualifiers.append("存在冲突，待核查")
        if record.get("event_at"):
            qualifiers.append("事件时间 " + record["event_at"])
        review = record.get("review_at")
        if review and datetime.fromisoformat(review) <= now:
            qualifiers.append("已到复查时间，不代表完成")
        prefix = f"[{record['topic']} / {record['subject']}]"
        suffix = "（" + "；".join(qualifiers) + "）" if qualifiers else ""
        line = prefix + " " + record["content"] + suffix
        cost = counter.count_text(line + "\n").count + separator_cost
        if not groups[group]:
            cost += counter.count_text(group + "：\n").count
        if used + cost > budget:
            omitted.append(record["record_id"])
        else:
            groups[group].append(line)
            selected.append(record["record_id"])
            selected_groups.append(group)
            used += cost
    body = text()
    tokens = counter.count_text(body).count
    # Tokenization is not strictly additive. Verify the actual combined text and
    # remove only whole low-priority records if a boundary changes the count.
    while tokens > budget and selected:
        omitted.append(selected.pop())
        groups[selected_groups.pop()].pop()
        body = text()
        tokens = counter.count_text(body).count
    if tokens > budget:
        body = ""
        omitted = list(dict.fromkeys([*selected, *omitted]))
        selected = []
        tokens = 0
    return {"text": body, "selected_ids": selected, "omitted_ids": omitted,
            "tokens": tokens, "count_method": counter.count_text("").method}
