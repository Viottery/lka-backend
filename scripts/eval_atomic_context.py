"""Explicit session-only shadow replay. Never publish into the source database."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.background_llm import SelectedBackgroundClient
from app.core.config import Settings
from app.core.context_intent import render_intent
from app.core.context_projection import render_context, render_handoff
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


def next_batch_target(covered_seq: int, target_seq: int, batch_messages: int | None) -> int:
    """Bound a replay step by new source messages; None preserves historical batching."""
    if batch_messages is None:
        return target_seq
    if batch_messages < 2 or batch_messages % 2:
        raise ValueError("batch_messages must be a positive even number")
    return min(target_seq, covered_seq + batch_messages)


def replay_task_id(snapshot, profile, experiment_id=None):
    """One job per unprocessed prefix; retries of that prefix share a budget."""
    prefix = [r for r in snapshot["messages"] if r["seq"] <= snapshot["covered_seq"]]
    identity_parts = [snapshot["session_id"], profile, snapshot["covered_seq"], source_digest(prefix)]
    if experiment_id:
        identity_parts.append(experiment_id)
    identity = json.dumps(identity_parts)
    return "atomic-replay-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def copy_session(source, session, destination, *, through_seq=None, reset_legacy=False):
    if destination.resolve() == source.resolve():
        raise ValueError("Replay must not use the source database")
    init_db(destination)
    sessions = SessionService(lambda: connect(destination))
    with connect(destination) as conn:
        sessions._ensure_context_state_table(conn)
    tables = (("agent_sessions", "agent_session_context_messages") if reset_legacy else
              ("agent_sessions", "agent_session_context_state", "agent_session_context_windows",
               "agent_session_context_messages"))
    with sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True) as original, connect(destination) as copied:
        original.row_factory = sqlite3.Row
        original.execute("BEGIN")
        for table in tables:
            rows = original.execute(f"SELECT * FROM {table} WHERE session_id=?", (session,)).fetchall()
            if table == "agent_session_context_messages":
                rows.sort(key=lambda row: row["seq"])
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
        if reset_legacy:
            last = copied.execute("SELECT COALESCE(MAX(seq),0) FROM agent_session_context_messages WHERE session_id=?",
                                  (session,)).fetchone()[0]
            copied.execute("INSERT OR REPLACE INTO agent_session_context_state(session_id,revision,next_seq,covered_seq) "
                           "VALUES(?,0,?,0)", (session, last+1))
        elif through_seq is not None:
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
    profile = getattr(args, "profile", "general")
    experiment_id = getattr(args, "experiment_id", None)
    if experiment_id is not None and (not experiment_id.strip() or len(experiment_id) > 100):
        raise ValueError("experiment_id must contain 1..100 characters and be reused on retry")
    batch_messages = getattr(args, "batch_messages", None)
    projection_tokens = getattr(args, "projection_tokens", None)
    if projection_tokens is None:
        projection_tokens = 512 if profile == "intent" else 2048 if profile == "handoff" else 4096
    if profile not in {"general", "handoff", "intent"}:
        raise ValueError("profile must be general, handoff or intent")
    if batch_messages is not None and (batch_messages < 2 or batch_messages % 2):
        raise ValueError("batch_messages must be a positive even number")
    if projection_tokens < 128 or (profile == "handoff" and projection_tokens > 4096) or (profile == "intent" and projection_tokens > 1024):
        raise ValueError("invalid projection token limit")
    if args.output.resolve().is_relative_to(ROOT):
        import subprocess

        if subprocess.run(["git", "check-ignore", "-q", str(args.output.resolve())], cwd=ROOT, check=False).returncode != 0:
            raise ValueError("Private replay output must be Git-ignored")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    database = args.output / "isolated.sqlite3"
    copy_session(args.source, args.session, database, through_seq=args.through_seq,
                 reset_legacy=profile in {"handoff", "intent"})
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
              "production_modified": False, "completed": False, "covered_seq": initial["covered_seq"], "batches": [],
              "source_protocol": "passage_ids_v1", "initial_covered_seq": initial["covered_seq"],
              "profile": profile, "batch_messages": batch_messages,
              "experiment_id": experiment_id,
              "projection_tokens": projection_tokens,
              "semantic_quality": "structure_only_requires_separate_semantic_review"}
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
    # A new output directory cannot erase failed usage or a circuit breaker.
    workload_ledger = getattr(args, "workload_ledger", None) or budget.path
    service.workloads = LLMWorkloadController(workload_ledger)
    report["workload_ledger"] = str(workload_ledger)
    client = SelectedBackgroundClient(service, provider.name, model, limit_output_tokens=False,
                                      request_timeout_seconds=180)
    try:
        while True:
            snapshot = store.snapshot(args.session, target_seq=target)
            batch_target = next_batch_target(snapshot["covered_seq"], target, batch_messages)
            messages = select_prefix(snapshot, target_seq=batch_target, input_tokens=args.input_tokens,
                                     counter=counter, profile=profile)
            if not messages:
                break
            snapshot["messages"] = [r for r in snapshot["messages"] if r["seq"] <= messages[-1]["seq"]]
            snapshot["source_digest"] = source_digest(snapshot["messages"])
            started = perf_counter()
            task_id = replay_task_id(snapshot, profile, experiment_id)
            report["active_task_id"] = task_id
            with workload_scope("background_memory", task_id=task_id):
                changes, response = propose_delta(client, snapshot, messages, input_tokens=args.input_tokens,
                                                  counter=counter, profile=profile)
            revision = store.publish(snapshot, changes)
            state_after = store.snapshot(args.session)
            if profile == "intent":
                projection_fn = render_intent
            elif profile == "handoff":
                projection_fn = render_handoff
            else:
                projection_fn = render_context
            projection = projection_fn(state_after["records"], covered_seq=state_after["covered_seq"],
                                       budget=projection_tokens, counter=counter)
            prefix = profile if profile in {"handoff", "intent"} else "projection"
            projection_name = f"{prefix}_{state_after['covered_seq']}.txt"
            (args.output / projection_name).write_text(projection["text"])
            state_name = f"state_{state_after['covered_seq']}.json"
            (args.output / state_name).write_text(json.dumps({
                "covered_seq": state_after["covered_seq"], "revision": revision,
                "records": state_after["records"], "selected_ids": projection["selected_ids"],
                "omitted_ids": projection["omitted_ids"], "tokens": projection["tokens"],
            }, ensure_ascii=False, indent=2))
            report["batches"].append({"from_seq": messages[0]["seq"], "to_seq": messages[-1]["seq"],
                                      "new_messages": len(messages), "covered_seq": state_after["covered_seq"],
                                      "revision": revision, "changes": len(changes), "seconds": perf_counter()-started,
                                      "task_id": task_id,
                                      "usage": getattr(response, "usage", None), "model_skipped": response is None,
                                      "projection_file": projection_name, "state_file": state_name,
                                      "projection_tokens": projection["tokens"],
                                      "omitted_records": len(projection.get("omitted_ids", [])),
                                      "records": len(state_after["records"])})
            report["covered_seq"] = state_after["covered_seq"]
            (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        state = store.snapshot(args.session)
        if profile == "intent":
            projection_fn = render_intent
        elif profile == "handoff":
            projection_fn = render_handoff
        else:
            projection_fn = render_context
        projection = projection_fn(
            state["records"], covered_seq=state["covered_seq"], budget=projection_tokens, counter=counter
        )
        (args.output / "records.json").write_text(json.dumps(state["records"], ensure_ascii=False, indent=2))
        (args.output / "projection.txt").write_text(projection["text"])
        sessions = SessionService(factory, context_token_counter=counter)
        sessions.atomic_context_store, sessions.atomic_context_mode = store, "active"
        sessions.atomic_context_profile = profile
        sessions.atomic_context_projection_tokens = projection_tokens
        if profile == "handoff":
            sessions.atomic_context_handoff_tokens = projection_tokens
        if profile == "intent":
            sessions.atomic_context_intent_tokens = projection_tokens
        prompt = sessions.get_prompt_context_window(session_id=args.session)
        timings = []
        for _ in range(30):
            started = perf_counter()
            sessions.get_prompt_context_window(session_id=args.session)
            timings.append((perf_counter()-started)*1000)
        timings.sort()
        assert source_digest(state["messages"]) == report["source_prefix_digest"]
        report.update(completed=state["covered_seq"] >= target,
                      covered_seq=state["covered_seq"], records=len(state["records"]), projection_tokens=projection["tokens"],
                      omitted_records=len(projection["omitted_ids"]), effective_method=prompt.summary_metadata.get("method"),
                      raw_history_messages=len(prompt.recent_messages),
                      raw_tail_messages=prompt.summary_metadata.get("raw_tail_messages", len(prompt.recent_messages)),
                      history_tokens=prompt.token_estimate,
                      read_latency_ms={"p50": timings[15], "p95": timings[28]})
    except BaseException as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        try:
            committed = store.snapshot(args.session, include_history=False)
            report.update(covered_seq=committed["covered_seq"], committed_records=len(committed["records"]))
            renderer = render_intent if profile == "intent" else render_handoff if profile == "handoff" else render_context
            partial = renderer(committed["records"], covered_seq=committed["covered_seq"], budget=projection_tokens, counter=counter)
            (args.output / "committed_records.json").write_text(json.dumps(committed["records"], ensure_ascii=False, indent=2))
            (args.output / "partial_projection.txt").write_text(partial["text"])
            report["committed_projection_tokens"] = partial["tokens"]
        except (ValueError, sqlite3.Error) as state_error:
            report["partial_state_error_type"] = type(state_error).__name__
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
    parser.add_argument("--profile", choices=("general", "handoff", "intent"), default="general")
    parser.add_argument("--experiment-id", help="Stable label for an independent experimental arm; reuse on retries, not an allowance reset")
    parser.add_argument("--batch-messages", type=int, help="Replay at most this many new messages per batch (positive even count)")
    parser.add_argument("--projection-tokens", type=int, help="Default: intent 512, handoff 2048, general 4096")
    parser.add_argument("--usd-limit", type=float, default=2)
    parser.add_argument("--call-limit", type=int, default=4)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--workload-ledger", type=Path, help="Persistent workload accounting; defaults to the budget ledger, never reset on retry")
    parsed = parser.parse_args()
    if (parsed.keep_tail < 0 or parsed.input_tokens < 2048
            or (parsed.through_seq is not None and parsed.through_seq < 1)
            or (parsed.batch_messages is not None and (parsed.batch_messages < 2 or parsed.batch_messages % 2))
            or (parsed.projection_tokens is not None and parsed.projection_tokens < 128)
            or (parsed.profile == "handoff" and parsed.projection_tokens is not None and parsed.projection_tokens > 4096)
            or (parsed.profile == "intent" and parsed.projection_tokens is not None and parsed.projection_tokens > 1024)):
        parser.error("Invalid replay limits")
    run(parsed)
