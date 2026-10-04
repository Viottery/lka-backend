"""Goal-only, budgeted real-model probes with durable private run artifacts.

Run explicitly with --remote. Each case has its own database/workspace. This
does not touch the running backend, start mail sync, or invoke coding experts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from evals.lka_evals.fixtures import apply_setup
from evals.lka_evals.live_budget import LiveBudget, instrument_service

CASES = {
    "known_fact": {
        "goal": "这个项目目前的状态是什么？唯一标识是什么？",
        "files": {"README.md": "# Sample project\nStatus: amber\nUnique marker: citrus-642\n"},
        "facts": ["amber", "citrus-642"], "read_only": True,
    },
    "long_file_tail": {
        "goal": "检查 deployment.log：最后一次部署最终成功了吗？给出最后一次的版本与错误原因。不要把之前成功的部署当成最终结果。",
        "files": {"deployment.log": "2026-10-01 release v3.1 SUCCESS\n" +
                  "INFO heartbeat normal; deployment monitor active\n" * 1800 +
                  "2026-10-05 release v3.9 FAILED: migration checksum mismatch CHECK_783\n"},
        "facts": ["v3.9", "checksum", "CHECK_783"], "read_only": True,
    },
    "conflicting_files": {
        "goal": "这个项目的发布窗口最终定在什么时候？对照资料中的更新，告诉我现在有效的安排和仍需确认的部分。",
        "files": {
            "README.md": "Release notes are in docs/.\n",
            "docs/original.md": "2026-09-01: release scheduled 2026-10-08 09:00, region west.\n",
            "docs/update.md": "2026-10-03: supersedes original schedule. Release 2026-10-09 14:30, region east. Rollback owner remains unconfirmed.\n",
        }, "facts": ["2026-10-09", "14:30", "east"], "read_only": True,
    },
    "native_code_fix": {
        "goal": "这个小项目分页会漏数据。请找出问题并修复，验证分页正常且没有破坏边界行为。",
        "files": {
            "README.md": "# Page demo\nPure Python, no external dependencies. Tests: python -m unittest discover -s tests\n",
            "paging.py": "def page(items, offset, limit):\n    if offset < 0 or limit < 0:\n        raise ValueError('negative bounds')\n    return items[offset:limit]\n",
            "tests/test_paging.py": "import unittest\nfrom paging import page\n\nclass PagingTests(unittest.TestCase):\n    def test_offset(self):\n        self.assertEqual(page(list(range(10)), 4, 3), [4, 5, 6])\n    def test_empty(self):\n        self.assertEqual(page([1, 2], 0, 0), [])\n    def test_past_end(self):\n        self.assertEqual(page([1, 2], 9, 3), [])\n    def test_invalid(self):\n        with self.assertRaises(ValueError):\n            page([1], -1, 2)\n",
        }, "verify_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
    },
    "bash_diagnosis": {
        "goal": "应用启动失败，帮我查清原因，给出日志与配置证据。只诊断，不修改任何文件。",
        "files": {
            "README.md": "Configuration: config/app.env. Startup output: logs/startup.log.\n",
            "config/app.env": "DB_PORT=5433\nSERVICE_PORT=8766\n",
            "logs/startup.log": "INFO application boot\nERROR database refused connection localhost:5433\nINFO database service listening localhost:5432\n",
        }, "facts": ["5433", "5432"], "read_only": True,
    },
    "mail_conflict": {
        "goal": "Thesis defense 最终时间和地点是什么？同时告诉我材料提交截止时间。按最新邮件核对，不要混用过时安排。",
        "files": {}, "setup": {"mail_fixtures": ["hard_mailbox"]},
        "facts": ["2026-08-24", "10:30", "Room C"], "read_only": True,
    },
    "web_official": {
        "goal": "核实 Python 3.13 的 free-threaded 模式是不是默认开启，以及扩展模块可能有什么兼容性限制。请查官方资料并引用来源，不要只根据搜索摘要回答。",
        "files": {}, "web": True, "facts": ["3.13"], "read_only": True,
    },
}


class _SearchReservation:
    def __init__(self, budget):
        self.budget = budget

    def reserve(self):
        call = self.budget.reserve(kind="search", stage="web.search")
        # Charge a query at dispatch. Failure does not return the quota.
        self.budget.finish(call, None, status="dispatched")


def _hashes(workspace: Path) -> dict[str, str]:
    return {p.relative_to(workspace).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in workspace.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


async def run_case(case_id: str, *, output: Path, budget: LiveBudget, planning: bool = False,
                   timeout: float = 180) -> dict:
    case = CASES[case_id]
    root = output / (case_id + "_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    for name, content in case["files"].items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    before = _hashes(workspace)
    settings = Settings(LKA_DATA_DIR=root / "data", LKA_WORKSPACE_ROOTS=str(workspace))
    config = settings.load_local_config()
    config = config.model_copy(update={
        "agent": config.agent.model_copy(update={"orchestrator": "langgraph",
            "checkpoint_backend": "sqlite", "multi_agent_planning_enabled": planning,
            "codex_expert_enabled": False, "max_decision_steps": 12}),
        "mail": config.mail.model_copy(update={
            "outlook": config.mail.outlook.model_copy(update={"enabled": False}),
            "imap": config.mail.imap.model_copy(update={"enabled": False})}),
        "memory": config.memory.model_copy(update={"background_enabled": False}),
        "message_history": config.message_history.model_copy(update={"background_enabled": False}),
        "embedding": config.embedding.model_copy(update={"enabled": False}),
        "reranker": config.reranker.model_copy(update={"enabled": False}),
        # Writes are explicitly authorized inside this synthetic fixture only.
        "safety": config.safety.model_copy(update={"tool_review_mode": "skip"}),
    })
    with patch.object(Settings, "load_local_config", return_value=config):
        runtime = LocalKnowledgeAgentRuntime(settings)
    instrument_service(runtime.agent_llm_client, budget, allowed_model=config.llm.model)
    search_tool = runtime.tool_registry.get_tool_or_none("web.search")
    if search_tool:
        search_tool.adapter.quota = _SearchReservation(budget)
    apply_setup(runtime, case.get("setup"))
    session = runtime.create_session(title="quality: " + case_id)
    session_id = session.session.session_id
    runtime.set_session_workspace(session_id=session_id, path=str(workspace.resolve()), platform="linux")
    started = time.perf_counter()
    report = {"case_id": case_id, "goal": case["goal"], "model": config.llm.model,
              "planning": planning, "session_id": session_id, "private_artifacts": str(root),
              "budget_before": budget.snapshot(), "test_output_cap_if_unspecified": 16384}
    try:
        result = await asyncio.wait_for(runtime.run_agent_turn_async(
            session_id=session_id, user_input=case["goal"]), timeout=timeout)
        report["result"] = result.model_dump(mode="json")
        answer = result.answer
        checks = {"fact_coverage": all(s.lower() in answer.lower() for s in case.get("facts", []))}
        if case.get("read_only"):
            checks["files_unchanged"] = _hashes(workspace) == before
        if case.get("verify_command"):
            completed = await asyncio.to_thread(subprocess.run, case["verify_command"],
                cwd=workspace, capture_output=True, text=True, timeout=15, check=False)
            report["independent_verification"] = {"exit_code": completed.returncode,
                "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]}
            checks["independent_tests_pass"] = completed.returncode == 0
            checks["tests_not_modified"] = all(_hashes(workspace).get(p) == h
                for p, h in before.items() if p.startswith("tests/"))
        if case.get("web"):
            checks["page_evidence_loaded"] = any(e.tool_name == "web.open" and e.result.get("status") == "completed"
                                                for e in result.tool_events)
        # Coverage and a successful fetch are necessary, not semantic-proof scores.
        report["checks"] = checks
        report["mechanical_pass"] = all(checks.values())
    except Exception as exc:  # noqa: BLE001 - preserve per-case failure evidence
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["mechanical_pass"] = False
    finally:
        report["task_wall_seconds"] = round(time.perf_counter() - started, 3)
        report["budget_after"] = budget.snapshot()
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        runtime.stop()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--planning", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, default=Path("data/quality_runs/linux_20261005"))
    args = parser.parse_args()
    if args.list:
        for name in CASES:
            print(name)
        return
    if not args.remote or not args.case:
        parser.error("explicit --remote and --case are required; no implicit paid full-suite run")
    output = args.output.resolve()
    budget = LiveBudget(output / "budget.sqlite3")
    for case_id in args.case:
        report = asyncio.run(run_case(case_id, output=output, budget=budget,
                                     planning=args.planning, timeout=args.timeout))
        print(json.dumps({k: report.get(k) for k in
              ("case_id", "task_wall_seconds", "mechanical_pass", "checks", "error", "private_artifacts",
               "budget_after")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
