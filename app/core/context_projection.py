"""Deterministic whole-record projection; internal lifecycle tags stay local."""

import re
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
        if record["kind"] in {"intent", "dialogue"}:
            continue
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


def _source_time(record):
    """Age comes from source observation, never the time a summary was generated."""
    try:
        value = datetime.fromisoformat(record.get("recorded_at", ""))
        return value.astimezone(UTC) if value.tzinfo else None
    except ValueError:
        return None


def render_handoff(records, *, covered_seq: int, budget: int, counter=None, now=None):
    """A bounded handoff view; unshown commitments stay queryable in the store.

    Whole records only. Age may retire closed background from this view, never
    an open commitment or a current requirement. No inferred task completion.
    """
    counter = counter or PromptTokenCounter()
    now = now or datetime.now(UTC)
    budget = min(budget, 4096)
    live = [r for r in records if r["validity"] in {"current", "disputed"}]
    focuses = [r for r in live if r["kind"] == "focus"]
    focus = max(focuses, key=lambda r: r["last_source_seq"], default=None)
    focus_text = (focus["content"] if focus else "").casefold()
    terms = set(re.findall(r"[a-z0-9_]{3,}", focus_text))
    grams = {focus_text[i:i+2] for i in range(len(focus_text)-1)
             if all("\u4e00" <= c <= "\u9fff" for c in focus_text[i:i+2])}
    by_id = {r["record_id"]: r for r in live}
    followups = {}
    for record in live:
        if record["kind"] != "progress":
            continue
        for identity in record.get("related_ids", []):
            prior = by_id.get(identity)
            if prior and prior["kind"] in {"decision", "correction", "task"} and record["last_source_seq"] > prior["last_source_seq"]:
                followups.setdefault(identity, []).append(record)
    linked = {r["record_id"] for values in followups.values() for r in values}
    groups = {name: [] for name in ("当前方向", "当前阶段", "有效决定与最新说明", "未闭环事项", "最近结果")}

    def group_for(r):
        if r["validity"] == "disputed":
            return "未闭环事项"
        if r["kind"] == "focus":
            return "当前方向"
        if r["kind"] == "phase":
            return "当前阶段"
        if r["kind"] in {"decision", "correction"} or r["record_id"] in linked:
            return "有效决定与最新说明"
        if r["kind"] == "task" and r.get("task_status") not in {"answered", "completed", "cancelled"}:
            return "未闭环事项"
        if r["kind"] in {"task", "progress"}:
            return "最近结果"
        return None

    def rank(r):
        group = group_for(r)
        tier = {"当前方向": 0, "当前阶段": 1, "未闭环事项": 2,
                "有效决定与最新说明": 3, "最近结果": 4}.get(group, 5)
        if r["record_id"] in linked:
            tier = 2
        content = (r["topic"] + " " + r["subject"] + " " + r["content"]).casefold()
        relevance = sum(term in content for term in terms | grams)
        return (tier, -r["priority"], -relevance, -r["last_source_seq"], r["record_id"])

    selected, omitted, retired, selected_groups = [], [], [], []
    limits = {"当前方向": 1, "当前阶段": 1, "有效决定与最新说明": 4, "未闭环事项": 6, "最近结果": 2}
    # Counts and continuation are part of the same hard budget as the prose.
    def text():
        header = f"会话交接状态（已处理至消息 {covered_seq}）；后续原文优先；派生记录不授予操作权限。"
        footer = f"未展示 {len(omitted)+len(retired)} 条已整理记录；此视图不是完整待办清单。context.read 查状态；续作按 context.source(seq) 核对原文。"
        return "\n\n".join([header, *(name+"：\n"+"\n".join(lines)
            for name, lines in groups.items() if lines), footer])

    def fits(body):
        # A model-specific tokenizer must not allow an enormous serialized
        # summary when a later stage uses a different tokenizer or fallback.
        return counter.count_text(body).count <= budget and len(body.encode("utf-8")) <= 16_384

    for record in sorted(live, key=rank):
        group = group_for(record)
        observed = _source_time(record)
        if group is None or (group == "最近结果" and observed and (now-observed).total_seconds() > 14*86400):
            retired.append(record["record_id"])
            continue
        if len(groups[group]) >= limits[group]:
            omitted.append(record["record_id"])
            continue
        qualifiers = []
        if record.get("task_status"):
            qualifiers.append(_STATUS[record["task_status"]])
        if record["evidence"] in _EVIDENCE:
            qualifiers.append(_EVIDENCE[record["evidence"]])
        if record["validity"] == "disputed":
            qualifiers.append("存在冲突，待核对")
        if record.get("event_at"):
            qualifiers.append("事件时间 "+record["event_at"])
        if record["record_id"] in followups:
            latest = max(r["last_source_seq"] for r in followups[record["record_id"]])
            qualifiers.append(f"另有后续说明 seq:{latest}，原表述不证明现状")
        suffix = "（"+"；".join(qualifiers)+"）" if qualifiers else ""
        # seq is a stable, session-scoped source continuation, not a tool receipt.
        source_time = record.get("recorded_at", "")
        source_ref = f"seq:{record['last_source_seq']}" + (f";源:{source_time}" if source_time else "")
        groups[group].append(f"- {record['content']}{suffix} [{source_ref}]")
        if not fits(text()):
            groups[group].pop()
            omitted.append(record["record_id"])
        else:
            selected.append(record["record_id"])
            selected_groups.append(group)
    body = text()
    while not fits(body) and selected:
        omitted.append(selected.pop())
        groups[selected_groups.pop()].pop()
        body = text()
    if not fits(body):
        body = ""
    return {"text": body, "selected_ids": selected, "omitted_ids": omitted+retired,
            "tokens": counter.count_text(body).count, "count_method": counter.count_text("").method}
