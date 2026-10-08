"""Lossless projected-message codec and deterministic experimental selection.

Inputs are already redacted, single-conversation snapshot projections. This module
does not authenticate snapshots, redact content, count tokens or certify model
coverage. ``full_text`` means the selector returned every projected message; the
caller must still send every fragment before publishing full-text coverage.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter
from typing import Any

FIELDS = (
    "id",
    "sender",
    "seq",
    "sent_at",
    "received_at",
    "text",
    "kind",
    "mentions",
    "reply",
    "capabilities",
    "parts",
)
CODEC_VERSION = 2
SELECTOR_VERSION = "v1.4"
OPTIONAL_FIELDS = ("timestamp_quality", "thread", "fragment_index", "fragment_count")
ENCODED_FIELDS = (*FIELDS, "optional")
_SIGNALS = {
    "self_statement": re.compile(
        r"我(?:只(?:喝|用|看|买|吃)|喜欢|偏好|需要|想要|希望|常用|习惯|关注|寻找)|"
        r"\bI (?:prefer|like|need|want|usually use)\b",
        re.IGNORECASE,
    ),
    "deadline": re.compile(
        r"截止|期限|最迟|之前|明天|今晚|今天|deadline|due\b|before\b|"
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}[日号]",
        re.IGNORECASE,
    ),
    "action": re.compile(
        r"请|麻烦|需要|务必|帮忙|确认|提交|报名|安排|please\b|must\b|need\b|action\b", re.IGNORECASE
    ),
    "correction": re.compile(
        r"更正|纠正|改为|改成|撤回|说错|不是.*而是|correct|instead", re.IGNORECASE
    ),
    "negation": re.compile(r"不|没|否|取消|拒绝|\b(?:no|not|never|cannot|cancel)\b", re.IGNORECASE),
}
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_WORDS = re.compile(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
_SHORT_QUESTION = re.compile(r"怎么|为什么|为啥|怎么办|哪|谁|[?？]")


def _validate(messages: list[dict[str, Any]]) -> None:
    if not isinstance(messages, list):
        raise TypeError("messages must be a list")
    ids = set()
    for message in messages:
        if (
            not isinstance(message, dict)
            or not set(FIELDS) <= set(message)
            or (set(message) - set(FIELDS) - set(OPTIONAL_FIELDS))
        ):
            raise ValueError("message must contain exactly the canonical projection fields")
        for field in ("id", "sender", "text", "kind"):
            if not isinstance(message[field], str):
                raise TypeError(f"{field} must be a string")
        for field in ("seq", "received_at"):
            if type(message[field]) is not int:
                raise ValueError(f"{field} must be an integer")
        if message["sent_at"] is not None and type(message["sent_at"]) is not int:
            raise ValueError("sent_at must be an integer or null")
        if message["reply"] is not None and not isinstance(message["reply"], str):
            raise ValueError("reply must be a string or null")
        if "timestamp_quality" in message and not isinstance(message["timestamp_quality"], str):
            raise ValueError("timestamp_quality must be a string")
        for field in ("fragment_index", "fragment_count"):
            if field in message and (type(message[field]) is not int or message[field] < 1):
                raise ValueError("fragment counters must be positive integers")
        if message.get("thread") is not None and not isinstance(message["thread"], str):
            raise ValueError("thread must be a string or null")
        if not isinstance(message["mentions"], list) or any(
            not isinstance(value, str) for value in message["mentions"]
        ):
            raise ValueError("mentions must be a list of strings")
        if not isinstance(message["capabilities"], dict) or not isinstance(message["parts"], list):
            raise TypeError("capabilities must be a dict and parts must be a list")
        if any(not isinstance(part, dict) for part in message["parts"]):
            raise ValueError("parts must contain dictionaries")
        if message["id"] in ids:
            raise ValueError("message aliases must be unique")
        ids.add(message["id"])

    # Reject non-JSON values and non-string nested keys instead of silently coercing them.
    def check(value: Any) -> None:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("JSON object keys must be strings")
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif value is not None and type(value) not in (str, bool, int, float):
            raise ValueError("projection must be JSON-safe")

    check(messages)
    try:
        json.dumps(messages, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("projection must be JSON-safe") from exc


_INLINE_FIELDS = frozenset({"id", "sender", "seq", "text"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def encode_shared_defaults(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Hoist repeated metadata; source identities and text always stay inline."""
    _validate(messages)
    rows = copy.deepcopy(messages)
    defaults: dict[str, Any] = {}
    for field in (*FIELDS, *OPTIONAL_FIELDS):
        if field in _INLINE_FIELDS or not rows or any(field not in row for row in messages):
            continue
        values = [_canonical_json(row[field]) for row in messages]
        common, count = Counter(values).most_common(1)[0]
        if count < 2:
            continue
        defaults[field] = copy.deepcopy(messages[values.index(common)][field])
        for row, value in zip(rows, values, strict=True):
            if value == common:
                del row[field]
    return {"messages": rows, "message_defaults": defaults}


