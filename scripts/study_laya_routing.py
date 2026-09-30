#!/usr/bin/env python3
"""Offline Laya study for context answers, tool hints, and knowledge routing.

Run in an isolated Python environment with laya and CPU PyTorch installed.
Only synthetic fixture data is sent to the model; no Agent code is imported,
no tool is called, and no production configuration is changed.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


FAST_BASE = {
    "answer": "The user can be answered directly using only the given request and session context.",
    "retrieve": "The user needs new evidence, a tool, clarification, or Agent planning.",
}
FAST_ADAPTED = {
    "answer": (
        "当前请求和同一权限范围内仍有效的会话内容已经包含回答所需事实；"
        "无需读取新数据，也没有冲突。用户提供完整文本的改写、翻译也属于此类。"
    ),
    "retrieve": (
        "缺少事实、上下文过期或跨权限范围、证据冲突、指代不清、需要最新状态，"
        "或任务需要新工具调用、写操作与进一步规划。"
    ),
}
TOOL_BASE = {
    "mail": {
        "mail.search": "Search locally indexed mail evidence.",
        "mail.load_messages": "Load bounded source text of selected known mail messages.",
        "mail.sync": "Synchronize a remote mail provider.",
        "agent_decides": "Leave the next step to the Agent.",
    },
    "knowledge": {
        "knowledge.search": "Search local indexed knowledge chunks.",
        "knowledge.list_sources": "List local knowledge sources.",
        "knowledge.load_chunks": "Load selected known knowledge chunks.",
        "agent_decides": "Leave the next step to the Agent.",
    },
    "matter": {
        "matter.search": "Search independent local matters.",
        "agent_decides": "Leave the next step to the Agent.",
    },
}
TOOL_ADAPTED = {
    "mail": {
        "mail.search": "先从本地邮件索引取证；不知道具体邮件 id，或需要一批邮件的摘要时优先考虑。",
        "mail.load_messages": "已有明确邮件 id，搜索片段不足，确实需要少量邮件原文时才考虑。",
        "mail.sync": "用户明确要求同步，或需要最新远程邮件且本地缓存可能过期时才建议；仍由 Agent 审核。",
        "agent_decides": "发送、删除、写入、跨包协作、冲突调查或其他复杂动作交回 Agent。",
    },
    "knowledge": {
        "knowledge.search": "已有明确问题，先检索已授权的本地知识片段。",
        "knowledge.list_sources": "用户明确要来源清单，或必须先知道可用来源才能决定查询范围。",
        "knowledge.load_chunks": "已有搜索返回的具体 chunk id，且摘要不够时读取少量片段。",
        "agent_decides": "写操作、跨来源综合、冲突证据或其他复杂步骤交回 Agent。",
    },
    "matter": {
        "matter.search": "只读查询已保存的本地事项、待办或提醒。",
        "agent_decides": "创建、更新、批量操作、证据去重或其他复杂步骤交回 Agent。",
    },
}
READ_ONLY_TOOL_HINTS = frozenset({
    "mail.search",
    "mail.load_messages",
    "knowledge.search",
    "knowledge.list_sources",
    "knowledge.load_chunks",
    "matter.search",
})
KNOWLEDGE_BASE = {
    "exact_lookup": "Find an exact keyword, code, field, title, or section.",
    "semantic_retrieval": "Find related ideas with approximate meaning.",
    "source_discovery": "First list available knowledge sources.",
    "multi_step": "Complex multi-source investigation needs Agent planning.",
}
KNOWLEDGE_ADAPTED = {
    "exact_lookup": "有准确术语、编号、字段、文档标题或章节；优先建议一次 keyword 检索。",
    "semantic_retrieval": "口语化、主题性或同义表达，缺少准确词项；建议混合或语义检索，由系统按可用索引选择。",
    "source_discovery": "用户在问可访问哪些来源，或来源范围未知而必须先枚举；建议 list_sources。",
    "multi_step": "需要比较多份资料、消解冲突、推断或多轮取证；交给 Agent 规划。",
}
FAST_FACTORIZED = {
    "answer": "All facts needed to answer are present in the supplied request or context.",
    "retrieve": "A required fact is missing from the supplied text, or investigation is needed.",
}
KNOWLEDGE_FACTORIZED = {
    "source_list": {
        "type": "noul",
        "instructions": (
            "Does the user explicitly ask to list available information sources or "
            "source types, rather than search content?"
        ),
    },
    "complex": {
        "type": "choice",
        "instructions": "Is this simple retrieval or investigation?",
        "criteria": {
            "simple": "One lookup or search may find relevant evidence.",
            "multi_step": (
                "Compare multiple documents, resolve contradictions, or reason "
                "across evidence sources."
            ),
        },
    },
    "mode": {
        "type": "choice",
        "instructions": "Choose a starting retrieval style.",
        "criteria": {
            "exact_lookup": "Find literal codes, identifiers, names, titles, and section numbers.",
            "semantic_retrieval": (
                "Find conceptually related text when exact terms are unknown."
            ),
        },
    },
}
EXACT_MARKER = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b|\b[a-z]+(?:_[a-z]+)+\b|\b\d+(?:\.\d+)+\b")
PACKAGE_FAST_QUESTION = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "evals/fixtures/decision_models/routing_smoke.jsonl"
    ).read_text(encoding="utf-8").splitlines()[0]
)["questions"]["package"]
PACKAGE_ADAPTED = {
    "mail": "邮箱消息、收件箱、邮件发件人与正文、邮件同步，以及从邮件取得证据。",
    "knowledge": "已导入的本地文档、笔记、网页快照或跨来源知识证据检索。",
    "matter": "已保存的待办、事项、提醒和任务记录的查询或修改。",
    "filesystem": "用户给出明确文件路径，要求读取或定点编辑该文件。",
    "bash": "在工作区列目录、搜索代码文本、运行命令、测试或构建。",
    "none": "当前请求和会话上下文已含全部答案，不需要新数据或工具操作。",
}


def _load_cases(path: Path, split: str) -> list[dict[str, Any]]:
    result = []
    ids = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict) or case.get("id") in ids:
            raise ValueError(f"Invalid or repeated case at line {number}")
        ids.add(case["id"])
        if case.get("split") == split:
            result.append(case)
    if not result:
        raise ValueError(f"No {split} cases in {path}")
    return result


def _question(case: dict[str, Any], variant: str) -> dict[str, Any]:
    if case["task"] == "package":
        if variant == "baseline":
            return {"decision": PACKAGE_FAST_QUESTION}
        if variant == "mail_knowledge":
            return {
                "decision": {
                    "type": "choice",
                    "instructions": "Choose which evidence source to inspect first.",
                    "criteria": {
                        name: PACKAGE_ADAPTED[name] for name in ("mail", "knowledge")
                    },
                }
            }
        if variant == "factorized":
            return {
                "decision": {
                    "type": "choice",
                    "instructions": (
                        "A tool package is needed. Suggest the first package to expand; "
                        "do not select a concrete tool."
                    ),
                    "criteria": {
                        name: description for name, description in PACKAGE_ADAPTED.items()
                        if name != "none"
                    },
                }
            }
        return {
            "decision": {
                "type": "choice",
                "instructions": "只建议本轮第一个需要展开的能力包；不要选择具体工具。",
                "criteria": PACKAGE_ADAPTED,
            }
        }
    if variant == "package_guarded" and case["task"] == "fast":
        return {"decision": PACKAGE_FAST_QUESTION}
    if variant == "factorized" and case["task"] == "knowledge":
        return KNOWLEDGE_FACTORIZED
    if case["task"] == "fast":
        criteria = (
            FAST_BASE if variant == "baseline" else
            FAST_FACTORIZED if variant == "factorized" else FAST_ADAPTED
        )
        instructions = (
            "Decide if no new tool call is needed."
            if variant == "baseline"
            else "Is a direct context answer supported by the supplied text?"
            if variant == "factorized"
            else "只判断是否能安全地直接进入 context answer；不生成答案。"
        )
    elif case["task"] == "tool":
        package = case["package"]
        criteria = dict((TOOL_BASE if variant == "baseline" else TOOL_ADAPTED)[package])
        if variant == "factorized":
            known_type = case.get("known_id_type")
            if not case.get("known_ids") or known_type != "mail_message":
                criteria.pop("mail.load_messages", None)
            if not case.get("known_ids") or known_type != "knowledge_chunk":
                criteria.pop("knowledge.load_chunks", None)
        instructions = (
            "Suggest the next tool, or leave it to the Agent."
            if variant == "baseline"
            else "Suggest a candidate only; Agent retains final choice and safety review."
            if variant == "factorized"
            else "仅为已展开的包提出下一步候选；这不是工具调用，具体选择、参数和安全审查由 Agent 保留。"
        )
    elif case["task"] == "knowledge":
        criteria = KNOWLEDGE_BASE if variant == "baseline" else KNOWLEDGE_ADAPTED
        instructions = (
            "Choose a suitable local knowledge retrieval strategy."
            if variant == "baseline"
            else "判断本地知识请求下一步最适合的检索意图；不自行扩大授权来源或执行查询。"
        )
    else:
        raise ValueError(f"Unknown task {case['task']!r}")
    return {
        "decision": {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        }
    }


def _state(case: dict[str, Any], variant: str) -> dict[str, Any]:
    if case["task"] == "package":
        return {"request": case["request"], "context": case.get("context", "")}
    if variant == "package_guarded" and case["task"] == "fast":
        return {"request": case["request"], "context": case["context"]}
    if variant == "factorized" and case["task"] == "knowledge":
        return {"request": case["request"]}
    if variant == "factorized" and case["task"] == "fast":
        return {"request": case["request"], "context": case["context"]}
    return {
        key: value
        for key, value in case.items()
        if key not in {"id", "split", "task", "expected"}
    }


def _pct(values: list[float], fraction: float) -> float:
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def _fast_guard(case: dict[str, Any], probs: dict[str, float], threshold: float) -> str:
    if (
        case.get("scope_same") is False
        or case.get("evidence_conflict") is True
        or float(case.get("context_age_hours", 0)) > 24
    ):
        return "retrieve"
    return "answer" if probs.get("answer", 0.0) >= threshold else "retrieve"


def _package_fast_guard(
    case: dict[str, Any], probs: dict[str, float], threshold: float
) -> str:
    if (
        case.get("scope_same") is False
        or case.get("evidence_conflict") is True
        or float(case.get("context_age_hours", 0)) > 24
    ):
        return "retrieve"
    return "answer" if probs.get("none", 0.0) >= threshold else "retrieve"


def _knowledge_decision(
    case: dict[str, Any], answers: dict[str, Any]
) -> tuple[str, str, dict[str, float], dict[str, Any]]:
    list_probability = float(answers["source_list"]["noul"])
    complex_probabilities = answers["complex"]["probabilities"]
    complex_probability = float(complex_probabilities["multi_step"])
    mode_probabilities = answers["mode"]["probabilities"]
    exact_markers = EXACT_MARKER.findall(case["request"])
    raw = (
        "source_discovery" if list_probability >= 0.5 else
        "multi_step" if answers["complex"]["choice"] == "multi_step" else
        answers["mode"]["choice"]
    )
    policy = (
        "source_discovery" if list_probability >= 0.4 else
        "exact_lookup" if len(exact_markers) == 1 and complex_probability < 0.65 else
        "multi_step" if complex_probability >= 0.65 else
        answers["mode"]["choice"]
    )
    details = {
        "source_list_probability": round(list_probability, 4),
        "complex_probabilities": complex_probabilities,
        "mode_probabilities": mode_probabilities,
        "structural_exact_marker_count": len(exact_markers),
    }
    return raw, policy, mode_probabilities, details


def _summarize(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_task[row["task"]].append(row)
    summary: dict[str, Any] = {}
    for task, task_rows in by_task.items():
        latencies = [row["latency_ms"] for row in task_rows]
        choices = [row["policy_choice"] for row in task_rows]
        expected = [row["expected"] for row in task_rows]
        item: dict[str, Any] = {
            "cases": len(task_rows),
            "raw_top1_correct": sum(row["choice"] == row["expected"] for row in task_rows),
            "policy_correct": sum(a == b for a, b in zip(choices, expected)),
            "p50_ms": round(statistics.median(latencies), 1),
            "p95_ms": round(_pct(latencies, 0.95), 1),
            "by_label": {
                label: {
                    "cases": sum(e == label for e in expected),
                    "policy_correct": sum(e == label and p == label for e, p in zip(expected, choices)),
                }
                for label in sorted(set(expected))
            },
        }
        if task == "fast":
            item["false_direct_answers"] = sum(
                actual == "answer" and gold == "retrieve"
                for actual, gold in zip(choices, expected)
            )
            item["direct_answer_coverage"] = sum(actual == "answer" for actual in choices)
            item["threshold"] = threshold
        if task == "tool":
            item["top2_correct"] = sum(
                row["expected"] in row["top2"] for row in task_rows
            )
            concrete_rows = [row for row in task_rows if row["expected"] != "agent_decides"]
            read_rows = [
                row for row in task_rows if row["expected"] in READ_ONLY_TOOL_HINTS
            ]
            defer_rows = [row for row in task_rows if row["expected"] == "agent_decides"]
            item["concrete_hint_cases"] = len(concrete_rows)
            item["concrete_hint_top2_correct"] = sum(
                row["expected"] in row["top2"] for row in concrete_rows
            )
            item["read_tool_cases"] = len(read_rows)
            item["read_tool_top1_correct"] = sum(
                row["choice"] == row["expected"] for row in read_rows
            )
            item["read_tool_top2_correct"] = sum(
                row["expected"] in row["top2"] for row in read_rows
            )
            item["agent_decides_cases"] = len(defer_rows)
            item["agent_decides_top1_correct"] = sum(
                row["choice"] == "agent_decides" for row in defer_rows
            )
        if task == "package":
            item["top2_correct"] = sum(
                row["expected"] in row["top2"] for row in task_rows
            )
            active_rows = [row for row in task_rows if row["expected"] != "none"]
            item["active_package_cases"] = len(active_rows)
            item["active_package_top1_correct"] = sum(
                row["choice"] == row["expected"] for row in active_rows
            )
            item["active_package_top2_correct"] = sum(
                row["expected"] in row["top2"] for row in active_rows
            )
        summary[task] = item
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", type=Path,
        default=Path("evals/fixtures/decision_models/laya_routing_study.jsonl"),
    )
    parser.add_argument(
        "--split", choices=("dev", "holdout", "transfer", "challenge"), required=True
    )
    parser.add_argument(
        "--variant",
        choices=("baseline", "adapted", "factorized", "package_guarded", "mail_knowledge"),
        required=True,
    )
    parser.add_argument("--fast-threshold", type=float, default=0.5)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 <= args.fast_threshold <= 1 or args.threads < 1 or args.warmups < 0:
        parser.error("threshold must be in [0, 1], threads positive, warmups nonnegative")

    import torch
    import laya

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    started = time.perf_counter()
    agent = laya.load("convaiinnovations/laya-multilingual", device="cpu")
    load_ms = (time.perf_counter() - started) * 1000
    cases = _load_cases(args.cases, args.split)
    if args.warmups:
        warmup_case = cases[0]
        for _ in range(args.warmups):
            agent.predict(
                _state(warmup_case, args.variant),
                _question(warmup_case, args.variant),
            )

    rows = []
    for case in cases:
        started = time.perf_counter()
        response = agent.predict(_state(case, args.variant), _question(case, args.variant))
        latency_ms = (time.perf_counter() - started) * 1000
        answer_details: dict[str, Any] = {}
        if args.variant == "factorized" and case["task"] == "knowledge":
            choice, policy_choice, probabilities, answer_details = _knowledge_decision(
                case, response["answers"]
            )
        elif args.variant == "package_guarded" and case["task"] == "fast":
            answer = response["answers"]["decision"]
            probabilities = {
                str(name): float(probability)
                for name, probability in answer["probabilities"].items()
            }
            choice = "answer" if answer["choice"] == "none" else "retrieve"
            policy_choice = _package_fast_guard(
                case, probabilities, args.fast_threshold
            )
            answer_details = {"selected_package": answer["choice"]}
        else:
            answer = response["answers"]["decision"]
            probabilities = {
                str(name): float(probability)
                for name, probability in answer["probabilities"].items()
            }
            choice = str(answer["choice"])
            policy_choice = (
                _fast_guard(case, probabilities, args.fast_threshold)
                if case["task"] == "fast"
                else choice
            )
        top2 = [
            name for name, _ in sorted(
                probabilities.items(), key=lambda pair: pair[1], reverse=True
            )[:2]
        ]
        rows.append({
            "case_id": case["id"],
            "task": case["task"],
            "expected": case["expected"],
            "choice": choice,
            "probabilities": probabilities,
            "top2": top2,
            "latency_ms": round(latency_ms, 1),
            "policy_choice": policy_choice,
            "answer_details": answer_details,
            "case": case,
        })
    report = {
        "model": "convaiinnovations/laya-multilingual",
        "split": args.split,
        "variant": args.variant,
        "model_load_ms": round(load_ms, 1),
        "threads": args.threads,
        "summary": _summarize(rows, args.fast_threshold),
        "rows": [{key: value for key, value in row.items() if key != "case"} for row in rows],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
