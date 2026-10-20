"""Opt-in paired synthetic harness probe; never connects to the daily backend."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import threading
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.core.config import Settings
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded, instrument_service
from scripts import eval_realworld

CONTROL_STAGES = frozenset({"route", "decision", "decision_repair"})
CASE_IDS = ("user_plan_correction", "user_record_verification", "conflicting_files",
            "long_file_tail", "efficiency_multihop", "efficiency_softmax")
EXTRA_CASES = {
    "efficiency_multihop": {
        "goal": "核对这个项目能否发布：找到当前维护团队、团队负责人和适用审批政策。"
                "给出各项依据；实际审批完成与否没有记录就明确说明。不要修改文件。",
        "files": {
            "README.md": "Project BirchHarbor. Evidence files are in records/.\n",
            "records/project.txt": "BirchHarbor: team MT-41; policy P7. Old MT-19 assignment revoked.\n",
            "records/teams.txt": "MT-41 owner: Lena Park. MT-19 owner: Ivan Cole.\n",
            "records/policies.txt": "P7 requires 3 distinct approvers. P2 requires 1. Actual approvals are not recorded.\n",
        }, "facts": ["MT-41", "Lena Park", "P7"], "read_only": True,
    },
    "efficiency_softmax": {
        "goal": "我刚学softmax，p_i是分子e的x_i次方吗？请先纠正这个概念，"
                "再用输入(0,0)举例，并解释对自身输入和对另一个输入的偏导为何不同。"
                "一小步一小步解释，不用查外部资料。",
        "files": {}, "facts": ["0.5"], "read_only": True,
    },
}


class AttemptBudget:
    """Durable attempt reservations, also charged to the existing shared ledger."""

    def __init__(self, ledger, tag, token_limit=1_000_000, call_limit=80):
        self.ledger, self.tag = ledger, tag
        self.token_limit, self.call_limit = token_limit, call_limit
        self.lock = threading.Lock()
        with ledger.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS efficiency_reservations("
                         "tag TEXT, id TEXT PRIMARY KEY, tokens INTEGER NOT NULL)")

    def reserve(self, **kwargs):
        if kwargs["kind"] != "llm":
            raise LiveBudgetExceeded("This experiment permits no external search")
        tokens = kwargs.get("incoming", 0) + kwargs.get("outgoing", 0)
        with self.lock, self.ledger.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            used, count = conn.execute(
                "SELECT COALESCE(SUM(tokens),0),COUNT(*) FROM efficiency_reservations WHERE tag=?",
                (self.tag,),
            ).fetchone()
            if used + tokens > self.token_limit or count >= self.call_limit:
                raise LiveBudgetExceeded("Efficiency attempt reservation limit reached")
            # Commit local admission before the shared ledger takes its own lock.
            # A refused shared reservation remains locally spent, conservatively.
            conn.execute("INSERT INTO efficiency_reservations VALUES(?,?,?)", (self.tag, uuid4().hex, tokens))
        return self.ledger.reserve(**{**kwargs, "stage": self.tag + ":" + kwargs["stage"]})

    def finish(self, *args, **kwargs):
        self.ledger.finish(*args, **kwargs)

    def snapshot(self):
        with self.ledger.connect() as conn:
            used, calls = conn.execute(
                "SELECT COALESCE(SUM(tokens),0),COUNT(*) FROM efficiency_reservations WHERE tag=?",
                (self.tag,),
            ).fetchone()
        return {"attempt_reserved_tokens": used, "attempt_calls": calls,
                "token_limit": self.token_limit, "shared": self.ledger.snapshot()}


class StageClient:
    def __init__(self, client, arm, records):
        self.client, self.arm, self.records = client, arm, records

    def __getattr__(self, name):
        return getattr(self.client, name)

    def prepare(self, request):
        stage = request.metadata.get("stage")
        if self.arm == "control_no_thinking" and stage in CONTROL_STAGES:
            request = request.model_copy(update={"thinking_enabled": False})
        self.records.append({"stage": stage, "thinking_enabled": request.thinking_enabled,
                             "model": request.model, "summary": request.prompt_summary})
        return request

    async def complete(self, request):
        return await self.client.complete(self.prepare(request))

    async def stream(self, request):
        async for event in self.client.stream(self.prepare(request)):
            yield event


def paired_summary(rows, candidate="control_no_thinking"):
    eligible = {(r["case_id"], r["arm"]): r for r in rows
                if not r.get("error") and not r.get("budget_exhausted") and r.get("metrics")}
    cases = sorted({case for case, _ in eligible if (case, "configured") in eligible
                    and (case, candidate) in eligible})
    summary = {"paired_cases": cases, "sample_count": len(cases),
               "quality_metric": "mechanical contract pass, not population accuracy"}
    for arm in ("configured", candidate):
        selected = [eligible[(case, arm)] for case in cases]
        summary[arm] = {"passes": sum(r["mechanical_pass"] for r in selected),
                        "wall_total": sum(r["wall_seconds"] for r in selected),
                        "wall_median": statistics.median(r["wall_seconds"] for r in selected) if selected else None,
                        **{key: sum(r["metrics"][key] for r in selected)
                           for key in ("llm_calls", "tool_calls", "input_tokens", "output_tokens")}}
    return summary


async def experiment(output, ledger_path, selected, *, resume=False, candidate="control_no_thinking"):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "paired_results.json"
    if manifest.exists() and not resume:
        raise ValueError("Existing experiment requires explicit --resume")
    config = Settings().load_local_config()
    clients = config.llm.client_configs()
    if config.llm.model != "deepseek-flash" or not clients or any(
        c.default_model != "deepseek-flash" or c.thinking_control != "deepseek" for c in clients
    ):
        raise ValueError("Requires explicitly configured DeepSeek Flash thinking-control capability")
    config = config.model_copy(update={
        "memory": config.memory.model_copy(update={"enabled": False, "background_enabled": False}),
        "message_history": config.message_history.model_copy(update={"enabled": False, "background_enabled": False}),
        "agent": config.agent.model_copy(update={"unified_entry_enabled": False}),
    })
    ledger = LiveBudget(ledger_path, usd_limit=50)
    token_limit = 1_000_000
    budget = AttemptBudget(ledger, "efficiency:" + str(output), token_limit=token_limit)
    protocol_path = output / "protocol.json"
    protocol = {"candidate": candidate, "token_limit": token_limit,
                "model": config.llm.model, "cases": list(selected)}
    if protocol_path.exists() and json.loads(protocol_path.read_text()) != protocol:
        raise ValueError("Resume cannot change the experiment protocol or limits")
    protocol_path.write_text(json.dumps(protocol, indent=2))
    results = json.loads(manifest.read_text()) if manifest.exists() else []
    completed = {(r["case_id"], r["arm"]) for r in results}
    class SyntheticRuntime(eval_realworld.LocalKnowledgeAgentRuntime):
        def __init__(self, settings):
            super().__init__(settings)
            # Do not import personal guidance or invoke foreground memory learning.
            self.agent_turn_loop.instruction_files = None
            self.agent_turn_loop.memory_pre_turn_callback = None
    with patch.dict(eval_realworld.CASES, EXTRA_CASES):
        for index, case_id in enumerate(selected):
            arms = ("configured", candidate) if index % 2 == 0 else (candidate, "configured")
            for arm in arms:
                if (case_id, arm) in completed:
                    continue
                calls = []
                def instrument(service, allowance, *, allowed_model, selected_arm=arm, dispatches=calls):
                    for client in service.registry.list_clients():
                        service.registry.register_client(StageClient(client, selected_arm, dispatches))
                    instrument_service(service, allowance, allowed_model=allowed_model)
                arm_config = config.model_copy(update={"agent": config.agent.model_copy(update={
                    "decision_thinking_enabled": False if arm == "decision_only" else None})})
                with patch.object(Settings, "load_local_config", return_value=arm_config), \
                     patch.object(eval_realworld, "LocalKnowledgeAgentRuntime", SyntheticRuntime), \
                     patch.object(eval_realworld, "instrument_service", instrument):
                    report = await eval_realworld.run_case(case_id, output=output / arm, budget=budget,
                                                          timeout=240, response_mode="stream")
                row = {"case_id": case_id, "arm": arm, "wall_seconds": report["task_wall_seconds"],
                       "metrics": report.get("metrics"), "checks": report.get("checks"),
                       "mechanical_pass": report["mechanical_pass"], "error": report.get("error"),
                       "answer": report.get("result", {}).get("answer"),
                       "artifacts": report["private_artifacts"], "dispatch_policy": calls,
                       "budget": budget.snapshot(),
                       "budget_exhausted": any(e.get("payload", {}).get("error_type") == "LiveBudgetExceeded"
                                               for e in report["run_events"])}
                results.append(row)
                manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2))
                print(json.dumps({k: row[k] for k in ("case_id", "arm", "wall_seconds", "metrics", "mechanical_pass", "error")}, ensure_ascii=False), flush=True)
                if row["error"] or row["budget_exhausted"]:
                    return results  # Preserve incomplete pairs, no automatic retry.
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--case", action="append", choices=CASE_IDS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--candidate", choices=["control_no_thinking", "decision_only"],
                        default="control_no_thinking")
    args = parser.parse_args()
    if not args.remote:
        print(json.dumps({"cases": args.case or CASE_IDS, "arms": ["configured", args.candidate],
                          "token_reservation_limit": 1_000_000, "calls": 80, "searches": 0}))
        return
    if not args.output or not args.ledger or not args.ledger.is_file():
        parser.error("Explicit output and existing shared ledger required")
    asyncio.run(experiment(args.output, args.ledger, args.case or CASE_IDS,
                           resume=args.resume, candidate=args.candidate))


if __name__ == "__main__":
    main()
