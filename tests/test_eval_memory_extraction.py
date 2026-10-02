from pathlib import Path

from scripts.eval_memory_extraction import evaluate


def test_offline_memory_extraction_fixture_has_no_false_promotions():
    path = Path(__file__).resolve().parents[1] / "evals/fixtures/memory_extraction_cases.jsonl"
    report = evaluate(path)
    assert report["cases"] == 11
    assert report["false_promotions"] == 0
    assert report["recall"] == 1.0
