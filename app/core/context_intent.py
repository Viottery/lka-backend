"""Small, source-linked projection of conversational direction and response state."""

from app.core.prompt_tokens import PromptTokenCounter

_MAX_TOKENS = 1024
_MAX_BYTES = 4096
_MAX_LENGTHS = {"intent": 240, "dialogue": 160}
_LABELS = {"intent": "当前对话方向", "dialogue": "回应状态"}


def conversation_messages(rows):
    """Keep user messages and only trace-linked agent replies to unambiguous users."""
    user_traces = set()
    tool_traces = set()
    for row in rows:
        trace_id = row.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            continue
        if row.get("role") == "user":
            user_traces.add(trace_id)
        elif row.get("role") == "tool":
            tool_traces.add(trace_id)

    return [
        row for row in rows
        if row.get("role") == "user"
        or (row.get("role") == "agent"
            and isinstance(row.get("trace_id"), str)
            and bool(row["trace_id"])
            and row["trace_id"] in user_traces
            and row["trace_id"] not in tool_traces)
    ]


def intent_records(records):
    """Select only current records explicitly typed for the intent profile."""
    return [r for r in records if r.get("kind") in {"intent", "dialogue"}
            and r.get("validity") == "current"]


def render_intent(records, *, covered_seq, budget, counter=None, now=None):
    """Render at most one newest whole record per intent category within hard bounds."""
    del now
    counter = counter or PromptTokenCounter()
    candidates = intent_records(records)
    selected_by_kind = {}
    for kind in ("intent", "dialogue"):
        eligible = [r for r in candidates
                    if r.get("kind") == kind
                    and isinstance(r.get("content"), str)
                    and len(r["content"]) <= _MAX_LENGTHS[kind]]
        if eligible:
            selected_by_kind[kind] = max(eligible, key=lambda r: r.get("last_source_seq", -1))

    selected = []
    omitted = [r.get("record_id") for r in candidates]
    hard_budget = min(max(0, budget), _MAX_TOKENS)

    def body_for(items):
        lines = [f"{_LABELS[r['kind']]}：{r['content']} [seq:{r['last_source_seq']}]" for r in items]
        return "\n".join(lines)

    for kind in ("intent", "dialogue"):
        record = selected_by_kind.get(kind)
        if record is None:
            continue
        trial = selected + [record]
        rendered = body_for(trial)
        if (counter.count_text(rendered).count <= hard_budget
                and len(rendered.encode("utf-8")) <= _MAX_BYTES):
            selected.append(record)

    selected_ids = [r.get("record_id") for r in selected]
    selected_set = set(selected_ids)
    omitted_ids = [item for item in omitted if item not in selected_set]
    rendered = body_for(selected) if selected else ""
    return {"text": rendered, "selected_ids": selected_ids, "omitted_ids": omitted_ids,
            "tokens": counter.count_text(rendered).count,
            "count_method": counter.count_text("").method}
