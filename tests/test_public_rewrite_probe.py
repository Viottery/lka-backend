import pytest

from evals.lka_evals.public_rewrite_probe import MAX_REWRITES, clause_rewrites


def test_clause_probe_produces_two_question_only_rewrites():
    question = "Does source A describe an eclipse, while source B reports a comet?"
    assert clause_rewrites(question) == [
        "Does source A describe an eclipse",
        "source B reports a comet",
    ]


def test_clause_probe_produces_three_or_more_rewrites():
    question = (
        "Does source A describe an eclipse, while source B reports a comet, "
        "and source C records a meteor shower?"
    )
    assert clause_rewrites(question) == [
        "Does source A describe an eclipse",
        "source B reports a comet",
        "source C records a meteor shower",
    ]


def test_clause_probe_caps_rewrites_at_eight():
    clauses = [f"S{index} reports a comet today" for index in range(1, 10)]
    question = ", and ".join(clauses) + "?"
    assert clause_rewrites(question) == clauses[:8]


def test_clause_probe_accepts_a_configured_rewrite_cap():
    question = (
        "Does source A describe an eclipse, while source B reports a comet, "
        "and source C records a meteor shower?"
    )
    assert len(clause_rewrites(question, max_rewrites=2)) == 2
    with pytest.raises(ValueError, match="between 1 and 8"):
        clause_rewrites(question, max_rewrites=MAX_REWRITES + 1)


def test_clause_probe_skips_unstructured_or_oversized_question():
    assert clause_rewrites("Where is the original report?") == []
    assert clause_rewrites("A" * 301 + " while source B reports a comet?") == []
