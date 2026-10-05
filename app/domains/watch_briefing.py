"""Deterministic normalization and comparison for watch briefings."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _item_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key, "")) for key in ("claim", "title", "summary", "note", "status")
    ).strip()


def _rule_terms(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.casefold()] if value.strip() else []
    if isinstance(value, list):
        return [item.casefold() for item in value if isinstance(item, str) and item.strip()]
    return []


def _normalized_text(value: str) -> str:
    # Signs, currency, fractions, filenames and punctuation can carry facts.
    # Only formatting whitespace/case may vary; do not erase those distinctions.
    return " ".join(value.casefold().split())


def _excerpt_supports(claim: str, excerpt: str) -> bool:
    normalized_claim = _normalized_text(claim)
    if not normalized_claim or not _normalized_text(excerpt):
        return False
    # Require a whole source line/sentence/clause, not a substring that can
    # remove qualifiers or turn "unapproved" into "approved". This verifies
    # literal attribution, NOT entailment or the truth of quoted source text.
    # A period inside a number/version/path, or an ellipsis, is not a sentence
    # boundary: cutting there can silently change the quoted value/qualification.
    candidates = [excerpt, *excerpt.splitlines(),
                  *re.split(r"[;。；\r\n]+|(?<!\.)\.(?!\.)(?=\s|$)", excerpt)]
    return any(
        normalized_claim == _normalized_text(candidate)
        and bool(re.search(r"[?？]", claim)) == bool(re.search(r"[?？]", candidate))
        for candidate in candidates
    )


def reconcile_current_observations(
    changed: list[dict[str, Any]],
    unchanged: list[dict[str, Any]],
    prior_by_key: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconcile already-verified identities, never infer equivalence or timestamp authority."""
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for section, items in (("changes", changed), ("unchanged", unchanged)):
        for item in items:
            if isinstance(item, dict):
                key = item.get("event_key") or item.get("fingerprint")
                if isinstance(key, str) and key:
                    groups.setdefault(key, []).append((section, item))

    def observation(item: dict[str, Any]) -> str:
        return str(item.get("current_observation") or item.get("claim")
                   or item.get("title") or item.get("status") or "")

    current: dict[str, list[dict[str, Any]]] = {"changes": [], "unchanged": []}
    conflicts: list[dict[str, Any]] = []
    for key, entries in groups.items():
        values = {observation(item).casefold() for _, item in entries}
        if len(values) == 1:
            section, item = entries[0]
            current[section].append(item)
            continue
        prior = prior_by_key.get(key, {})
        old_value = observation(prior)
        baseline_entries = [item for _, item in entries
                            if observation(item).casefold() == old_value.casefold()]
        new_entries = [item for _, item in entries
                       if observation(item).casefold() != old_value.casefold()]
        prior_refs = prior.get("evidence_refs", [])
        prior_refs = [prior_refs] if isinstance(prior_refs, str) else prior_refs
        # A newly read independent source repeating the old value is a conflict,
        # not proof that it is merely history. Only the actual baseline refs qualify.
        if (old_value and len(values) == 2 and baseline_entries and new_entries
                and prior_refs and all(
                    item.get("evidence_refs")
                    and all(ref in prior_refs for ref in item["evidence_refs"])
                    for item in baseline_entries)):
            current["changes"].append({**new_entries[0], "previous_observation": old_value})
            continue
        conflicts.extend({**item, "reason": "current_state_conflict"} for _, item in entries)
    return current["changes"], current["unchanged"], conflicts


