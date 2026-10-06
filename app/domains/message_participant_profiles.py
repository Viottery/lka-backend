"""Local, per-conversation replay statistics and conservative profile candidates.

No model, database, runtime or ReadingProfile dependencies. Identity aliases must
already represent reliable sender IDs, never display names. These structural and
lexical gates are deliberately conservative, not semantic verification: accepted
claims remain candidates for review. Callers supply authorized canonical evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DAY = 86400
BUCKET = 1800
FOCUS_WINDOW = 900
FOCUS_LABELS = ("technical_support", "project_collaboration", "interest", "social", "general")
ClaimKind = Literal["preference", "characteristic", "need", "communication_style",
                    "recurring_topic", "experience", "role", "context_event"]


class ParticipantClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sender: str = Field(min_length=1, max_length=256)
    kind: ClaimKind
    text: str = Field(min_length=1, max_length=1000)
    source_ids: list[str] = Field(min_length=1, max_length=20)
    quote: str = Field(min_length=1, max_length=4000)
    evidence_quotes: dict[str, str] = Field(default_factory=dict, max_length=20)
    facet: str | None = Field(default=None, min_length=1, max_length=120)
    basis: Literal["explicit", "observed", "uncertain"]
    valid_until: int | None
    supersedes: list[str] = Field(default_factory=list, max_length=12)


class FocusCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    focus: Literal["technical_support", "project_collaboration", "interest", "social", "general"]
    source_ids: list[str] = Field(min_length=1, max_length=50)
    quote: str = Field(min_length=1, max_length=4000)


class ProfileRevisionConflict(ValueError):
    """The caller's reviewed local revision no longer matches."""


def _event_time(message: dict, now: int) -> int | None:
    received = message.get("received_at")
    sent = message.get("sent_at")
    if type(received) is not int or received > now:
        return None
    event = sent if type(sent) is int else received
    return event if 0 <= event <= now else None


def _usable(message: dict, now: int) -> bool:
    return (
        isinstance(message.get("id"), str)
        and bool(message["id"])
        and isinstance(message.get("sender"), str)
        and bool(message["sender"])
        and isinstance(message.get("text"), str)
        and _event_time(message, now) is not None
    )


def _weight(timestamp: int, now: int) -> float:
    return 2 ** (-max(0, now - timestamp) / (7 * DAY))


def _direct(message: dict) -> bool:
    # Unknown/structured attribution stays out of stable self statements.
    if message.get("kind") != "text" or message.get("reply") is not None:
        return False
    parts = message.get("parts")
    if parts:
        if not isinstance(parts, list) or any(
            not isinstance(part, dict)
            or part.get("kind") != "text"
            or not isinstance(part.get("text"), str)
            for part in parts
        ):
            return False
        if "".join(part["text"] for part in parts) != message["text"]:
            return False
    text = message["text"].strip()
    return not re.search(
        r"[\"“”「」]|(^|\n)\s*>|转发|引用|他说|她说|有人说|said|says", text, re.IGNORECASE
    )


_DISALLOWED = re.compile(
    r"政治|宗教|种族|民族|性取向|性倾向|性癖|性偏好|性嗜好|恋童|恋幼|萝莉控|正太控|"
    r"幼女控|幼男控|性欲|性幻想|pedophil|paedophil|"
    r"疾病|抑郁|诊断|人格|性格|人品|智商|"
    r"愚蠢|懒惰|自私|暴躁|蠢|religio|ethnic|race|sexual|diagnos|depress|"
    r"personality|lazy|stupid|selfish|bipolar|autis",
    re.IGNORECASE,
)
_SELF = re.compile(
    r"^((?:(?:平时|最近|目前|现在|工作时|休息时|购物时|干喝|写代码时)[，,：:\s]*)?"
    r"我(?:只(?:喝|用|看|买|吃)|喜欢|偏好|需要|想要|希望|正在|最近|目前|常用|习惯|关注|寻找|是|在|曾|做|负责|刚|已经|之前|有|用)|"
    r"I (?:prefer|like|need|want|am|was|have|work|worked|usually use)\b)",
    re.IGNORECASE,
)


