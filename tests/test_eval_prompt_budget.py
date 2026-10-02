from app.core.prompt_tokens import PromptTokenCounter
from scripts.eval_prompt_budget import fixture_cases, local_records


def test_synthetic_prompt_budget_fixtures_are_deterministic_and_private():
    first = fixture_cases()
    second = fixture_cases()
    assert [case.user for case in first] == [case.user for case in second]
    assert {case.name for case in first} == {"chinese", "json", "unicode", "tool_schemas"}
    rows = local_records(PromptTokenCounter())
    assert len(rows) == 4
    assert all(row["input_estimate"] > 0 and row["conservative"] for row in rows)
