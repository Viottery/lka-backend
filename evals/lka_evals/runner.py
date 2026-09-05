"""Command-line benchmark runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from evals.lka_evals.case_loader import load_suite
from evals.lka_evals.metrics import evaluate_case, summarize_metrics
from evals.lka_evals.report import write_reports
from evals.lka_evals.subject import EvalRunArtifact, build_subject
from evals.lka_evals.judge import judge_answer
from app.core.local_config import load_local_config
from app.core.llm import build_text_llm_client


def run_suite(
    suite_path: Path,
    *,
    subject_name: str = "runtime",
    base_url: str = "http://127.0.0.1:8765",
    allow_http_setup: bool = False,
    llm_mode: str = "scripted",
    local_config: Path | None = None,
    timeout: float = 120.0,
    report_dir: Path = Path("evals/reports"),
    judge: bool = False,
) -> dict[str, Any]:
    suite = load_suite(suite_path)
    suite_id = str(suite.get("suite_id") or suite_path.stem)
    subject = build_subject(
        name=subject_name,
        base_url=base_url,
        allow_http_setup=allow_http_setup,
        llm_mode=llm_mode,
        local_config=local_config,
        timeout=timeout,
    )
    case_results: list[dict[str, Any]] = []
    judge_client = None
    if judge:
        config_path = local_config or Path("config/local.toml")
        judge_client = build_text_llm_client(load_local_config(config_path))
        if judge_client is None:
            raise RuntimeError("LLM judge requested but no configured LLM client is available")
    for case in suite.get("cases", []):
        if not isinstance(case, dict):
            continue
        artifact = subject.run_case(suite_id=suite_id, case=case)
        metrics = evaluate_case(case, artifact)
        if judge_client is not None:
            expected = case.get("expect") if isinstance(case.get("expect"), dict) else {}
            gold = str(expected.get("answer") or expected.get("answer_gold") or "")
            if gold:
                judged = asyncio.run(judge_answer(client=judge_client, question=str(case.get("request", {}).get("user_input", "")), answer=str(artifact.result.get("answer") or ""), gold=gold, evidence=[str(x) for x in expected.get("evidence_text", [])]))
                from evals.lka_evals.metrics import MetricResult
                metrics.append(MetricResult("llm_judge_answer_quality", float(judged.get("score", 0.0)), judged.get("status") == "completed" and float(judged.get("score", 0.0)) >= float(expected.get("judge_min_score", 0.7)), judged))
        metric_summary = summarize_metrics(metrics)
        case_results.append(_case_result(case=case, artifact=artifact, summary=metric_summary))

    report_paths = write_reports(
        report_dir=report_dir,
        suite_id=suite_id,
        subject=subject_name,
        case_results=case_results,
    )
    passed = all(case["passed"] for case in case_results)
    score = (
        sum(float(case["score"]) for case in case_results) / len(case_results)
        if case_results
        else 0.0
    )
    return {
        "suite_id": suite_id,
        "subject": subject_name,
        "llm_mode": llm_mode if subject_name == "runtime" else None,
        "score": round(score, 4),
        "passed": passed,
        "case_count": len(case_results),
        "report_paths": report_paths,
        "cases": case_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.lka_evals.runner",
        description="Run Local Knowledge Agent OS benchmark suites.",
    )
    parser.add_argument("suite", type=Path)
    parser.add_argument("--subject", choices=["runtime", "http", "stream"], default="runtime")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--allow-http-setup", action="store_true")
    parser.add_argument(
        "--llm-mode",
        choices=["scripted", "real"],
        default="scripted",
        help="For --subject runtime: use deterministic scripted LLM or configured real LLM.",
    )
    parser.add_argument(
        "--local-config",
        type=Path,
        help="For --llm-mode real: TOML provider config to use inside isolated runtime.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--report-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--judge", action="store_true", help="Run optional LLM-as-judge answer scoring.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    args = parser.parse_args(argv)

    result = run_suite(
        args.suite,
        subject_name=args.subject,
        base_url=args.base_url,
        allow_http_setup=args.allow_http_setup,
        llm_mode=args.llm_mode,
        local_config=args.local_config,
        timeout=args.timeout,
        report_dir=args.report_dir,
        judge=args.judge,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"suite={result['suite_id']} subject={result['subject']} "
            f"score={result['score']} passed={result['passed']} "
            f"cases={result['case_count']}"
        )
        print(f"json_report={result['report_paths']['json']}")
        print(f"markdown_report={result['report_paths']['markdown']}")
    return 0 if result["passed"] else 1


def _case_result(
    *,
    case: dict[str, Any],
    artifact: EvalRunArtifact,
    summary: dict[str, Any],
) -> dict[str, Any]:
    result = dict(artifact.result)
    if "answer" in result and isinstance(result["answer"], str):
        result["answer_preview"] = result["answer"][:500]
    return {
        "case_id": artifact.case_id,
        "name": case.get("name") or artifact.case_id,
        "subject": artifact.subject,
        "score": summary["score"],
        "passed": summary["passed"],
        "metrics": summary["metrics"],
        "request": artifact.request,
        "timings": artifact.timings,
        "error": artifact.error,
        "fixture_index": artifact.fixture_index,
        "log": {
            "path": artifact.log.get("path") if artifact.log else None,
            "exists": artifact.log.get("exists") if artifact.log else False,
            "missing_sections": artifact.log.get("missing_sections") if artifact.log else [],
            "json_parse_errors": artifact.log.get("json_parse_errors") if artifact.log else [],
        },
        "sse_event_count": len(artifact.sse_frames),
        "result": result,
    }


if __name__ == "__main__":
    sys.exit(main())
