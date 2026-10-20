"""Explicit session-only shadow replay. Never publish into the source database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.background_llm import SelectedBackgroundClient
from app.core.config import Settings
from app.core.context_projection import render_context
from app.core.context_reconcile import propose_delta, select_prefix
from app.core.llm import build_llm_service
from app.core.llm_workloads import LLMWorkloadController, workload_scope
from app.core.prompt_tokens import PromptTokenCounter
from app.core.sessions import SessionService
from app.storage.context_records import ContextRecordStore, source_digest
from app.storage.db import connect, init_db
from evals.lka_evals.live_budget import LiveBudget, MeteredClient


class ReplayClient(MeteredClient):
    """Keep production's uncapped output; reserve full capacity in the eval ledger."""

    def __init__(self, *args, output, output_reservation, **kwargs):
        super().__init__(*args, **kwargs)
        self.output = output
        self.output_reservation = output_reservation
        self.records = []

    def _reserve(self, request):
        _, call_id = super()._reserve(request.model_copy(update={"max_output_tokens": self.output_reservation}))
        return request, call_id

    async def complete(self, request):
        record = {"request": request.model_dump(mode="json")}
        self.records.append(record)
        started = perf_counter()
        try:
            response = await super().complete(request)
            record["response"] = response.model_dump(mode="json")
            return response
        except BaseException as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["seconds"] = perf_counter()-started
            self.output.write_text(json.dumps(self.records, ensure_ascii=False, indent=2))


def copy_session(source, session, destination, *, through_seq=None):
    if destination.resolve() == source.resolve():
        raise ValueError("Replay must not use the source database")
    init_db(destination)
    sessions = SessionService(lambda: connect(destination))
    with connect(destination) as conn:
        sessions._ensure_context_state_table(conn)
    tables = ("agent_sessions", "agent_session_context_state", "agent_session_context_windows", "agent_session_context_messages")
    with sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True) as original, connect(destination) as copied:
        original.row_factory = sqlite3.Row
        original.execute("BEGIN")
        for table in tables:
            rows = original.execute(f"SELECT * FROM {table} WHERE session_id=?", (session,)).fetchall()
            if through_seq is not None and table == "agent_session_context_messages":
                rows = [row for row in rows if row["seq"] <= through_seq]
                if not rows or rows[-1]["seq"] != through_seq:
                    raise ValueError("Selected source watermark does not exist")
            if table == "agent_sessions" and not rows:
                raise ValueError("Requested session not found")
            if rows:
                columns = ",".join(rows[0].keys())
                placeholders = ",".join("?" for _ in rows[0])
                copied.executemany(f"INSERT INTO {table}({columns}) VALUES({placeholders})", [tuple(row) for row in rows])
        if through_seq is not None:
            state = copied.execute("SELECT covered_seq FROM agent_session_context_state WHERE session_id=?", (session,)).fetchone()
            if state and state[0] > through_seq:
                raise ValueError("Legacy summary exceeds selected history; use an earlier frozen snapshot")
            copied.execute("UPDATE agent_session_context_state SET next_seq=? WHERE session_id=?", (through_seq+1, session))


def copy_atomic_state(source, session, destination):
    """Resume an isolated evaluation; never copy jobs, leases or other sessions."""
    if source.resolve() == destination.resolve():
        raise ValueError("Replay must not use the source database")
    with sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True) as original, connect(destination) as copied:
        original.row_factory = sqlite3.Row
        original.execute("BEGIN")
        for table in ("context_record_heads", "context_records", "context_record_events"):
            rows = original.execute(f"SELECT * FROM {table} WHERE session_id=?", (session,)).fetchall()
            if rows:
                columns = ",".join(rows[0].keys())
                placeholders = ",".join("?" for _ in rows[0])
                copied.executemany(f"INSERT INTO {table}({columns}) VALUES({placeholders})", [tuple(row) for row in rows])


