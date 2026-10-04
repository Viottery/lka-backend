"""Bounded synthetic evaluation of the actual background summarizer.

No user data is read. --remote makes at most four configured provider calls.
The keyword checks are regression probes, not a model-judged quality score.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from app.core.background_jobs import BackgroundJobStore
from app.core.background_llm import complete_text_in_worker
from app.core.llm import build_llm_service
from app.core.llm.errors import LLMClientError
from app.core.llm_workloads import LLMWorkloadController, workload_scope
from app.core.local_config import load_local_config
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.sessions import SessionRecentMessage, SessionService
from app.domains.memory import MemoryService

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    {"id": "mail-correction", "messages": [
        "目标：整理演出票务邮件，比较上海场和杭州场。",
        "更正：不是10月12日，是10月21日；不要自动付款，票价上限600元。",
        "事项尚未完成：等待主办方确认退票政策，再决定城市。",
    ], "required": ["10月21日", "不要自动付款", "600", "退票"]},
    {"id": "project-decision", "messages": [
        "本项目决定采用SQLite持久化。必须保留来源；项目甲与项目乙不能共享偏好。",
        "更正：发布截止时间改为11月5日，不是11月2日。不要删除旧数据库。",
        "未完成：需要验证断网恢复，再进入灰度发布。",
    ], "required": ["SQLite", "不能共享偏好", "11月5日", "不要删除旧数据库", "断网恢复"]},
]


class CountingClient:
    def __init__(self, service: Any):
        self.service, self.calls = service, 0
        self.tokens = {"input": 0, "output": 0}

    def supports_thinking_control(self, **kwargs):
        return self.service.supports_thinking_control(**kwargs)

    def complete_text(self, **kwargs):
        self.calls += 1
        response = complete_text_in_worker(self.service, **kwargs)
        usage = response.usage or {}
        self.tokens["input"] += usage.get("prompt_tokens", usage.get("input_tokens", 0))
        self.tokens["output"] += usage.get("completion_tokens", usage.get("output_tokens", 0))
        return response


def evaluate(*, remote=False, config_path=None, budget_ledger=None):
    config = load_local_config(config_path or ROOT / "config/local.toml").llm if remote else None
    if config is not None and not config.is_disabled():
        if budget_ledger is None:
            raise ValueError("Remote evaluation requires --budget-ledger")
        if config.model != "deepseek-flash":
            raise ValueError("unpriced model refused: evaluation pricing covers deepseek-flash only")
    if config is not None:
        config = config.model_copy(update={"timeout_seconds": 30, "clients": [
            client.model_copy(update={"timeout_seconds": 30}) for client in config.clients]})
    service = build_llm_service(config) if config is not None else None
    if remote and service is None:
        raise RuntimeError("Remote evaluation requires a configured LLM service")
    live_budget = None
    if service is not None and budget_ledger is not None:
        from evals.lka_evals.live_budget import LiveBudget, instrument_service

        live_budget = LiveBudget(budget_ledger, usd_limit=50)
        instrument_service(service, live_budget, allowed_model="deepseek-flash")
    client = CountingClient(service) if service else None
    with tempfile.TemporaryDirectory(prefix="lka-compaction-eval-") as directory:
        db = str(Path(directory) / "eval.sqlite3")
        memory = MemoryService(db)
        memory.ensure_schema()
        store = BackgroundJobStore(db)
        store.ensure_schema()
        if service:
            service.workloads = LLMWorkloadController(db)
        coordinator = MemoryBackgroundCoordinator(
            db_path=db, memory=memory, store=store,
            session_service=SessionService(lambda: sqlite3.connect(db)), llm_client=client,
        )
        results = []
        for case in CASES:
            messages = [SessionRecentMessage(role="user", content=text, created_at="2026-10-02T00:00:00Z",
                                             trace_id=f"{case['id']}-{index}")
                        for index, text in enumerate(case["messages"])]
            start = time.perf_counter()
            error = None
            try:
                with workload_scope("background_memory", task_id=case["id"], max_tokens=32768):
                    summary = coordinator._summarize("", messages, 16384)
            except LLMClientError as exc:
                error = type(exc).__name__
                summary = ""
            missing = [value for value in case["required"] if value not in summary]
            results.append({"id": case["id"], "missing_critical_terms": missing,
                            "passed": not missing, "summary": summary,
                            "provider_error": error,
                            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)})
        return {"synthetic_only": True, "production_slo": False, "mode": "remote" if remote else "offline",
                "cases": len(results), "passed": sum(result["passed"] for result in results),
                "provider_calls": client.calls if client else 0,
                "provider_usage_available": remote and all(result["provider_error"] is None for result in results),
                "provider_tokens": client.tokens if client else {"input": 0, "output": 0},
                "budget_ledger_path": str(budget_ledger) if live_budget is not None else None,
                "budget_ledger": live_budget.snapshot() if live_budget is not None else None,
                "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--budget-ledger", type=Path,
        help="Persistently meter remote provider calls with the shared USD 50 evaluation budget",
    )
    arguments = parser.parse_args()
    print(json.dumps(evaluate(remote=arguments.remote, config_path=arguments.config,
                              budget_ledger=arguments.budget_ledger), ensure_ascii=False, indent=2))