@dataclass
class _Participant:
    buckets: dict[int, int] = field(default_factory=dict)
    claims: list[dict] = field(default_factory=list)
    revision: int = 0
    pinned: bool = False
    hidden: bool = False
    suppressed: bool = False
    correction: str | None = None
    topics: dict[str, int] = field(default_factory=dict)


class ParticipantProfileIndex:
    """Single-group, in-memory index; user edits require revision CAS.

    Ordinary pool capacity excludes at most ten pinned people. Profile keys are
    (conversation_id, sender); the supplied sender aliases must not be nicknames.
    Future-dated rows are skipped and may be observed again when clock catches up.
    """

    def __init__(self, conversation_id: str, capacity: int = 30, *, pinned_capacity: int = 10,
                 cold_days: int = 14, retention_days: int = 30):
        if not conversation_id or type(capacity) is not int or capacity < 1:
            raise ValueError("invalid_profile_index_configuration")
        self.conversation_id = conversation_id
        self.capacity = capacity
        self.pinned_capacity = pinned_capacity
        self.cold_days = cold_days
        self.retention_days = retention_days
        self._people: dict[str, _Participant] = {}
        self._seen: dict[str, tuple[str, int]] = {}
        self._pool: set[str] = set()
        self._promotion: dict[str, int] = {}
        self._last_recompute: int | None = None
        self.last_rejection_reason: str | None = None
        self.observed_seq = 0
        self._deferred: dict[str, int] = {}

    def observe(self, messages: list[dict], now: int) -> None:
        self.cleanup(now)
        for message in messages:
            if not _usable(message, now):
                continue
            if _event_time(message, now) < now - self.retention_days * DAY:
                continue
            signature = hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            old = self._seen.get(message["id"])
            if old is not None:
                if old[0] != signature:
                    raise ValueError("message_alias_collision")
                continue
            if len(self._seen) >= 20000:
                self._seen.pop(min(self._seen, key=lambda key: (self._seen[key][1], key)))
            self._seen[message["id"]] = (signature, _event_time(message, now))
            if message["sender"] not in self._people and len(self._people) >= 1000:
                continue
            person = self._people.setdefault(message["sender"], _Participant())
            timestamp = _event_time(message, now)
            bucket = timestamp // BUCKET
            person.buckets.setdefault(bucket, timestamp)
            self.observed_seq = max(self.observed_seq, message.get("seq", 0))

    def cleanup(self, now: int) -> None:
        cutoff = now - self.retention_days * DAY
        self._seen = {k: v for k, v in self._seen.items() if v[1] >= cutoff}
        for sender, person in list(self._people.items()):
            person.buckets = {k: v for k, v in person.buckets.items() if v >= cutoff}
            person.topics = {k: v for k, v in person.topics.items() if v >= cutoff}
            person.claims = [c for c in person.claims if c["created_at"] >= cutoff][-12:]
            if not person.buckets and not person.claims and not (person.pinned or person.hidden or person.suppressed or person.correction is not None):
                del self._people[sender]
                self._pool.discard(sender)
                self._promotion.pop(sender, None)

    def observe_topics(self, topics: list[dict], messages: list[dict], now: int) -> None:
        sources = {m["id"]: m for m in messages if _usable(m, now)}
        for topic in topics[:40]:
            key = topic.get("existing_topic_id") or topic.get("batch_local_key")
            for alias in (topic.get("member_message_ids") or topic.get("source_message_ids", []))[:200]:
                row = sources.get(alias)
                if key and row and row["sender"] in self._people:
                    person = self._people[row["sender"]]
                    person.topics[key] = max(person.topics.get(key, 0), _event_time(row, now))
                    person.topics = dict(sorted(person.topics.items(), key=lambda item: (-item[1], item[0]))[:100])

    @staticmethod
    def _eligible(person: _Participant, now: int) -> bool:
        times = [t for t in person.buckets.values() if t <= now]
        return len(times) >= 3 and max(times) - min(times) >= 6 * 3600

    @staticmethod
    def _score(person: _Participant, now: int) -> float:
        times = [t for t in person.buckets.values() if t <= now]
        buckets = sum(_weight(t, now) for t in times)
        days: dict[int, int] = {}
        for t in times:
            days[t // DAY] = max(days.get(t // DAY, 0), t)
        days_score = sum(_weight(t, now) for t in days.values())
        # Available components reweighted; topic participation is missing, not zero.
        base = 0.45 * (1 - math.exp(-buckets / 6)) + 0.35 * (1 - math.exp(-days_score / 3))
        if person.topics:
            return base + 0.20 * (1 - math.exp(-sum(_weight(t, now) for t in person.topics.values()) / 3))
        return base / 0.8

    def _rank(self, sender: str, now: int) -> tuple:
        person = self._people[sender]
        return (-self._score(person, now), -max(person.buckets.values(), default=0), sender)

    def _recompute(self, now: int) -> None:
        if self._last_recompute is not None and now - self._last_recompute < 6 * 3600:
            return
        self._last_recompute = now
        eligible = {
            s
            for s, p in self._people.items()
            if not p.hidden
            and not p.suppressed
            and not p.pinned
            and p.buckets
            and now - max(p.buckets.values()) < self.cold_days * DAY
            and self._eligible(p, now)
        }
        self._pool &= eligible
        for sender in sorted(eligible - self._pool, key=lambda s: self._rank(s, now)):
            if len(self._pool) < self.capacity:
                self._pool.add(sender)
                continue
            lowest = max(self._pool, key=lambda s: self._rank(s, now))
            if self._score(self._people[sender], now) > 1.25 * self._score(
                self._people[lowest], now
            ):
                self._promotion[sender] = self._promotion.get(sender, 0) + 1
                if self._promotion[sender] >= 2:
                    self._pool.remove(lowest)
                    self._pool.add(sender)
                    self._promotion.pop(sender, None)
            else:
                self._promotion.pop(sender, None)

    def profile(self, sender: str, now: int) -> dict:
        person = self._people.get(sender, _Participant())
        claims = deepcopy([c for c in person.claims if c["created_at"] <= now])
        for claim in claims:
            claim["status"] = "stale" if now >= claim["expires_at"] else claim.get("status", "candidate")
        active = [c for c in claims if c["status"] == "candidate" and c["basis"] != "uncertain"]
        cold = bool(person.buckets) and now - max(person.buckets.values()) >= self.cold_days * DAY
        status = (
            "suppressed"
            if person.suppressed
            else "hidden"
            if person.hidden
            else "pinned"
            if person.pinned
            else "cold"
            if cold
            else "hot"
            if sender in self._pool
            else "activity_only"
        )
        return {
            "conversation_id": self.conversation_id,
            "sender": sender,
            "identity": [self.conversation_id, sender],
            "revision": person.revision,
            "score": self._score(person, now),
            "score_version": "activity_topics_v2" if person.topics else "activity_v1_no_topics",
            "missing_components": [] if person.topics else ["topics"],
            "topic_participation": len(person.topics),
            "activity_observed_seq": self.observed_seq,
            "active_buckets": sum(t <= now for t in person.buckets.values()),
            "status": status,
            "claims": claims,
            "summary": (
                person.correction
                if person.correction is not None
                else "；".join(
                    (c["text"] if c.get("evidence_quotes") or c["kind"] not in
                     ("preference", "characteristic", "need") else
                     ("近期多次讨论：" if c["basis"] == "observed" else "") + c["quote"])
                    for c in active
                )
            )[:240]
            if not person.hidden
            and not person.suppressed
            and (person.correction is not None or person.pinned or sender in self._pool)
            else "",
        }

    def hot_profiles(self, now: int) -> list[dict]:
        self._recompute(now)
        senders = self._pool | {s for s, p in self._people.items() if p.pinned}
        return [
            self.profile(s, now)
            for s in sorted(senders, key=lambda s: self._rank(s, now))
            if not self._people[s].hidden
            and not self._people[s].suppressed
            and (self._people[s].pinned or now - max(self._people[s].buckets.values()) < self.cold_days * DAY)
        ]

    def candidates(self, now: int) -> list[dict]:
        """Includes important needs outside hot pool, without promoting the author."""
        return [
            self.profile(s, now)
            for s, p in sorted(self._people.items())
            if p.claims and not p.hidden and not p.suppressed
        ]

    def _reject(self, reason: str) -> bool:
        self.last_rejection_reason = reason
        return False

    def add_claim(self, claim: dict, messages: list[dict], now: int) -> bool:
        self.last_rejection_reason = None
        try:
            candidate = ParticipantClaim.model_validate(claim)
        except ValidationError:
            return self._reject("invalid_claim_schema")
        if _DISALLOWED.search(candidate.text + " " + candidate.quote + " " +
                              " ".join(candidate.evidence_quotes.values())):
            return self._reject("sensitive_or_personality_claim")
        sources = {m.get("id"): m for m in messages}
        if len(sources) != len(messages) or len(set(candidate.source_ids)) != len(
            candidate.source_ids
        ):
            return self._reject("ambiguous_source_alias")
        evidence = [sources.get(alias) for alias in candidate.source_ids]
        if any(
            m is None or not _usable(m, now) or m["sender"] != candidate.sender for m in evidence
        ):
            return self._reject("source_author_or_time_mismatch")
        if candidate.evidence_quotes and (set(candidate.evidence_quotes) != set(candidate.source_ids)
                or any(not quote or len(quote) > 4000 for quote in candidate.evidence_quotes.values())):
            return self._reject("invalid_evidence_quotes")
        quotes = [candidate.evidence_quotes.get(m["id"], candidate.quote) for m in evidence]
        if candidate.evidence_quotes and candidate.quote not in quotes:
            return self._reject("representative_quote_not_in_evidence")
        # Behavioral observations may use authored replies; quoted/forwarded
        # content remains ambiguous and cannot support an observation.
        behavioral = candidate.kind in ("communication_style", "recurring_topic")
        if any(not _direct({**m, "reply": None} if candidate.basis == "observed" or behavioral else m)
               for m in evidence):
            return self._reject("quoted_forwarded_or_ambiguous_attribution")
        if any(quote not in m["text"] for quote, m in zip(quotes, evidence)):
            return self._reject("quote_not_exact_in_each_source")
        if candidate.basis == "explicit":
            if candidate.kind in ("communication_style", "recurring_topic"):
                return self._reject("behavior_requires_observation")
            if not _SELF.match(candidate.quote) or any(
                m["text"].strip() != candidate.quote for m in evidence
            ):
                return self._reject("not_direct_self_statement")
            if candidate.text != candidate.quote:
                return self._reject("unverified_paraphrase")
        if candidate.basis == "observed":
            if len({_event_time(m, now) // BUCKET for m in evidence}) < 2:
                return self._reject("observed_requires_independent_windows")
            if candidate.kind in ("experience", "role", "context_event"):
                return self._reject("self_report_requires_explicit_basis")
            if candidate.kind in ("preference", "characteristic", "need") and not candidate.text.startswith("近期多次讨论"):
                return self._reject("observed_requires_temporary_wording")
        if behavioral and candidate.basis == "uncertain" and (
                len(evidence) < 2 or len({_event_time(m, now) for m in evidence}) < 2):
            return self._reject("tentative_behavior_requires_distinct_messages")
        if candidate.sender not in self._people and len(self._people) >= 1000:
            return self._reject("participant_capacity")
        person = self._people.setdefault(candidate.sender, _Participant())
        if person.suppressed or person.hidden:
            return self._reject("profile_suppressed")
        payload = candidate.model_dump()
        if candidate.valid_until is not None and candidate.valid_until <= now:
            return self._reject("claim_already_expired")
        prior = {c.get("claim_id"): c for c in person.claims}
        if candidate.supersedes and (candidate.basis != "explicit" or any(
            key not in prior or prior[key]["kind"] != candidate.kind for key in candidate.supersedes
        )):
            return self._reject("invalid_claim_supersession")
        if any(all(c.get(k) == v for k, v in payload.items()) for c in person.claims):
            return True
        payload.update(
            {
                "created_at": now,
                "expires_at": candidate.valid_until
                if candidate.valid_until is not None
                else now + (7 * DAY if candidate.kind == "need" else 30 * DAY),
                "validation": "structural_candidate_not_semantic_verification",
                "claim_id": "participant_claim_" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                "status": "candidate",
            }
        )
        for old in person.claims:
            if old.get("claim_id") in candidate.supersedes:
                old["status"] = "superseded"
            elif (candidate.kind in ("preference", "characteristic", "need") and
                  candidate.basis == old["basis"] == "explicit" and old["kind"] == candidate.kind
                  and candidate.facet == old.get("facet")
                  and old["text"] != candidate.text and old["expires_at"] > now and old.get("status") != "superseded"):
                old["status"] = payload["status"] = "contested"
        person.claims.append(payload)
        person.claims.sort(key=lambda c: (c["created_at"], c["claim_id"]))
        if sum(c["basis"] == "uncertain" for c in person.claims) > 3:
            person.claims.remove(next(c for c in person.claims if c["basis"] == "uncertain"))
        if len(person.claims) > 12:
            person.claims.pop(0)
        person.revision += 1
        return True

    def _edit(self, sender: str, expected_revision: int) -> _Participant:
        person = self._people.setdefault(sender, _Participant())
        if person.revision != expected_revision:
            raise ProfileRevisionConflict("profile_revision_conflict")
        return person

    def pin(self, sender: str, pinned: bool, *, expected_revision: int) -> int:
        person = self._edit(sender, expected_revision)
        if pinned and not person.pinned and sum(p.pinned for p in self._people.values()) >= self.pinned_capacity:
            raise ValueError("pinned_capacity")
        if person.suppressed and pinned:
            raise ValueError("profile_suppressed")
        person.pinned = pinned
        self._pool.discard(sender)
        person.revision += 1
        return person.revision

    def hide(self, sender: str, hidden: bool, *, expected_revision: int) -> int:
        person = self._edit(sender, expected_revision)
        person.hidden = hidden
        self._pool.discard(sender)
        person.revision += 1
        return person.revision

    def delete(self, sender: str, *, expected_revision: int) -> int:
        person = self._edit(sender, expected_revision)
        person.claims.clear()
        person.correction = None
        person.suppressed = True
        person.pinned = False
        self._pool.discard(sender)
        person.revision += 1
        return person.revision

    def correct(self, sender: str, summary: str, *, expected_revision: int) -> int:
        if not isinstance(summary, str) or len(summary) > 240:
            raise ValueError("invalid_correction")
        person = self._edit(sender, expected_revision)
        person.correction = summary
        person.revision += 1
        return person.revision


class ConversationFocusTracker:
    """Deterministic merge of upstream topic candidates; no topic inference here."""

    def __init__(self, conversation_id: str):
        if not conversation_id:
            raise ValueError("conversation_id_required")
        self.conversation_id = conversation_id
        self.revision = 0
        self.mode = "auto"
        self._evidence: dict[str, dict[str, dict]] = {}
        self._labels: list[str] = []
        self._last_merge: int | None = None

    def observe(self, candidates: list[dict], messages: list[dict], now: int) -> None:
        self.cleanup(now)
        sources = {m.get("id"): m for m in messages}
        if len(sources) != len(messages):
            raise ValueError("ambiguous_source_alias")
        for value in candidates:
            candidate = FocusCandidate.model_validate(value)
            evidence = [sources.get(alias) for alias in candidate.source_ids]
            if any(m is None or not _usable(m, now) for m in evidence):
                raise ValueError("focus_source_or_time_mismatch")
            if any(candidate.quote not in m["text"] for m in evidence):
                raise ValueError("focus_quote_not_exact")
            target = self._evidence.setdefault(candidate.focus, {})
            for m in evidence:
                stored = {**deepcopy(m), "text": "", "parts": [],
                          "evidence_digest": hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()}
                if m["id"] in target and target[m["id"]] != stored:
                    raise ValueError("message_alias_collision")
                target[m["id"]] = stored
            if len(target) > 1000:
                self._evidence[candidate.focus] = dict(sorted(target.items(), key=lambda item: (-_event_time(item[1], now), item[0]))[:1000])

    def cleanup(self, now: int) -> None:
        for label, evidence in list(self._evidence.items()):
            self._evidence[label] = {key: row for key, row in evidence.items()
                                     if (_event_time(row, now) or 0) >= now - 30 * DAY}

    def _support(self, label: str, now: int) -> dict:
        rows = [m for m in self._evidence.get(label, {}).values() if _usable(m, now)]
        windows: dict[int, int] = {}
        for m in rows:
            t = _event_time(m, now)
            windows[t // FOCUS_WINDOW] = max(windows.get(t // FOCUS_WINDOW, 0), t)
        return {
            "windows": sorted(windows),
            "senders": len({m["sender"] for m in rows}),
            "score": sum(_weight(t, now) for t in windows.values()),
            "source_ids": sorted(m["id"] for m in rows)[:50],
            "span": max(windows.values(), default=0) - min(windows.values(), default=0),
        }

    def merge(self, now: int) -> dict:
        if self.mode == "manual" or (self._last_merge is not None and now - self._last_merge < DAY):
            return self.snapshot(now)
        self._last_merge = now
        labels = []
        for label in FOCUS_LABELS:
            support = self._support(label, now)
            threshold = 1.5 if label in self._labels else 2.5
            if (
                len(support["windows"]) >= 3
                and support["senders"] >= 2
                and support["score"] >= threshold
            ):
                labels.append(label)
        self._labels = sorted(labels, key=lambda label: -self._support(label, now)["score"])[:4]
        self.revision += 1
        return self.snapshot(now)

    def _cas(self, expected_revision: int) -> None:
        if self.revision != expected_revision:
            raise ProfileRevisionConflict("focus_revision_conflict")

    def set_manual(self, labels: list[str], *, expected_revision: int) -> int:
        self._cas(expected_revision)
        if (
            len(labels) > 4
            or len(set(labels)) != len(labels)
            or any(l not in FOCUS_LABELS for l in labels)
        ):
            raise ValueError("invalid_focus_labels")
        self.mode = "manual"
        self._labels = list(labels)
        self.revision += 1
        return self.revision

    def resume_auto(self, *, expected_revision: int) -> int:
        self._cas(expected_revision)
        self.mode = "auto"
        self._labels = []
        self._last_merge = None
        self.revision += 1
        return self.revision

    def snapshot(self, now: int) -> dict:
        items = []
        for label in self._labels:
            support = self._support(label, now)
            items.append(
                {
                    "label": label,
                    "source_windows": support["windows"],
                    "source_ids": support["source_ids"],
                    "confidence_label": "manual"
                    if self.mode == "manual"
                    else "supported"
                    if support["span"] >= DAY
                    else "low_support",
                    "sample_coverage": "provided_candidates_only",
                }
            )
        return {
            "conversation_id": self.conversation_id,
            "mode": self.mode,
            "revision": self.revision,
            "updated_at": self._last_merge,
            "focus": items,
            "fallback": None if items else "unknown",
            "transient_topics": [
                l
                for l in FOCUS_LABELS
                if l not in self._labels and self._support(l, now)["score"] >= 0.5
            ],
        }
