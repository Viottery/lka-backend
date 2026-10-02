#!/usr/bin/env python3
"""Offline mail intent experiments; no production routing or tool execution."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

from benchmark_mail_expert_routes import FIXTURE, LABELS, MODEL, load_cases

CHALLENGE = FIXTURE.with_name("mail_expert_routes_challenge.jsonl")
AXIS_STRESS = FIXTURE.with_name("mail_expert_axis_stress.jsonl")


def axis_pair_question() -> dict[str, Any]:
    return {"axis": {"type": "choice", "instructions": "The task is organizing emails. Choose the requested grouping or matching axis. 已知要整理邮件，判断整理维度。", "criteria": {
        "group_sender": "Group by sender address/person; count or summarize per sender. 按发件人或地址分组。",
        "align_matter": "Match to already existing matters or tracked tasks. 对齐已有事项或待办。",
    }}}


def bilingual_questions() -> dict[str, Any]:
    return {"route": {"type": "choice", "instructions": "Choose the single primary email task. 只选主要邮件任务；发送或修改不属于只读任务。", "criteria": {
        "search": "Find a particular message or fact using a query. 查找特定邮件或信息，不逐封分析。",
        "list": "Enumerate message metadata: date, sender, subject, count. 只列清单、数量或元数据，不分析正文。",
        "review": "Read message contents to summarize, prioritize, or identify actions. 阅读正文，逐封总结、评估优先级或待办。",
        "group_sender": "Organize messages by sender address and count per sender. 按发件人或发件地址分组统计，不是按时间清单。",
        "align_matter": "Match messages to already existing matters or tracked tasks. 将邮件与已有事项或待办对应，不创建新事项。",
        "unsupported": "Send, reply, archive, delete, mark read, change data, or unclear task. 发送回复、归档删除、标记已读、写入或目标不明确。",
    }}}


def facet_questions() -> dict[str, Any]:
    return {
        "safety": {"type": "choice", "instructions": "Does the user ask to change external or stored data? 用户是否要求写入/发送？", "criteria": {
            "read": "Only inspect, find, list, summarize, or compare; negated writes do not count. 仅查看或分析；明确说不要写入也属于只读。",
            "write": "Send/reply, archive/delete, mark read, create/update/link, or other change, even alongside reading. 任何发送、删除、标记或修改，包括先读后写。",
            "unclear": "Cannot determine the requested operation. 无法判断。",
        }},
        "goal": {"type": "choice", "instructions": "What is the main read-only mail goal? 主要只读目标是什么？", "criteria": {
            "find": "Locate a specific mail or fact. 定位特定邮件或信息。",
            "enumerate": "List message metadata in time order or count. 按时间列元数据或总数。",
            "analyze": "Read bodies and assess summaries, urgency, or actions. 阅读正文并分析摘要、紧急程度或待办。",
            "organize": "Group or match multiple messages by an organizing dimension. 按维度整理多封邮件。",
            "unclear": "No clear read-only task. 没有明确的只读目标。",
        }},
        "axis": {"type": "choice", "instructions": "If organizing, which dimension? 若需整理，按什么维度？", "criteria": {
            "sender": "Sender email address/person, with per-sender groups. 发件人或发件地址分组。",
            "matter": "Already existing matter/task, matching mail to tracked work. 对齐已有事项或待办。",
            "none": "Neither sender nor existing matter. 不是这两种整理维度。",
        }},
    }


def _choice(answer: dict[str, Any], key: str) -> str:
    return str(answer["answers"][key]["choice"])


def _margin(answer: dict[str, Any], key: str) -> float:
    values = sorted((float(v) for v in answer["answers"][key]["probabilities"].values()), reverse=True)
    return values[0] - values[1] if len(values) > 1 else 0.0


def classify_facets(answer: dict[str, Any]) -> tuple[str | None, float]:
    safety, goal, axis = (_choice(answer, key) for key in ("safety", "goal", "axis"))
    if safety == "write" or goal == "unclear":
        label = "unsupported"
    elif safety != "read":
        label = None
    elif goal == "organize":
        label = {"sender": "group_sender", "matter": "align_matter"}.get(axis)
    else:
        label = {"find": "search", "enumerate": "list", "analyze": "review"}.get(goal)
    relevant = ["safety", "goal"] + (["axis"] if goal == "organize" else [])
    return label, min(_margin(answer, key) for key in relevant)


def _percentile(values: list[float], fraction: float) -> float:
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 2)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [row["latency_ms"] for row in rows]
    return {"correct": sum(row["correct"] for row in rows), "cases": len(rows),
            "accuracy": round(sum(row["correct"] for row in rows) / len(rows), 4),
            "p50_ms": _percentile(latencies, .5), "p95_ms": _percentile(latencies, .95),
            "by_label": {label: {"correct": sum(row["correct"] for row in rows if row["expected"] == label),
                                 "cases": sum(row["expected"] == label for row in rows)} for label in LABELS}}


def run_laya(agent: Any, cases: list[dict[str, Any]], *, variant: str) -> list[dict[str, Any]]:
    questions = {"bilingual": bilingual_questions, "facets": facet_questions,
                 "axis_pair": axis_pair_question}[variant]()
    rows = []
    for case in cases:
        started = time.perf_counter()
        answer = agent.predict({"user_request": case["request"]}, questions)
        latency = (time.perf_counter() - started) * 1000
        if variant == "bilingual":
            choice, margin = _choice(answer, "route"), _margin(answer, "route")
        elif variant == "axis_pair":
            choice, margin = _choice(answer, "axis"), _margin(answer, "axis")
        else:
            choice, margin = classify_facets(answer)
        rows.append({"case_id": case["id"], "expected": case["label"], "choice": choice,
                     "correct": choice == case["label"], "margin": round(margin, 4),
                     "latency_ms": round(latency, 2),
                     "answers": {key: value["choice"] for key, value in answer["answers"].items()}})
    return rows


def selective_replay(rows: list[dict[str, Any]], llm_rows: list[dict[str, Any]], *,
                     direct_labels: set[str], min_margin: float = .9) -> dict[str, Any]:
    by_id = {row["case_id"]: row for row in llm_rows}
    if len(by_id) != len(rows) or any(
        row["case_id"] not in by_id or row["expected"] != by_id[row["case_id"]]["expected"] for row in rows
    ):
        raise ValueError("LLM replay must cover exactly the same cases")
    replay = []
    for row in rows:
        direct = row["choice"] in direct_labels and row["margin"] >= min_margin
        fallback = by_id[row["case_id"]]
        choice = row["choice"] if direct else fallback["choice"]
        replay.append({"case_id": row["case_id"], "expected": row["expected"], "choice": choice,
                       "correct": choice == row["expected"], "direct": direct,
                       "latency_ms": row["latency_ms"] + (0 if direct else fallback["latency_ms"]),
                       "tokens": 0 if direct else (fallback.get("token_usage") or {}).get("total_tokens")})
    summary = _summary(replay)
    summary.update({"direct": sum(row["direct"] for row in replay),
                    "fallback": sum(not row["direct"] for row in replay),
                    "known_llm_tokens": sum(row["tokens"] or 0 for row in replay),
                    "missing_llm_token_records": sum(row["tokens"] is None for row in replay),
                    "latency_note": "Offline sum of separate LayA and prior LLM timings, not measured end-to-end."})
    return {"summary": summary, "cases": replay}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-llm-report", type=Path, required=True)
    parser.add_argument("--challenge-llm-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    import laya
    import torch

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    started = time.perf_counter()
    agent = laya.load(MODEL, device="cpu")
    load_ms = round((time.perf_counter() - started) * 1000, 2)
    cases = {"dev": load_cases(FIXTURE), "challenge": load_cases(CHALLENGE)}
    llm_reports = {"dev": json.loads(args.dev_llm_report.read_text(encoding="utf-8")),
                   "challenge": json.loads(args.challenge_llm_report.read_text(encoding="utf-8"))}
    report: dict[str, Any] = {"model": MODEL, "model_load_ms": load_ms,
                              "selective_margin": .9, "direct_labels": ["search", "list", "review", "group_sender", "align_matter"],
                              "datasets": {}}
    for split, split_cases in cases.items():
        variants = {}
        for variant in ("bilingual", "facets"):
            rows = run_laya(agent, split_cases, variant=variant)
            variants[variant] = {"summary": _summary(rows), "cases": rows,
                                 "selective": selective_replay(
                                     rows, llm_reports[split]["cases"]["llm_json"],
                                     direct_labels=set(report["direct_labels"]), min_margin=.9,
                                 )}
        axis_cases = [case for case in split_cases if case["label"] in {"group_sender", "align_matter"}]
        axis_rows = run_laya(agent, axis_cases, variant="axis_pair")
        variants["axis_pair"] = {"summary": _summary(axis_rows), "cases": axis_rows,
                                 "condition": "Oracle knows the task is organizing; not an end-to-end router."}
        report["datasets"][split] = variants
    stress_rows = run_laya(agent, load_cases(AXIS_STRESS), variant="axis_pair")
    report["axis_stress"] = {"summary": _summary(stress_rows), "cases": stress_rows,
                             "condition": "Oracle knows the task is organizing; samples authored after observing first pairwise results."}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"model_load_ms": load_ms, "summary": {
        split: {variant: {"raw": data["summary"],
                          **({"selective": data["selective"]["summary"]} if "selective" in data else {})}
                for variant, data in variants.items()}
        for split, variants in report["datasets"].items()
    }, "axis_stress": report["axis_stress"]["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
