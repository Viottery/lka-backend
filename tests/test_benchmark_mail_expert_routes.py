from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/benchmark_mail_expert_routes.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_mail_expert_routes", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_BENCHMARK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BENCHMARK)
LABELS = _BENCHMARK.LABELS
load_cases = _BENCHMARK.load_cases
run_benchmark = _BENCHMARK.run_benchmark


def test_fixture_is_synthetic_and_safe_actions_are_unsupported() -> None:
    cases = load_cases()
    assert {case["label"] for case in cases} == set(LABELS)
    assert all(case["label"] == "unsupported" for case in cases if case.get("safe_write_or_send"))
    assert all("@example.test" not in case["request"] or "synthetic" in case["request"] for case in cases)


def test_injected_predictors_compare_same_cases_and_report_metrics() -> None:
    cases = load_cases()

    def laya(case):
        return {"answers": {"route": {"choice": case["label"]}}}

    async def llm(case):
        return SimpleNamespace(content=json.dumps({"label": case["label"]}), usage={"total_tokens": 7},
                               status="completed", partial=False)

    report = asyncio.run(run_benchmark(cases=cases, laya_predict=laya, llm_predict=llm,
                                       laya_load_ms=12, laya_resource={"peak": 1}))
    assert report["summary"]["laya"]["accuracy"] == 1
    assert report["summary"]["llm_json"]["accuracy"] == 1
    assert report["summary"]["llm_json"]["token_usage"]["total_tokens"] == 7 * len(cases)
    assert [r["case_id"] for r in report["cases"]["laya"]] == [r["case_id"] for r in report["cases"]["llm_json"]]
    assert report["laya_model_load_ms"] == 12
