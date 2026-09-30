from dataclasses import FrozenInstanceError

import pytest

from app.domains.knowledge_query import (
    ValidatedRewritePlan,
    ValidatedRewriteQuery,
    normalize_query,
    validate_rewrite_request,
)


def test_normalize_query_only_collapses_whitespace() -> None:
    assert normalize_query("  What\n  did\tthe report say?  ") == "What did the report say?"


def test_validates_and_normalizes_rewrite_plan() -> None:
    plan = validate_rewrite_request(
        "  Original\n query ",
        {
            "evidence_gap": " missing   dates ",
            "expected_gain": "Find dated evidence",
            "queries": [
                {"query": "  report\t2024 ", "purpose": "Find dates"},
                {"query": "report author", "purpose": "Find attribution"},
            ],
        },
    )

    assert plan == ValidatedRewritePlan(
        original_query="  Original\n query ",
        normalized_query="Original query",
        evidence_gap="missing dates",
        expected_gain="Find dated evidence",
        queries=(
            ValidatedRewriteQuery(query="report 2024", purpose="Find dates"),
            ValidatedRewriteQuery(query="report author", purpose="Find attribution"),
        ),
    )
    with pytest.raises(FrozenInstanceError):
        plan.evidence_gap = "changed"  # type: ignore[misc]


def test_accepts_three_rewrites_with_default_budget() -> None:
    plan = validate_rewrite_request(
        "original",
        {
            "evidence_gap": "gap",
            "expected_gain": "gain",
            "queries": [
                {"query": "first", "purpose": "one"},
                {"query": "second", "purpose": "two"},
                {"query": "third", "purpose": "three"},
            ],
        },
    )

    assert len(plan.queries) == 3


@pytest.mark.parametrize(
    ("original", "payload"),
    [
        ("q", None),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [], "extra": 1}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": []}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [
            {"query": "a", "purpose": "b", "extra": True}
        ]}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": "a"}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [
            {"query": 4, "purpose": "b"}
        ]}),
        ("q", {"evidence_gap": " ", "expected_gain": "y", "queries": [
            {"query": "a", "purpose": "b"}
        ]}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [
            {"query": "a", "purpose": " "}
        ]}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [
            {"query": "Q", "purpose": "b"}
        ]}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [
            {"query": "a", "purpose": "b"}, {"query": " A ", "purpose": "c"}
        ]}),
        ("q", {"evidence_gap": "x" * 301, "expected_gain": "y", "queries": [
            {"query": "a", "purpose": "b"}
        ]}),
        ("q", {"evidence_gap": "x", "expected_gain": "y" * 301, "queries": [
            {"query": "a", "purpose": "b"}
        ]}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [
            {"query": "a", "purpose": "p" * 301}
        ]}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [
            {"query": "a" * 301, "purpose": "b"}
        ]}),
        ("q", {"evidence_gap": "x", "expected_gain": "y", "queries": [
            {"query": "a" * 300, "purpose": "b"}, {"query": "c" * 301, "purpose": "d"}
        ]}),
    ],
)
def test_rejects_invalid_rewrite_requests(original: str, payload: object) -> None:
    with pytest.raises(ValueError):
        validate_rewrite_request(original, payload)


def test_rejects_rewrite_count_over_configured_budget() -> None:
    payload = {
        "evidence_gap": "gap",
        "expected_gain": "gain",
        "queries": [
            {"query": f"query {index}", "purpose": "purpose"}
            for index in range(4)
        ],
    }

    with pytest.raises(ValueError, match="between 1 and 3 rewrites"):
        validate_rewrite_request("original", payload, max_rewrites=3)


def test_rejects_total_query_length_over_configured_budget() -> None:
    payload = {
        "evidence_gap": "gap",
        "expected_gain": "gain",
        "queries": [
            {"query": "a" * 151, "purpose": "one"},
            {"query": "b" * 150, "purpose": "two"},
        ],
    }

    with pytest.raises(ValueError, match="at most 300 characters"):
        validate_rewrite_request("original", payload, max_total_chars=300)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_rewrites": 0}, "max_rewrites"),
        ({"max_rewrites": 17}, "max_rewrites"),
        ({"max_rewrites": True}, "max_rewrites"),
        ({"max_total_chars": 0}, "max_total_chars"),
        ({"max_total_chars": 4801}, "max_total_chars"),
        ({"max_total_chars": True}, "max_total_chars"),
    ],
)
def test_rejects_unsafe_or_invalid_budgets(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_rewrite_request(
            "original",
            {
                "evidence_gap": "gap",
                "expected_gain": "gain",
                "queries": [{"query": "rewrite", "purpose": "purpose"}],
            },
            **kwargs,  # type: ignore[arg-type]
        )


def test_rejects_non_string_original_query() -> None:
    with pytest.raises(ValueError):
        validate_rewrite_request(None, {  # type: ignore[arg-type]
            "evidence_gap": "gap",
            "expected_gain": "gain",
            "queries": [{"query": "rewrite", "purpose": "purpose"}],
        })
