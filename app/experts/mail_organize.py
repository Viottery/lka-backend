"""Pure, deterministic helpers for organizing mail cards."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

_WORD_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")


def _valid_email(sender: Any) -> tuple[str, str] | None:
    raw = str(sender or "").strip()
    if not raw:
        return None
    label, address = parseaddr(raw)
    address = address.strip()
    if "@" not in address:
        return None
    local, domain = address.rsplit("@", 1)
    if not local.strip() or not domain.strip() or any(ch.isspace() for ch in address):
        return None
    return address.casefold(), (label.strip() or address)


def _parsed_time(value: Any) -> tuple[datetime, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC), raw


def group_by_sender(cards: list[dict]) -> list[dict]:
    """Group cards by exact canonical email, isolating every invalid sender."""
    groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    labels: dict[str, str] = {}
    for index, card in enumerate(cards):
        parsed = _valid_email(card.get("sender"))
        if parsed is None:
            key = f"unknown:{index}"
            label = "发件人未知"
        else:
            key, label = parsed
        groups[key].append((index, card))
        labels.setdefault(key, label)

    result: list[dict] = []
    for key, entries in groups.items():
        dates = [parsed for _, card in entries if (parsed := _parsed_time(card.get("received_at"))) is not None]
        subjects = sorted(
            {str(card.get("subject") or "").strip() for _, card in entries if str(card.get("subject") or "").strip()},
            key=lambda subject: (subject.casefold(), subject),
        )
        message_ids = sorted(
            (str(card.get("message_id")) for _, card in entries if card.get("message_id") is not None),
            key=lambda message_id: (message_id.casefold(), message_id),
        )
        date_values = [item[1] for item in sorted(dates, key=lambda item: (item[0], item[1]))]
        result.append(
            {
                "sender_key": key,
                "sender_label": labels[key],
                "count": len(entries),
                "message_ids": message_ids,
                "earliest_received_at": date_values[0] if date_values else None,
                "latest_received_at": date_values[-1] if date_values else None,
                "subject_examples": subjects[:5],
            }
        )
    return sorted(result, key=lambda group: (-group["count"], group["sender_key"]))


def _terms(value: Any) -> set[str]:
    text = str(value or "").casefold()
    terms: set[str] = set()
    for match in _WORD_RE.findall(text):
        if _CJK_RE.fullmatch(match):
            # Require a multi-character exact segment; overlapping bigrams are
            # useful for Chinese text without treating individual characters as evidence.
            terms.update(match[i : i + 2] for i in range(len(match) - 1))
        elif len(match) > 1:
            terms.add(match)
    return terms


def shortlist_matters(
    cards: list[dict], matters: list[dict], *, max_candidates: int = 3
) -> dict[str, list[dict]]:
    """Return existing linked matters first, then conservative lexical candidates."""
    limit = max(0, int(max_candidates))
    matter_rows: list[tuple[str, dict, set[str]]] = []
    for matter in matters:
        matter_id = matter.get("matter_id")
        if matter_id is None:
            continue
        matter_id = str(matter_id)
        terms = _terms(matter.get("title")) | _terms(matter.get("summary"))
        matter_rows.append((matter_id, matter, terms))

    output: dict[str, list[dict]] = {}
    for card in cards:
        raw_id = card.get("message_id")
        if raw_id is None:
            continue
        message_id = str(raw_id)
        linked: list[dict] = []
        lexical: list[tuple[float, str, dict]] = []
        card_terms = _terms(card.get("subject"))
        for matter_id, matter, terms in matter_rows:
            title = matter.get("title")
            summary = matter.get("summary")
            links = matter.get("source_links")
            if not isinstance(links, list):
                links = []
            has_exact_link = any(
                isinstance(link, dict)
                and link.get("source_type") == "mail_message"
                and str(link.get("source_id")) == message_id
                for link in links
            )
            if has_exact_link:
                linked.append(
                    {
                        "matter_id": matter_id,
                        "title": title,
                        "summary": summary,
                        "reason": "existing_source_link",
                        "score": 1.0,
                    }
                )
                continue
            shared = card_terms & terms
            if not shared or (all(_CJK_RE.fullmatch(term) for term in card_terms) and len(shared) < 2):
                continue
            score = len(shared) / len(card_terms | terms)
            lexical.append(
                (
                    score,
                    matter_id,
                    {
                        "matter_id": matter_id,
                        "title": title,
                        "summary": summary,
                        "reason": "lexical_candidate_needs_review",
                        "score": round(score, 6),
                    },
                )
            )
        linked.sort(key=lambda item: item["matter_id"])
        lexical.sort(key=lambda item: (-item[0], item[1]))
        output[message_id] = (linked + [item[2] for item in lexical])[:limit]
    return output
