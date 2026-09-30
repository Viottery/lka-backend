"""Deterministic validation for bounded knowledge query rewrite plans."""

from __future__ import annotations

from dataclasses import dataclass

_MAX_TEXT_LENGTH = 300
_MAX_REWRITES = 16
_MAX_TOTAL_QUERY_LENGTH = 4800


@dataclass(frozen=True)
class ValidatedRewriteQuery:
    query: str
    purpose: str


@dataclass(frozen=True)
class ValidatedRewritePlan:
    original_query: str
    normalized_query: str
    evidence_gap: str
    expected_gain: str
    queries: tuple[ValidatedRewriteQuery, ...]


def normalize_query(query: str) -> str:
    """Trim and collapse whitespace without changing query meaning."""
    if not isinstance(query, str):
        raise ValueError("query must be a string")  # noqa: TRY004 - one validation error type
    return " ".join(query.split())


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")  # noqa: TRY004 - one validation error type
    normalized = normalize_query(value)
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{field} must be at most {_MAX_TEXT_LENGTH} characters")
    return normalized


def _duplicate_key(query: str) -> str:
    return normalize_query(query).casefold()


def validate_rewrite_request(
    original_query: str,
    payload: object,
    *,
    max_rewrites: int = 8,
    max_total_chars: int = 2400,
) -> ValidatedRewritePlan:
    """Validate an untrusted rewrite payload and return its immutable plan."""
    if (
        isinstance(max_rewrites, bool)
        or not isinstance(max_rewrites, int)
        or not 1 <= max_rewrites <= _MAX_REWRITES
    ):
        raise ValueError(f"max_rewrites must be an integer between 1 and {_MAX_REWRITES}")
    if (
        isinstance(max_total_chars, bool)
        or not isinstance(max_total_chars, int)
        or not 1 <= max_total_chars <= _MAX_TOTAL_QUERY_LENGTH
    ):
        raise ValueError(
            f"max_total_chars must be an integer between 1 and {_MAX_TOTAL_QUERY_LENGTH}"
        )
    normalized_original = _required_text(original_query, "original_query")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")  # noqa: TRY004 - one validation error type
    if set(payload) != {"evidence_gap", "expected_gain", "queries"}:
        raise ValueError("payload must contain exactly evidence_gap, expected_gain, and queries")

    evidence_gap = _required_text(payload["evidence_gap"], "evidence_gap")
    expected_gain = _required_text(payload["expected_gain"], "expected_gain")
    raw_queries = payload["queries"]
    if not isinstance(raw_queries, list):
        raise ValueError("queries must be a list")  # noqa: TRY004 - one validation error type
    if not 1 <= len(raw_queries) <= max_rewrites:
        raise ValueError(f"queries must contain between 1 and {max_rewrites} rewrites")

    seen = {_duplicate_key(normalized_original)}
    validated_queries: list[ValidatedRewriteQuery] = []
    total_query_length = 0
    for index, item in enumerate(raw_queries):
        if not isinstance(item, dict) or set(item) != {"query", "purpose"}:
            raise ValueError(f"queries[{index}] must contain exactly query and purpose")
        query = _required_text(item["query"], f"queries[{index}].query")
        purpose = _required_text(item["purpose"], f"queries[{index}].purpose")
        key = _duplicate_key(query)
        if key in seen:
            raise ValueError(f"queries[{index}].query duplicates another query")
        seen.add(key)
        total_query_length += len(query)
        if total_query_length > max_total_chars:
            raise ValueError(
                f"combined rewrite queries must be at most {max_total_chars} characters"
            )
        validated_queries.append(ValidatedRewriteQuery(query=query, purpose=purpose))

    return ValidatedRewritePlan(
        original_query=original_query,
        normalized_query=normalized_original,
        evidence_gap=evidence_gap,
        expected_gain=expected_gain,
        queries=tuple(validated_queries),
    )
