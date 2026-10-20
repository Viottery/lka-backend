"""Deterministic source passages. Model summaries never supply the stored quote."""

import hashlib
import json


def source_passages(session_id, row, *, max_chars=1200):
    """Partition without dropping characters; IDs bind message, scope and span."""
    if max_chars < 1:
        raise ValueError("Source passage size must be positive")
    identity = hashlib.sha256(json.dumps(
        [session_id, row["seq"], row["role"], row["trace_id"], row["created_at"], row["content"]],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()[:16]
    text = row["content"]
    passages = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            paragraph = text.rfind("\n\n", start, end)
            newline = text.rfind("\n", start, end)
            if paragraph > start + max_chars // 2:
                end = paragraph + 2
            elif newline > start + max_chars // 2:
                end = newline + 1
        passages.append({"passage_id": f"p_{row['seq']}_{start}_{end}_{identity}",
                         "seq": row["seq"], "start": start, "end": end,
                         "content": text[start:end]})
        start = end
    return passages


def passage_messages(session_id, rows):
    return [{"seq": row["seq"], "role": row["role"], "created_at": row["created_at"],
             "passages": [{"passage_id": p["passage_id"], "content": p["content"]}
                          for p in source_passages(session_id, row)]} for row in rows]


def passage_catalog(session_id, rows):
    return {p["passage_id"]: p for row in rows for p in source_passages(session_id, row)}
