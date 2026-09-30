#!/usr/bin/env python3
"""Compare Laya and hosted Jev on identical typed-decision JSONL cases.

Each JSONL row contains case_id, state, questions, and optional expected values:

    {"case_id":"mail_route","state":{"request":"..."},"questions":{"package":{"type":"choice","instructions":"...","criteria":{"mail":"..."}}},"expected":{"package":"mail"}}

The report omits state and question text. Jev is remote: selecting it requires both
--allow-remote and TYPESAFE_API_KEY. Remote warmups are never sent; every measured
repetition is a billable API request.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(case, dict):
            raise ValueError(f"{path}:{line_number}: each row must be a JSON object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{path}:{line_number}: case_id must be a non-empty string")
        if case_id in seen_ids:
            raise ValueError(f"{path}:{line_number}: duplicate case_id {case_id!r}")
        if "state" not in case or not isinstance(case.get("questions"), dict):
            raise ValueError(f"{path}:{line_number}: state and questions are required")
        if not case["questions"]:
            raise ValueError(f"{path}:{line_number}: questions must not be empty")
        expected = case.get("expected", {})
        if not isinstance(expected, dict):
            raise ValueError(f"{path}:{line_number}: expected must be an object")
        seen_ids.add(case_id)
        cases.append(case)
    if not cases:
        raise ValueError(f"{path}: no benchmark cases found")
    return cases


def _answer_values(answers: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, answer in answers.items():
        if not isinstance(answer, dict):
            continue
        for field in ("choice", "noul", "score"):
            if field in answer:
                values[name] = answer[field]
                break
    return values


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _expected_matches(expected: dict[str, Any], values: dict[str, Any]) -> bool | None:
    if not expected:
        return None
    return all(values.get(name) == value for name, value in expected.items())


def _load_laya(model_id: str, device: str, threads: int) -> tuple[Any, float]:
    try:
        import torch
        import laya
    except ImportError as exc:
        raise RuntimeError(
            "Laya is not installed in this Python environment. Install a CPU PyTorch "
            "build and laya in an isolated environment first."
        ) from exc
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    started = time.perf_counter()
    agent = laya.load(model_id, device=device)
    return agent, (time.perf_counter() - started) * 1000


def _run_laya(
    cases: list[dict[str, Any]],
    *,
    model_id: str,
    device: str,
    threads: int,
    warmups: int,
    repeats: int,
) -> dict[str, Any]:
    agent, load_ms = _load_laya(model_id, device, threads)
    per_case: list[dict[str, Any]] = []
    latencies: list[float] = []
    expected_matches: list[bool] = []
    for case in cases:
        for _ in range(warmups):
            agent.predict(case["state"], case["questions"])
        case_latencies: list[float] = []
        last_values: dict[str, Any] = {}
        last_answers: dict[str, Any] = {}
        for _ in range(repeats):
            started = time.perf_counter()
            result = agent.predict(case["state"], case["questions"])
            elapsed_ms = (time.perf_counter() - started) * 1000
            answers = result.get("answers")
            if not isinstance(answers, dict):
                raise RuntimeError(f"Laya returned no answers for case {case['case_id']!r}")
            last_answers = answers
            last_values = _answer_values(answers)
            case_latencies.append(elapsed_ms)
            latencies.append(elapsed_ms)
        match = _expected_matches(case.get("expected", {}), last_values)
        if match is not None:
            expected_matches.append(match)
        per_case.append(
            {
                "case_id": case["case_id"],
                "samples": len(case_latencies),
                "p50_ms": round(statistics.median(case_latencies), 1),
                "p95_ms": round(_percentile(case_latencies, 0.95), 1),
                "answers": last_values,
                "answer_details": last_answers,
                "expected_match": match,
            }
        )
    return {
        "backend": "laya",
        "model": model_id,
        "device": device,
        "threads": threads,
        "model_load_ms": round(load_ms, 1),
        "samples": len(latencies),
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(_percentile(latencies, 0.95), 1),
        "expected_match_rate": (
            round(sum(expected_matches) / len(expected_matches), 4)
            if expected_matches
            else None
        ),
        "cases": per_case,
    }


def _run_jev(
    cases: list[dict[str, Any]],
    *,
    model: str,
    timeout_seconds: float,
    repeats: int,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError(
            "Jev benchmarking requires httpx; install the project dev dependencies "
            "or add httpx to this isolated environment."
        ) from exc
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("Jev requires TYPESAFE_API_KEY; the key value is never printed.")

    endpoint = "https://api.typesafe.ai/v1/systemone"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = httpx.Timeout(timeout_seconds)
    per_case: list[dict[str, Any]] = []
    latencies: list[float] = []
    expected_matches: list[bool] = []
    with httpx.Client(timeout=timeout, trust_env=True) as client:
        for case in cases:
            case_latencies: list[float] = []
            last_values: dict[str, Any] = {}
            last_answers: dict[str, Any] = {}
            payload = {
                "state": case["state"],
                "model": model,
                "questions": case["questions"],
            }
            for _ in range(repeats):
                started = time.perf_counter()
                response = client.post(endpoint, headers=headers, json=payload)
                elapsed_ms = (time.perf_counter() - started) * 1000
                if response.is_error:
                    request_id = response.headers.get("x-request-id")
                    detail = f"Jev returned HTTP {response.status_code}"
                    if request_id:
                        detail += f" (request_id={request_id})"
                    raise RuntimeError(detail)
                try:
                    response_payload = response.json()
                except ValueError as exc:
                    raise RuntimeError("Jev returned invalid JSON") from exc
                answers = response_payload.get("answers")
                if not isinstance(answers, dict):
                    raise RuntimeError("Jev response contains no answers object")
                last_answers = answers
                last_values = _answer_values(answers)
                case_latencies.append(elapsed_ms)
                latencies.append(elapsed_ms)
            match = _expected_matches(case.get("expected", {}), last_values)
            if match is not None:
                expected_matches.append(match)
            per_case.append(
                {
                    "case_id": case["case_id"],
                    "samples": len(case_latencies),
                    "p50_ms": round(statistics.median(case_latencies), 1),
                    "p95_ms": round(_percentile(case_latencies, 0.95), 1),
                    "answers": last_values,
                    "answer_details": last_answers,
                    "expected_match": match,
                }
            )
    return {
        "backend": "jev",
        "model": model,
        "endpoint": endpoint,
        "samples": len(latencies),
        "remote_requests": len(latencies),
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(_percentile(latencies, 0.95), 1),
        "expected_match_rate": (
            round(sum(expected_matches) / len(expected_matches), 4)
            if expected_matches
            else None
        ),
        "cases": per_case,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, help="JSONL cases shared by both backends")
    parser.add_argument("--backend", choices=("laya", "jev", "both"), default="laya")
    parser.add_argument("--laya-model", default="convaiinnovations/laya-multilingual")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--jev-model", default="jev-1.13.0")
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=3, help="Laya warmups per case")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or args.warmups < 0 or args.threads < 1 or args.timeout <= 0:
        parser.error("repeats and threads must be positive; warmups and timeout cannot be negative")
    if args.backend in {"jev", "both"} and not args.allow_remote:
        parser.error(
            "Jev sends each case state to TypeSafe. Pass --allow-remote only for data "
            "approved for external processing."
        )
    return args


def main() -> int:
    args = _parse_args()
    cases = _load_cases(args.cases)
    results: list[dict[str, Any]] = []
    if args.backend in {"laya", "both"}:
        results.append(
            _run_laya(
                cases,
                model_id=args.laya_model,
                device=args.device,
                threads=args.threads,
                warmups=args.warmups,
                repeats=args.repeats,
            )
        )
    if args.backend in {"jev", "both"}:
        remote_requests = len(cases) * args.repeats
        print(
            f"Sending {remote_requests} benchmark requests to hosted Jev; "
            "case states are sent to TypeSafe.",
            file=sys.stderr,
        )
        results.append(
            _run_jev(
                cases,
                model=args.jev_model,
                timeout_seconds=args.timeout,
                repeats=args.repeats,
            )
        )
    report = {
        "cases_file": str(args.cases),
        "case_count": len(cases),
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