def _briefing_summary(
    *,
    changes: list[dict[str, Any]],
    unchanged: list[dict[str, Any]],
    unconfirmed: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    max_chars: int = 2200,
) -> str:
    sections = (
        ("新增/变化", changes),
        ("未变化", unchanged),
        ("无法确认", unconfirmed),
        ("需决定", decisions),
    )
    lines = [
        f"每日关注：变化 {len(changes)} 项；未变化 {len(unchanged)} 项；无法确认 {len(unconfirmed)} 项；需决定 {len(decisions)} 项。"
    ]
    if any(item.get("reason") == "child_budget_finish" for item in unconfirmed):
        # A server-generated coverage warning must precede per-section and overall caps.
        lines.append("覆盖不足（partial / child_budget_finish）：Evidence collection stopped "
                     "at the child budget; coverage is incomplete and unchecked information "
                     "remains unknown.")
    for title, items in sections:
        lines.append(f"\n{title}：")
        if not items:
            lines.append("- 无")
            continue
        for item in items[:4]:
            refs = item.get("evidence_refs", [])
            refs = [refs] if isinstance(refs, str) else refs
            refs = [ref for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []
            item_title = item.get("title")
            title_supported = isinstance(item_title, str) and any(
                any(ref in (source.get("ref"), source.get("evidence_id")) for ref in refs)
                and _excerpt_supports(item_title, str(source.get("excerpt") or ""))
                for source in evidence
            )
            conclusion = str(
                item.get("current_observation")
                or item.get("claim")
                or item.get("title")
                or item.get("note")
                or "待核实信息"
            )
            subject = str((item_title if title_supported else None) or item.get("claim") or "关注项")
            if subject != conclusion:
                conclusion = f"{subject}：{conclusion}"
            if item.get("importance_filtered"):
                conclusion += "（按重要性规则折叠）"
            if item.get("reason"):
                conclusion += f"；说明：{item['reason']}"
            source_by_ref = {
                str(value): str(source.get("ref") or value)
                for source in evidence
                for value in (source.get("ref"), source.get("evidence_id"))
                if value
            }
            source = (
                ", ".join(source_by_ref[ref] for ref in refs[:2] if ref in source_by_ref)
                if item.get("evidence_check") == "excerpt_match"
                else "声明引用未通过来源/摘录核验"
            )
            line = f"- {conclusion[:260]}" + (f"；来源：{source[:240]}" if source else "")
            lines.append(line)
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= max_chars else rendered[: max_chars - 1].rstrip() + "…"


def _parse_summary(summary: str) -> dict[str, Any] | None:
    candidate = summary.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _optional_metadata_is_typed(item: dict[str, Any]) -> bool:
    if any(key in item and not isinstance(item[key], str)
           for key in ("event_id", "event_key", "title", "status", "note")):
        return False
    if "importance" in item:
        score = item["importance"]
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or isinstance(score, float) and not math.isfinite(score)):
            return False
    return True


def is_complete_briefing_payload(summary: str) -> bool:
    """Admission for budget-partial delivery; this does not verify its claims."""
    parsed = _parse_summary(summary)
    sections = ("changes", "unchanged", "unconfirmed", "decisions")
    if (parsed is None or set(parsed) != {"summary", *sections}
            or not isinstance(parsed["summary"], str) or not parsed["summary"].strip()):
        return False
    for section in sections:
        items = parsed[section]
        if not isinstance(items, list):
            return False
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("claim"), str)
                    or not item["claim"].strip() or not isinstance(item.get("evidence_refs"), list)
                    or any(not isinstance(ref, str) or not ref.strip() for ref in item["evidence_refs"])
                    or section != "unconfirmed" and not item["evidence_refs"]):
                return False
            if not _optional_metadata_is_typed(item):
                return False
            if "subject_key" in item and (
                not isinstance(item["subject_key"], str)
                or not item["subject_key"].strip() or len(item["subject_key"]) > 200
            ):
                return False
            if "current_observation" in item and (
                not isinstance(item["current_observation"], str)
                or not item["current_observation"].strip()
            ):
                return False
    return any(parsed[section] for section in sections)


