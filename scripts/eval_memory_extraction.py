"""Offline, reproducible conservative-memory extraction evaluation."""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.core.memory_extraction import extract_user_memories


def evaluate(path: Path) -> dict[str, object]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    true_positive = false_positive = false_negative = 0
    latencies: list[float] = []
    failures: list[str] = []
    for case in cases:
        started = time.perf_counter()
        result = extract_user_memories(source_id=case["id"], content=case["text"])
        latencies.append((time.perf_counter() - started) * 1000)
        actual = result[0].claim if result else None
        expected = case["expected_claim"]
        if actual == expected and expected is not None:
            true_positive += 1
        elif actual is not None:
            false_positive += 1
            failures.append(case["id"])
        elif expected is not None:
            false_negative += 1
            failures.append(case["id"])
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered)-1, int((len(ordered)-1)*0.95))] if ordered else 0.0
    return {
        "cases": len(cases), "precision": precision, "recall": recall,
        "false_promotions": false_positive, "failures": failures,
        "p95_local_ms": round(p95, 3), "llm_calls": 0, "llm_tokens": 0,
    }


if __name__ == "__main__":
    fixture = Path(__file__).resolve().parents[1] / "evals/fixtures/memory_extraction_cases.jsonl"
    print(json.dumps(evaluate(fixture), ensure_ascii=False, indent=2))
