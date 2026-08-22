"""Benchmark report generation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_reports(
    *,
    report_dir: Path,
    suite_id: str,
    subject: str,
    case_results: list[dict[str, Any]],
) -> dict[str, str]:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{suite_id}_{subject}_{timestamp}"
    json_path = report_dir / f"{base_name}.json"
    md_path = report_dir / f"{base_name}.md"
    summary = _suite_summary(case_results)
    payload = {
        "suite_id": suite_id,
        "subject": subject,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "cases": case_results,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md_path.write_text(_markdown_report(payload), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def _suite_summary(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(case_results)
    passed = sum(1 for case in case_results if case.get("passed"))
    score = (
        sum(float(case.get("score") or 0.0) for case in case_results) / count
        if count
        else 0.0
    )
    return {
        "case_count": count,
        "passed_count": passed,
        "failed_count": count - passed,
        "score": round(score, 4),
        "passed": passed == count,
    }


def _markdown_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        f"# Eval Report: {payload['suite_id']}",
        "",
        f"- subject: `{payload['subject']}`",
        f"- generated_at: `{payload['generated_at']}`",
        f"- score: `{summary['score']}`",
        f"- passed: `{summary['passed_count']}/{summary['case_count']}`",
        "",
        "## Cases",
        "",
    ]
    for case in payload["cases"]:
        lines.extend(
            [
                f"### {case['case_id']}",
                "",
                f"- score: `{case['score']}`",
                f"- passed: `{case['passed']}`",
                f"- wall_time_ms: `{case.get('timings', {}).get('wall_time_ms')}`",
                f"- selected_package: `{case.get('result', {}).get('selected_package')}`",
                f"- answer_preview: {str(case.get('result', {}).get('answer') or '')[:220]}",
                "",
                "| metric | score | pass | details |",
                "| --- | ---: | --- | --- |",
            ]
        )
        for metric in case.get("metrics", []):
            details = json.dumps(metric.get("details", {}), ensure_ascii=False, sort_keys=True)
            lines.append(
                f"| `{metric['name']}` | `{metric['score']}` | `{metric['passed']}` | `{details[:300]}` |"
            )
        lines.append("")
    return "\n".join(lines)
