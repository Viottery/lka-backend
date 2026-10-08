"""Experimental multi-lane lexical selector; retained input is not semantic coverage."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from app.domains.message_reading_codec import _SIGNALS, _WORDS, _validate

SELECTOR_VERSION = "candidate-v1"


def select_message_candidates(messages, *, max_messages=40, seed="v1",
                              exploration_fraction=.1, protected_senders=(),
                              protected_keywords=(), self_ids=(), known_topics=()):
    """Preserve hard signals and bounded ancestry, then diversify soft evidence.

    Exploration has an independent reserve even when protected rows overflow the
    soft target. Reply traversal is bounded by the finite input, includes complete
    available ancestry, and terminates on cycles. Missing evidence stays unknown.
    Lexical/time/reply groups are candidate hints, never semantic topic assignments.
    """
    _validate(messages)
    if type(max_messages) is not int or max_messages <= 0:
        raise ValueError("positive integer max_messages required")
    if not math.isfinite(exploration_fraction) or not 0 <= exploration_fraction <= 1:
        raise ValueError("exploration_fraction must be between zero and one")
    reasons = {i: [] for i in range(len(messages))}
    by_id = {row["id"]: i for i, row in enumerate(messages)}
    selves = set(self_ids)
    hard = set()
    for i, row in enumerate(messages):
        if row["sender"] in protected_senders:
            reasons[i].append("configured_important_contact")
        if any(word and word.casefold() in row["text"].casefold() for word in protected_keywords):
            reasons[i].append("configured_focus_keyword")
        if set(row["mentions"]) & selves:
            reasons[i].append("direct_mention")
        if "all" in row["mentions"]:
            reasons[i].append("group_mention")
        parent = by_id.get(row["reply"])
        if parent is not None and messages[parent]["sender"] in selves:
            reasons[i].append("reply_to_self")
        for name in ("action", "deadline", "correction"):
            if _SIGNALS[name].search(row["text"]):
                reasons[i].append(name)
        if reasons[i]:
            hard.add(i)
    # Only original hard signals generate time-bounded immediate context.
    protected = set(hard)
    for i in sorted(hard):
        time = messages[i]["sent_at"] or messages[i]["received_at"]
        for j in (i - 1, i + 1):
            if 0 <= j < len(messages):
                other_time = messages[j]["sent_at"] or messages[j]["received_at"]
                if abs(time - other_time) <= 900:
                    protected.add(j)
                    reasons[j].append("hard_signal_adjacent_context")
    pending = list(protected)
    while pending:
        i = pending.pop()
        parent = by_id.get(messages[i]["reply"])
        if parent is not None and parent not in protected:
            protected.add(parent)
            reasons[parent].append("hard_signal_reply_ancestry")
            pending.append(parent)
    selected = set(protected)
    remaining = set(range(len(messages))) - selected

    def rank(i):
        return hashlib.sha256(f"{seed}\0{messages[i]['id']}".encode()).hexdigest()

    if len(messages) <= max_messages:
        selected.update(remaining)
        for i in remaining:
            reasons[i].append("below_selection_target")
    else:
        reserve = min(len(remaining), math.ceil(max_messages * exploration_fraction))
        # Reserve first; exploitation cannot consume the independent exploration lane.
        explorers = sorted(remaining, key=rank)[:reserve]
        for i in explorers:
            selected.add(i)
            remaining.remove(i)
            reasons[i].append("seeded_exploration")
        tokens = {i: set(_WORDS.findall(row["text"].casefold())) for i, row in enumerate(messages)}
        frequency = Counter(term for terms in tokens.values() for term in terms)
        terms = {term.casefold() for title in known_topics[:40]
                 for term in re.findall(r"[a-zA-Z0-9_]{3,}|[\u4e00-\u9fff]{3,}", title[:200])[:6]}
        senders = Counter(messages[i]["sender"] for i in selected)
        times = Counter(messages[i]["received_at"] // 900 for i in selected)
        groups = Counter((messages[i].get("thread") or messages[i]["reply"]) for i in selected)
        while remaining and len(selected) < max_messages:
            def priority(i):
                row = messages[i]
                soft = (int(row["kind"] != "text" or any(
                    part.get("kind", part.get("type")) not in ("text", "mention", "reply")
                    for part in row["parts"])) + int(row["reply"] is not None)
                    + int(bool(_SIGNALS["self_statement"].search(row["text"])))
                    + int(bool(_SIGNALS["negation"].search(row["text"]))))
                return (senders[row["sender"]], times[row["received_at"] // 900],
                        groups[row.get("thread") or row["reply"]],
                        -int(any(term in row["text"].casefold() for term in terms)), -soft,
                        -sum(1 / frequency[term] for term in tokens[i]), rank(i))
            i = min(remaining, key=priority)
            remaining.remove(i)
            selected.add(i)
            reasons[i].append("lexical_time_reply_diversity_candidate")
            senders[messages[i]["sender"]] += 1
            times[messages[i]["received_at"] // 900] += 1
            groups[messages[i].get("thread") or messages[i]["reply"]] += 1
    decisions = []
    for i, row in enumerate(messages):
        detail = list(dict.fromkeys(reasons[i]))
        if row["reply"] and row["reply"] not in by_id:
            detail.append("unresolved_reply_unknown")
        if any(value == "unknown" for value in row["capabilities"].values()):
            detail.append("metadata_capability_unknown")
        if len(protected) > max_messages and i in protected:
            detail.append("protected_overflow_requires_fragmentation")
        decisions.append({"id": row["id"], "decision": "retained" if i in selected else "sampled_out",
                          "reasons": detail or ["outside_selection_budget"]})
    return {"messages": [row for i, row in enumerate(messages) if i in selected],
            "decisions": decisions,
            "coverage_mode": "full_text" if len(selected) == len(messages) else "selected_text",
            "protected_ids": [row["id"] for i, row in enumerate(messages) if i in protected],
            "deferred_ids": []}
