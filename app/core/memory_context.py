"""Bounded, scope-filtered memory view for a root Agent turn."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.core.memory_extraction import preference_conflict_hints
from app.domains.memory import MemoryConflictError, MemoryRecord, MemoryService


class MemoryContextProvider:
    def __init__(self, service: MemoryService, *, max_items: int = 8, max_chars: int = 2400):
        self.service = service
        self.max_items = max_items
        self.max_chars = max_chars

    def __call__(self, session_id: str, workspace_path: str | None, query: str) -> dict[str, Any]:
        # Never use scope=None: it would include every project's private memory.
        project_id = (
            self.service.resolve_project(workspace_path, create=True)
            if workspace_path else None
        )
        records = self.service.list(scope="global", limit=30)
        if query.strip():
            records.extend(self.service.search(query, scope="global", limit=100))
        if project_id is not None:
            records.extend(self.service.list(scope="project", project_id=project_id, limit=30))
            if query.strip():
                records.extend(self.service.search(
                    query, scope="project", project_id=project_id, limit=100,
                ))
        # A memory found by both the recent and relevant paths counts once.
        records = list({item.memory_id: item for item in records}.values())
        terms = _query_terms(query)
        ranked: list[tuple[int, MemoryRecord]] = []
        withheld_conflicts = 0
        for item in records:
            if item.metadata.get("needs_review"):
                withheld_conflicts += 1
                continue
            if item.sensitivity not in {"normal", "public"}:
                continue
            folded = item.content.casefold()
            score = sum(3 if term in folded else 0 for term in terms)
            # Stable preferences guide arbitrary tasks, but an unrelated old
            # fact should not be stuffed into every new conversation merely
            # because it was recently learned.
            if item.memory_type == "user_fact" and not score and not re.search(
                r"记住了什么|记得什么|记忆|what.*remember|memories", query, re.IGNORECASE,
            ):
                continue
            if item.memory_type == "preference":
                score += 1
            if item.scope == "project":
                score += 2
            score += int((item.confidence or 0) * 2)
            try:
                age_days = max(0, (datetime.now(UTC) - datetime.fromisoformat(item.updated_at)).days)
                score += max(0, 2 - age_days // 180)
            except (TypeError, ValueError):
                pass
            ranked.append((score, item))
        ranked.sort(key=lambda row: (row[0], row[1].updated_at), reverse=True)
        selected: list[dict[str, Any]] = []
        used = 0
        for _, item in ranked:
            if len(selected) >= self.max_items:
                break
            content = item.content[:500]
            if used + len(content) > self.max_chars:
                continue
            used += len(content)
            selected.append({
                "memory_id": item.memory_id,
                "content": content,
                "scope": item.scope,
                "project_id": item.project_id,
                "source_ids": item.source_ids[:8],
                "source_count": len(item.source_ids),
                "sources_omitted": max(0, len(item.source_ids) - 8),
                "content_truncated": len(item.content) > len(content),
                "read_ref": f"memory:{item.memory_id}",
                "confidence": item.confidence,
                "updated_at": item.updated_at,
                "version": item.version,
            })
        return {
            "session_id": session_id,
            "project_id": project_id,
            "items": selected,
            "omitted_count": max(0, len(ranked) - len(selected)),
            "withheld_conflict_count": withheld_conflicts,
            "policy": "Derived user memory; lower priority than current user request and AGENTS.md.",
        }


_FORGET_ID = re.compile(
    r"(?:忘记|删除记忆|forget)\s*[：: ]*\b(mem_[A-Za-z0-9]+|[0-9a-f]{32})\b",
    re.IGNORECASE,
)
_FORGET_WORDING = re.compile(
    r"(?:忘记|忘掉|删除|撤回)(?:关于|掉|掉关于)?(?P<target>[^，。！？!?；;\n]{2,100}?)(?:这条|这个偏好|这项记忆|的记忆|记忆)?(?=$|[，。！？!?；;\n])"
    r"|(?:别再记|不要再记|不必记住)(?P<target2>[^，。！？!?；;\n]{2,100})",
    re.IGNORECASE,
)
_CORRECTION_CUE = re.compile(
    r"(?:我(?:刚才|之前|先前)?说错了|记错了|更正一下|纠正一下|其实我(?:不|并不)|"
    r"我不再(?:喜欢|希望|偏好|习惯)|改成|改为|以后别再)", re.IGNORECASE,
)


def _query_terms(query: str) -> list[str]:
    terms = [part.casefold() for part in re.findall(
        r"[a-z0-9_+-]{2,}|[\u3400-\u9fff]{2,}", query, re.IGNORECASE,
    )]
    expanded: list[str] = []
    for term in terms[:8]:
        expanded.append(term)
        if re.fullmatch(r"[\u3400-\u9fff]+", term):
            expanded.extend(term[index:index + 2] for index in range(len(term) - 1))
    return list(dict.fromkeys(expanded))[:24]


def _opposite_polarity(slot: str, left: str, right: str) -> bool:
    pairs = {
        "response_detail": {"concise", "detailed"},
        "response_language": {"chinese", "english"},
        "response_structure": {"bullets", "prose"},
    }
    allowed = pairs.get(slot)
    if allowed is None and slot.startswith("assertion:"):
        allowed = {"positive", "negative"}
    return allowed is not None and {left, right} == allowed


def _target_matches(target: str, content: str) -> bool:
    target_terms = _query_terms(target)
    if not target_terms:
        return False
    content_folded = content.casefold()
    matched = sum(term in content_folded for term in target_terms)
    # Require a specific lexical anchor and a strong overlap; vague referents
    # such as “that” or “this memory” must never retract anything.
    hanzi = "".join(re.findall(r"[\u3400-\u9fff]", target))
    minimum = 1 if len(hanzi) >= 4 else 2
    return matched >= minimum and any(len(t) >= 2 for t in target_terms)


class MemoryPreTurnGate:
    """Apply only unambiguous user-directed retractions before recall."""

    def __init__(self, service: MemoryService):
        self.service = service

    def __call__(self, workspace_path: str | None, user_input: str) -> list[str]:
        id_matches = list(_FORGET_ID.finditer(user_input))
        targets = [match.group("target") or match.group("target2")
                   for match in _FORGET_WORDING.finditer(user_input)]
        correction = bool(_CORRECTION_CUE.search(user_input))
        if not id_matches and not targets and not correction:
            return []
        project_id = (
            self.service.resolve_project(workspace_path, create=False)
            if workspace_path else None
        )
        retracted: list[str] = []
        requested_ids = [match.group(1) for match in id_matches]
        correction_hints = preference_conflict_hints(user_input) if correction else ()
        if targets or correction:
            # Natural-language suppression is deliberately bounded to active
            # global/current-project records and needs a strong claim match.
            candidates = self.service.list(scope="global", limit=1000)
            if project_id:
                candidates.extend(self.service.list(scope="project", project_id=project_id, limit=1000))
            for record in candidates:
                if any(_target_matches(target, record.content) for target in targets):
                    requested_ids.append(record.memory_id)
                elif correction:
                    old_hints = record.metadata.get("conflict_hints", [])
                    if not isinstance(old_hints, list):
                        old_hints = []
                    if correction_hints and isinstance(old_hints, list) and any(
                        isinstance(old, dict)
                        and old.get("slot") == new.slot
                        and old.get("condition", "") == new.condition
                        and _opposite_polarity(old.get("slot", ""), old.get("polarity", ""), new.polarity)
                        for old in old_hints for new in correction_hints
                    ):
                        requested_ids.append(record.memory_id)
                        continue
                    # Only an explicit negative clause can identify a rejected
                    # claim. New positive preferences and generic “I was wrong”
                    # clauses are not deletion targets.
                    clauses = [part for part in re.split(r"[，。！？!?；;\n]", user_input)
                               if re.search(r"(?:不再|不喜欢|不希望|并非|不是|别再|不要)", part)]
                    if any(_target_matches(clause, record.content) for clause in clauses):
                        requested_ids.append(record.memory_id)
        for memory_id in dict.fromkeys(requested_ids):
            try:
                record = self.service.get(memory_id)
            except KeyError:
                continue
            if record.status != "active":
                continue
            if record.scope == "project" and record.project_id != project_id:
                continue
            try:
                self.service.retract(memory_id, expected_version=record.version)
            except MemoryConflictError:
                continue
            retracted.append(memory_id)
        return retracted