def normalize_briefing(
    *,
    summary: str,
    evidence: list[dict[str, Any]],
    previous: dict[str, Any] | None,
    importance_rules: dict[str, Any],
    retrieval_failures: list[str] | tuple[str, ...] = (),
    coverage_incomplete: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Accept only structured, cited claims; classify all other conclusions as unconfirmed."""
    parsed = _parse_summary(summary)
    sources = {
        str(value)
        for item in evidence
        for value in (item.get("ref"), item.get("evidence_id"))
        if isinstance(value, str) and value
    }
    old_changed, old_unchanged, _ = reconcile_current_observations(
        (previous or {}).get("changes", []), (previous or {}).get("unchanged", []), {}
    )
    old_items = [*old_changed, *old_unchanged]
    prior_by_key: dict[str, dict[str, Any]] = {}
    for item in old_items:
        if isinstance(item, dict):
            key = item.get("event_key") or item.get("fingerprint")
            if key:
                prior_by_key[str(key)] = item
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    unconfirmed: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    if parsed is None:
        unconfirmed.append(
            {
                "claim": summary.strip() or "No usable briefing was produced.",
                "reason": "Briefing was not valid structured JSON.",
            }
        )
    else:
        for section, target in (
            ("changes", changed),
            ("unchanged", unchanged),
            ("unconfirmed", unconfirmed),
            ("decisions", decisions),
        ):
            values = parsed.get(section, [])
            if not isinstance(values, list):
                unconfirmed.append(
                    {"claim": f"Invalid {section} section.", "reason": "Expected an array."}
                )
                continue
            for value in values[:50]:
                item_target = target
                if not isinstance(value, dict):
                    unconfirmed.append(
                        {"claim": str(value)[:1000], "reason": "Evidence item is not an object."}
                    )
                    continue
                item = dict(value)
                # These are normalization outputs, never model display authority.
                for key in ("reason", "freshness", "importance_filtered", "previous_observation"):
                    item.pop(key, None)
                if not _optional_metadata_is_typed(item):
                    item["evidence_check"] = "invalid_metadata"
                    item["reason"] = "Optional metadata has an invalid type or nonfinite importance."
                    unconfirmed.append(item)
                    continue
                refs = item.get("evidence_refs", [])
                if isinstance(refs, str):
                    refs = [refs]
                if (
                    not isinstance(refs, list)
                    or not refs
                    or any(str(ref) not in sources for ref in refs)
                ):
                    item["evidence_check"] = "citation_mismatch"
                    item["reason"] = (
                        "Citation is missing or does not match a collected evidence reference."
                    )
                    unconfirmed.append(item)
                    continue
                text = _item_text(item)
                subject_key = item.get("subject_key")
                if "subject_key" in item and (
                    not isinstance(subject_key, str) or not subject_key.strip()
                    or len(subject_key) > 200
                ):
                    item["evidence_check"] = "invalid_identity"
                    item["reason"] = "subject_key must be a nonempty raw identity of at most 200 characters."
                    unconfirmed.append(item)
                    continue
                # Identity is grouping metadata, never source/permission authority.
                canonical = item.get("event_key")
                if subject_key is not None:
                    key_material = subject_key.casefold().strip()
                    key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
                elif (not item.get("event_id") and isinstance(canonical, str)
                      and re.fullmatch(r"[0-9a-f]{64}", canonical)
                      and canonical in prior_by_key):
                    key = canonical
                else:
                    key_material = str(item.get("event_id") or canonical or text).casefold().strip()
                    key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
                item["event_key"] = key
                if section in {"changes", "unchanged"}:
                    if "current_observation" not in item:
                        item["current_observation"] = (
                            item.get("claim") or item.get("title") or item.get("status")
                        )
                    prior_item = prior_by_key.get(key)
                    if prior_item is not None:
                        old_value = (
                            prior_item.get("current_observation")
                            or prior_item.get("claim")
                            or prior_item.get("title")
                            or prior_item.get("status")
                        )
                        if old_value is not None:
                            item["previous_observation"] = old_value
                        if str(old_value).casefold() == str(item["current_observation"]).casefold():
                            item_target = unchanged
                        elif section == "unchanged":
                            # A changed value cannot be retained as "unchanged".
                            item_target = changed
                        else:
                            item_target = changed
                item["evidence_check"] = "excerpt_match"
                claim_to_check = item.get("claim", item.get("observation")
                    or item.get("current_observation") or item.get("title") or "")
                refs = item.get("evidence_refs", [])
                refs = [refs] if isinstance(refs, str) else refs
                cited_evidence = [
                    record
                    for record in evidence
                    if any(ref in {record.get("ref"), record.get("evidence_id")} for ref in refs)
                ]
                excerpts = [str(record.get("excerpt") or "") for record in cited_evidence]
                claim_supported = isinstance(claim_to_check, str) and any(
                    _excerpt_supports(claim_to_check, excerpt) for excerpt in excerpts if excerpt
                )
                observation = item.get("current_observation")
                observation_supported = "current_observation" not in item or (
                    isinstance(observation, str) and any(
                    _excerpt_supports(observation, excerpt)
                    for excerpt in excerpts if excerpt
                ))
                if not claim_supported or not observation_supported:
                    item["evidence_check"] = "unsupported_or_excerpt_missing"
                    item["reason"] = (
                        "Citation identifies a read source, but the claim or current observation could not be matched to its available excerpt."
                    )
                    unconfirmed.append(item)
                    continue
                item_target.append(item)

    if coverage_incomplete:
        unconfirmed.append({
            "claim": "Evidence collection stopped at the child budget; coverage is incomplete and unchecked information remains unknown.",
            "reason": "child_budget_finish", "evidence_refs": [], "task_status": "partial",
        })

    for failure in retrieval_failures:
        unconfirmed.append(
            {
                "claim": "One or more authorized sources could not be checked.",
                "reason": f"Retrieval failed: {failure}",
            }
        )

    if (
        parsed is not None
        and not any(parsed.get(key) for key in ("changes", "unchanged", "unconfirmed", "decisions"))
        and not retrieval_failures
    ):
        unconfirmed.append(
            {
                "claim": "No verifiable observations were returned.",
                "reason": "An empty briefing cannot establish that nothing changed.",
            }
        )

    changed, unchanged, conflicts = reconcile_current_observations(changed, unchanged, prior_by_key)
    unconfirmed.extend(conflicts)

    include = _rule_terms(importance_rules.get("include_keywords"))
    exclude = _rule_terms(importance_rules.get("exclude_keywords"))
    minimum = importance_rules.get("minimum_importance")
    retained = []
    for item in changed:
        text = _item_text(item).casefold()
        score = item.get("importance")
        qualifies = (not include or any(word in text for word in include)) and not any(
            word in text for word in exclude
        )
        if isinstance(minimum, (int, float)):
            qualifies = qualifies and isinstance(score, (int, float)) and score >= minimum
        if qualifies:
            retained.append(item)
        else:
            item["importance_filtered"] = True
            unchanged.append(item)
    changed = retained

    # Freshness is metadata, not inferred from the wording of an LLM claim.
    current = (now or datetime.now(UTC)).astimezone(UTC)
    max_age = importance_rules.get("max_age_hours")
    if isinstance(max_age, (int, float)) and max_age > 0:
        fresh_evidence = []
        for item in evidence:
            stamp = item.get("source_time") or item.get("fetched_at")
            try:
                observed = datetime.fromisoformat(str(stamp))
                if (
                    observed.tzinfo is not None
                    and (current - observed.astimezone(UTC)).total_seconds() <= max_age * 3600
                ):
                    fresh_evidence.append(item)
            except (TypeError, ValueError):
                continue
        fresh_refs = {
            str(v)
            for item in fresh_evidence
            for v in (item.get("ref"), item.get("evidence_id"))
            if v
        }
        for item in [*changed, *unchanged]:
            refs = item.get("evidence_refs", [])
            refs = [refs] if isinstance(refs, str) else refs
            if not refs or not all(str(ref) in fresh_refs for ref in refs):
                item["freshness"] = "stale_or_unknown"
                item["reason"] = (
                    "Source timestamp is outside the configured freshness window or unavailable."
                )
                unconfirmed.append(item)
        changed = [item for item in changed if item.get("freshness") != "stale_or_unknown"]
        unchanged = [item for item in unchanged if item.get("freshness") != "stale_or_unknown"]

    for item in [*changed, *unchanged]:
        refs = item.get("evidence_refs", [])
        refs = [refs] if isinstance(refs, str) else refs
        matched = [
            source
            for source in evidence
            if any(ref in {source.get("ref"), source.get("evidence_id")} for ref in refs)
        ]
        item.setdefault(
            "freshness",
            {
                "source_times": sorted(
                    {str(source["source_time"]) for source in matched if source.get("source_time")}
                ),
                "fetched_times": sorted(
                    {str(source["fetched_at"]) for source in matched if source.get("fetched_at")}
                ),
                "status": "timestamped"
                if any(source.get("source_time") or source.get("fetched_at") for source in matched)
                else "unknown",
            },
        )
    result_summary = _briefing_summary(
        changes=changed,
        unchanged=unchanged,
        unconfirmed=unconfirmed,
        decisions=decisions,
        evidence=evidence,
    )
    fingerprint_payload = {
        "changes": sorted(
            f"{item.get('event_key', '')}:{item.get('current_observation', _canonical(item))}"
            for item in changed
        ),
        "unchanged": sorted(
            f"{item.get('event_key', '')}:{item.get('current_observation', _canonical(item))}"
            for item in unchanged
        ),
        "unconfirmed": sorted(_canonical(item) for item in unconfirmed),
        "decisions": sorted(_canonical(item) for item in decisions),
    }
    fingerprint = hashlib.sha256(_canonical(fingerprint_payload).encode("utf-8")).hexdigest()
    return {
        "summary": result_summary,
        "changes": changed,
        "unchanged": unchanged,
        "unconfirmed": unconfirmed,
        "decisions": decisions,
        "fingerprint": fingerprint,
    }