def decode_shared_defaults(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Restore missing default fields, preserving explicit overrides and nulls."""
    try:
        rows, defaults = payload["messages"], payload["message_defaults"]
        if not isinstance(rows, list) or not isinstance(defaults, dict):
            raise TypeError("invalid shared-defaults payload")
        if set(defaults) - (set(FIELDS) | set(OPTIONAL_FIELDS)) or set(defaults) & _INLINE_FIELDS:
            raise ValueError("source identity and text must remain inline")
        if any(not isinstance(row, dict) or not _INLINE_FIELDS <= row.keys() for row in rows):
            raise ValueError("source identity and text must remain inline")
        messages = [{**copy.deepcopy(defaults), **copy.deepcopy(row)} for row in rows]
        _validate(messages)
        return messages
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed shared-defaults payload") from exc


def encode_messages(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Encode all text without truncation; integer timestamps remain exact."""
    _validate(messages)
    base = min((message["received_at"] for message in messages), default=0)
    texts: list[str] = []
    indexes: dict[str, int] = {}

    def intern(text: str) -> int:
        if text not in indexes:
            indexes[text] = len(texts)
            texts.append(text)
        return indexes[text]

    rows = []
    for message in messages:
        row = [copy.deepcopy(message[field]) for field in FIELDS]
        row[3] = None if message["sent_at"] is None else message["sent_at"] - base
        row[4] = message["received_at"] - base
        row[5] = intern(message["text"])
        parts = []
        for original in message["parts"]:
            part = copy.deepcopy(original)
            ref = intern(part.pop("text")) if isinstance(part.get("text"), str) else None
            parts.append([part, ref])
        row[10] = parts
        row.append(
            {field: copy.deepcopy(message[field]) for field in OPTIONAL_FIELDS if field in message}
        )
        rows.append(row)
    metadata: list[Any] = []
    metadata_indexes: dict[str, int] = {}
    for row in rows:
        for slot in (7, 9, 10, 11):
            key = json.dumps(row[slot], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key not in metadata_indexes:
                metadata_indexes[key] = len(metadata)
                metadata.append(row[slot])
            row[slot] = metadata_indexes[key]
    return {"v": CODEC_VERSION, "f": list(ENCODED_FIELDS), "b": base, "t": texts,
            "d": metadata, "m": rows}


def decode_messages(encoded: dict[str, Any]) -> list[dict[str, Any]]:
    """Decode v1 and v2, rejecting malformed structures and dictionary references."""
    try:
        if type(encoded["v"]) is not int or encoded["v"] not in (1, 2) or encoded["f"] != list(ENCODED_FIELDS):
            raise ValueError("unsupported codec version or fields")
        base, texts, rows = encoded["b"], encoded["t"], encoded["m"]
        if type(base) is not int or not isinstance(texts, list) or not isinstance(rows, list):
            raise ValueError("invalid codec header")
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("invalid text dictionary")

        def dereference(index: int) -> str:
            if type(index) is not int or not 0 <= index < len(texts):
                raise ValueError("invalid text dictionary reference")
            return texts[index]

        messages = []
        for row in rows:
            if not isinstance(row, list) or len(row) != len(ENCODED_FIELDS):
                raise ValueError("invalid message row")
            row = copy.deepcopy(row)
            if encoded["v"] == 2:
                metadata = encoded["d"]
                if not isinstance(metadata, list):
                    raise ValueError("invalid metadata dictionary")
                for slot in (7, 9, 10, 11):
                    ref = row[slot]
                    if type(ref) is not int or not 0 <= ref < len(metadata):
                        raise ValueError("invalid metadata dictionary reference")
                    row[slot] = copy.deepcopy(metadata[ref])
            message = dict(zip(FIELDS, copy.deepcopy(row[: len(FIELDS)]), strict=True))
            if not isinstance(row[11], dict) or set(row[11]) - set(OPTIONAL_FIELDS):
                raise ValueError("invalid optional metadata")
            message.update(copy.deepcopy(row[11]))
            if type(row[4]) is not int or (row[3] is not None and type(row[3]) is not int):
                raise ValueError("invalid time offset")
            message["sent_at"] = None if row[3] is None else row[3] + base
            message["received_at"] = row[4] + base
            message["text"] = dereference(row[5])
            parts = []
            for part, ref in row[10]:
                if not isinstance(part, dict):
                    raise TypeError("invalid part")
                restored = copy.deepcopy(part)
                if ref is not None:
                    if "text" in restored:
                        raise ValueError("ambiguous part text")
                    restored["text"] = dereference(ref)
                parts.append(restored)
            message["parts"] = parts
            messages.append(message)
        _validate(messages)
        return messages
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("malformed codec payload") from exc


def select_messages(
    messages: list[dict[str, Any]],
    *,
    max_messages: int = 40,
    seed: str = "v1",
    sampling_threshold: int | None = None,
    exploration_fraction: float = 0.1,
    context_radius: int = 1,
    context_seconds: int = 900,
    protected_senders: tuple[str, ...] = (),
    protected_keywords: tuple[str, ...] = (),
    known_topics: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return original rows in input order and an auditable decision for every row.

    The count limit is a soft selection target, never a token budget. Protected
    overflow is returned intact for caller-side fragmentation. Neighbor context
    is bounded by both row distance and effective timestamp; replies recursively
    protect available ancestors, while missing reply aliases are marked unresolved.
    Sender/time/rare-token representation and seeded exploration are heuristics,
    not semantic coverage guarantees. No messages are deferred here: only the
    caller knows which selected fragments can actually be dispatched.
    """
    _validate(messages)
    threshold = max_messages if sampling_threshold is None else sampling_threshold
    if (
        any(
            type(value) is not int or value < 0
            for value in (
                max_messages,
                threshold,
                context_radius,
                context_seconds,
            )
        )
        or max_messages == 0
    ):
        raise ValueError("positive max_messages and nonnegative integer thresholds required")
    if not math.isfinite(exploration_fraction) or not 0 <= exploration_fraction <= 1:
        raise ValueError("exploration_fraction must be between zero and one")
    reasons: dict[int, list[str]] = {index: [] for index in range(len(messages))}
    # A bounded lexical hint, not a semantic membership assertion. It competes
    # only for representative slots; explicit safety signals remain protected.
    topic_terms = {term.casefold() for title in known_topics[:40]
                   for term in re.findall(r"[a-zA-Z0-9_]{3,}|[\u4e00-\u9fff]{3,}", title[:200])[:6]}
    topic_matches = {index for index, row in enumerate(messages)
                     if any(term in row["text"].casefold() for term in topic_terms)}
    by_id = {message["id"]: index for index, message in enumerate(messages)}
    seen_urls: set[str] = set()
    for index, message in enumerate(messages):
        if message["sender"] in protected_senders:
            reasons[index].append("configured_important_contact")
        if any(word and word.casefold() in message["text"].casefold() for word in protected_keywords):
            reasons[index].append("configured_focus_keyword")
        if message["mentions"]:
            reasons[index].append("native_mention")
        if message["reply"] is not None:
            reasons[index].append("native_reply")
            if message["reply"] == "unresolved" or message["reply"] not in by_id:
                reasons[index].append("unresolved_reply")
        if message.get("thread") is not None:
            reasons[index].append("native_thread")
        for name, pattern in _SIGNALS.items():
            if pattern.search(message["text"]):
                reasons[index].append(name)
        urls = set(_URL.findall(message["text"]))
        if urls - seen_urls or "[link]" in message["text"]:
            reasons[index].append("new_link")
        seen_urls.update(urls)
        if message["kind"] != "text" or any(
            part.get("kind", part.get("type", "unknown")) != "text" for part in message["parts"]
        ):
            reasons[index].append("non_text_or_unknown")
    protected = {index for index, value in reasons.items() if value}
    signal_indexes = tuple(sorted(protected))
    # Short conversational follow-ups often carry a signal but no referent.
    # Preserve a bounded local exchange, even if it defeats the saving target.
    # This is context protection, not a claim that lexical sampling finds topics.
    for index in signal_indexes:
        message = messages[index]
        if len(message["text"]) <= 40 and _SHORT_QUESTION.search(message["text"]):
            timestamp = message["sent_at"]
            timestamp = message["received_at"] if timestamp is None else timestamp
            for other in range(max(0, index - 6), min(len(messages), index + 3)):
                other_time = messages[other]["sent_at"]
                other_time = messages[other]["received_at"] if other_time is None else other_time
                if abs(timestamp - other_time) <= context_seconds:
                    reasons[other].append("short_followup_context")
    # Only signal messages create adjacent context, preventing an unbounded flood fill.
    for index in signal_indexes:
        time = messages[index]["sent_at"]
        time = messages[index]["received_at"] if time is None else time
        for other in range(
            max(0, index - context_radius), min(len(messages), index + context_radius + 1)
        ):
            other_time = messages[other]["sent_at"]
            other_time = messages[other]["received_at"] if other_time is None else other_time
            if other != index and abs(time - other_time) <= context_seconds:
                reasons[other].append("adjacent_context")
    protected = {index for index, value in reasons.items() if value}
    pending = list(protected)
    while pending:
        index = pending.pop()
        for field in ("reply", "thread"):
            alias = messages[index].get(field)
            target = None if alias == "unresolved" else by_id.get(alias)
            if target is not None and target not in protected:
                protected.add(target)
                reasons[target].append(f"{field}_context")
                pending.append(target)
    selected = set(protected)
    if len(messages) <= threshold or len(messages) <= max_messages:
        selected.update(range(len(messages)))
        for index in selected - protected:
            reasons[index].append("below_sampling_threshold")
    else:
        remaining = set(range(len(messages))) - selected
        slots = max(0, max_messages - len(selected))
        exploration_slots = (
            min(slots, max(1, math.ceil(slots * exploration_fraction)))
            if (slots and exploration_fraction)
            else 0
        )

        def rank(index: int) -> str:
            return hashlib.sha256(f"{seed}\0{messages[index]['id']}".encode()).hexdigest()

        tokens = {
            index: set(_WORDS.findall(message["text"].lower()))
            for index, message in enumerate(messages)
        }
        frequency = Counter(token for values in tokens.values() for token in values)
        sender_counts = Counter(messages[index]["sender"] for index in selected)
        time_counts = Counter(messages[index]["received_at"] // 900 for index in selected)
        for _ in range(slots - exploration_slots):

            def priority(index: int) -> tuple:
                message = messages[index]
                return (
                    -int(index in topic_matches),
                    sender_counts[message["sender"]],
                    time_counts[message["received_at"] // 900],
                    -sum(1 / frequency[token] for token in tokens[index]),
                    rank(index),
                )

            chosen = min(remaining, key=priority)
            selected.add(chosen)
            remaining.remove(chosen)
            reasons[chosen].append("sender_time_rare_token_representative")
            if chosen in topic_matches:
                reasons[chosen].append("known_topic_lexical_candidate")
            sender_counts[messages[chosen]["sender"]] += 1
            time_counts[messages[chosen]["received_at"] // 900] += 1
        for chosen in sorted(remaining, key=rank)[:exploration_slots]:
            selected.add(chosen)
            reasons[chosen].append("seeded_exploration")
    overflow = len(protected) > max_messages
    decisions = []
    for index, message in enumerate(messages):
        detail = list(dict.fromkeys(reasons[index]))
        if overflow and index in protected:
            detail.append("protected_overflow_requires_fragmentation")
        decisions.append(
            {
                "id": message["id"],
                "decision": "retained" if index in selected else "sampled_out",
                "reasons": detail or ["outside_selection_budget"],
            }
        )
    return {
        "messages": [message for index, message in enumerate(messages) if index in selected],
        "decisions": decisions,
        "coverage_mode": "full_text" if len(selected) == len(messages) else "selected_text",
        "protected_ids": [
            message["id"] for index, message in enumerate(messages) if index in protected
        ],
        "deferred_ids": [],
    }