def run(args):
    if args.output.resolve().is_relative_to(ROOT):
        import subprocess

        if subprocess.run(["git", "check-ignore", "-q", str(args.output.resolve())], cwd=ROOT, check=False).returncode != 0:
            raise ValueError("Private replay output must be Git-ignored")
    args.output.mkdir(parents=True, exist_ok=False)
    database = args.output / "isolated.sqlite3"
    copy_session(args.source, args.session, database, through_seq=args.through_seq)
    factory = lambda: connect(database)
    store = ContextRecordStore(factory)
    store.ensure_schema()
    if getattr(args, "resume_records", False):
        copy_atomic_state(args.source, args.session, database)
    initial = store.snapshot(args.session)
    target = initial["messages"][-1]["seq"] - args.keep_tail
    if initial["covered_seq"] > target:
        raise ValueError("Existing atomic state exceeds the replay target")
    report = {"session_id": args.session, "source_messages": len(initial["messages"]), "target_seq": target,
              "source_prefix_digest": source_digest(initial["messages"]), "run": args.run,
              "production_modified": False, "batches": [], "semantic_quality": "requires_separate_review",
              "source_protocol": "passage_ids_v1", "initial_covered_seq": initial["covered_seq"]}
    (args.output / "source_messages.json").write_text(json.dumps(initial["messages"], ensure_ascii=False, indent=2))
    if not args.run:
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report))
        return
    config = Settings().load_local_config()
    service = build_llm_service(config.llm)
    if service is None:
        raise RuntimeError("Configured real model required")
    name = config.memory.background_client_name or config.llm.default_client
    provider = service.registry.get(name) if name else service.registry.list_clients()[0]
    model = args.model
    resolved = config.llm.resolve_model_config(provider.name, model)
    if model != "deepseek-flash" or resolved is None:
        raise ValueError("This ledger only supports configured DeepSeek Flash pricing")
    counter = PromptTokenCounter(resolved.tokenizer_json_path)
    budget = LiveBudget(args.ledger or args.output / "budget.sqlite3", usd_limit=args.usd_limit,
                        call_limit=args.call_limit, search_limit=0)
    service.registry.register_client(ReplayClient(provider, budget, allowed_model=model,
        output=args.output / "model_calls.json", output_reservation=resolved.context_window_tokens))
    service.workloads = LLMWorkloadController(database)
    client = SelectedBackgroundClient(service, provider.name, model, limit_output_tokens=False,
                                      request_timeout_seconds=180)
    try:
        while True:
            snapshot = store.snapshot(args.session, target_seq=target)
            messages = select_prefix(snapshot, target_seq=target, input_tokens=args.input_tokens, counter=counter)
            if not messages:
                break
            snapshot["messages"] = [r for r in snapshot["messages"] if r["seq"] <= messages[-1]["seq"]]
            snapshot["source_digest"] = source_digest(snapshot["messages"])
            started = perf_counter()
            with workload_scope("background_memory", task_id="atomic-replay"):
                changes, response = propose_delta(client, snapshot, messages, input_tokens=args.input_tokens, counter=counter)
            revision = store.publish(snapshot, changes)
            report["batches"].append({"from_seq": messages[0]["seq"], "to_seq": messages[-1]["seq"],
                                      "revision": revision, "changes": len(changes), "seconds": perf_counter()-started,
                                      "usage": response.usage})
        state = store.snapshot(args.session)
        projection = render_context(state["records"], covered_seq=state["covered_seq"], budget=4096, counter=counter)
        (args.output / "records.json").write_text(json.dumps(state["records"], ensure_ascii=False, indent=2))
        (args.output / "projection.txt").write_text(projection["text"])
        sessions = SessionService(factory, context_token_counter=counter)
        sessions.atomic_context_store, sessions.atomic_context_mode = store, "active"
        prompt = sessions.get_prompt_context_window(session_id=args.session)
        timings = []
        for _ in range(30):
            started = perf_counter()
            sessions.get_prompt_context_window(session_id=args.session)
            timings.append((perf_counter()-started)*1000)
        timings.sort()
        assert source_digest(state["messages"]) == report["source_prefix_digest"]
        report.update(covered_seq=state["covered_seq"], records=len(state["records"]), projection_tokens=projection["tokens"],
                      omitted_records=len(projection["omitted_ids"]), effective_method=prompt.summary_metadata.get("method"),
                      raw_tail_messages=len(prompt.recent_messages), history_tokens=prompt.token_estimate,
                      read_latency_ms={"p50": timings[15], "p95": timings[28]})
    except BaseException as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        raise
    finally:
        report["budget"] = budget.snapshot()
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k not in {"batches", "error"}}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--resume-records", action="store_true", help="Copy selected session atomic state from the explicit source for an incremental isolated replay")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--keep-tail", type=int, default=2)
    parser.add_argument("--through-seq", type=int, help="Freeze an explicit historical prefix, independent of new production turns")
    parser.add_argument("--input-tokens", type=int, default=90000)
    parser.add_argument("--usd-limit", type=float, default=2)
    parser.add_argument("--call-limit", type=int, default=4)
    parser.add_argument("--ledger", type=Path)
    parsed = parser.parse_args()
    if parsed.keep_tail < 0 or parsed.input_tokens < 2048 or (parsed.through_seq is not None and parsed.through_seq < 1):
        parser.error("Invalid replay limits")
    run(parsed)
