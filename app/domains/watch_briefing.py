"""Deterministic normalization and comparison for watch briefings."""

from __future__ import annotations

import hashlib
import json
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
    return " ".join(re.findall(r"[\w\u3400-\u9fff]+", value.replace("_", " ").casefold()))


def _excerpt_supports(claim: str, excerpt: str) -> bool:
    normalized_claim = _normalized_text(claim)
    normalized_excerpt = _normalized_text(excerpt)
    if not normalized_claim or not normalized_excerpt:
        return False
    # Token-overlap heuristics can falsely verify negation, reversed relations,
    # or unrelated claims that share names and dates. This deterministic gate
    # accepts only a normalized contiguous claim excerpt; paraphrases remain
    # unconfirmed until a stronger verifier is available.
    return normalized_claim in normalized_excerpt


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
    for title, items in sections:
        lines.append(f"\n{title}：")
        if not items:
            lines.append("- 无")
            continue
        for item in items[:4]:
            conclusion = str(
                item.get("current_observation")
                or item.get("claim")
                or item.get("title")
                or item.get("note")
                or "待核实信息"
            )
            subject = str(item.get("title") or item.get("claim") or "关注项")
            if subject != conclusion:
                conclusion = f"{subject}：{conclusion}"
            if item.get("importance_filtered"):
                conclusion += "（按重要性规则折叠）"
            if item.get("reason"):
                conclusion += f"；说明：{item['reason']}"
            refs = item.get("evidence_refs", [])
            refs = [refs] if isinstance(refs, str) else refs
            source_by_ref = {
                str(value): str(source.get("ref") or value)
                for source in evidence
                for value in (source.get("ref"), source.get("evidence_id"))
                if value
            }
            source = (
                ", ".join(source_by_ref.get(str(ref), str(ref)) for ref in refs[:2])
                if item.get("evidence_check")
                not in {"citation_mismatch", "unsupported_or_excerpt_missing"}
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


def normalize_briefing(
    *,
    summary: str,
    evidence: list[dict[str, Any]],
    previous: dict[str, Any] | None,
    importance_rules: dict[str, Any],
    retrieval_failures: list[str] | tuple[str, ...] = (),
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
    old_items = [*(previous or {}).get("changes", []), *(previous or {}).get("unchanged", [])]
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
                key_material = (
                    str(item.get("event_id") or item.get("event_key") or text).casefold().strip()
                )
                key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
                item["event_key"] = key
                if section in {"changes", "unchanged"}:
                    item["current_observation"] = (
                        item.get("current_observation")
                        or item.get("claim")
                        or item.get("title")
                        or item.get("status")
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
                claim_to_check = str(
                    item.get("claim")
                    or item.get("observation")
                    or item.get("current_observation")
                    or item.get("title")
                    or ""
                )
                refs = item.get("evidence_refs", [])
                refs = [refs] if isinstance(refs, str) else refs
                cited_evidence = [
                    record
                    for record in evidence
                    if any(ref in {record.get("ref"), record.get("evidence_id")} for ref in refs)
                ]
                excerpts = [str(record.get("excerpt") or "") for record in cited_evidence]
                if not any(
                    _excerpt_supports(claim_to_check, excerpt) for excerpt in excerpts if excerpt
                ):
                    item["evidence_check"] = "unsupported_or_excerpt_missing"
                    item["reason"] = (
                        "Citation identifies a read source, but the claim could not be matched to its available excerpt."
                    )
                    unconfirmed.append(item)
                    continue
                item_target.append(item)

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
