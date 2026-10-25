#!/usr/bin/env python3
"""Isolated loopback HTTP benchmark for durable event scheduling.

This measures the API, SQLite admission/binding path and dispatcher using a
50ms in-process stub. It never loads local TOML/.env or invokes a model/tool.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import random
import socket
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEXT_BYTES = 1024


def build_attempts(
    session_ids: list[str],
    requests: int,
    *,
    seed: int = 42,
    retry_fraction: float = 0.1,
    text_bytes: int = DEFAULT_TEXT_BYTES,
) -> list[dict[str, Any]]:
    """Construct deterministic requests, with exact payload retries included."""
    if not session_ids or requests < 1 or not 0 <= retry_fraction < 1 or text_bytes < 1:
        raise ValueError(
            "sessions/requests/text_bytes must be positive and retry_fraction in [0,1)"
        )
    duplicate_count = min(requests - 1, round(requests * retry_fraction))
    unique_count = requests - duplicate_count
    rng = random.Random(seed)
    unique: list[dict[str, Any]] = []
    for index in range(unique_count):
        # Deterministic entropy makes requests distinct while preserving exact byte size.
        raw = f"benchmark-{seed}-{index}-".encode() + rng.randbytes(text_bytes)
        content = base64.urlsafe_b64encode(raw)[:text_bytes].decode("ascii")
        unique.append(
            {
                "session_id": session_ids[index % len(session_ids)],
                "client_message_id": f"bench-{seed}-{index}",
                "user_input": content,
                "attachment_ids": [],
                "safety_review_mode": "skip",
            }
        )
    attempts = [dict(item) for item in unique]
    for _ in range(duplicate_count):
        attempts.append(dict(rng.choice(unique)))
    rng.shuffle(attempts)
    for item in attempts:
        if len(item["user_input"].encode("utf-8")) != text_bytes:
            raise AssertionError("generated user_input byte size is incorrect")
    return attempts


def validate_output_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    engineering = (ROOT / "docs" / "_engineering").resolve()
    if engineering not in resolved.parents:
        raise ValueError("--output must be inside ignored docs/_engineering")
    return resolved


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _clean_child_env(data_dir: Path, mode: str) -> dict[str, str]:
    # Provider, auth, local-config, cloud and user data settings are never inherited.
    blocked = (
        "LKA_",
        "OPENAI_",
        "ANTHROPIC_",
        "AZURE_",
        "MS_GRAPH_",
        "QDRANT_",
        "BRAVE_",
        "GOOGLE_",
        "COHERE_",
        "VOYAGE_",
        "MISTRAL_",
        "GROQ_",
        "OLLAMA_",
        "MAIL_",
        "OUTLOOK_",
        "SMTP_",
        "IMAP_",
        "MEMORY_",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(blocked) and key not in {"PYTHONPATH"}
    }
    env.update(
        {
            "LKA_DATA_DIR": str(data_dir),
            "LKA_HOST": "127.0.0.1",
            "LKA_SCHEDULING_MODE": mode,
            "LKA_DB_TIMING_ENABLED": "false",
            "PYTHONPATH": str(ROOT),
        }
    )
    return env


def _serve(data_dir: Path, port: int, mode: str, production_batches: bool = False) -> None:
    from app.core.config import Settings
    from app.core.local_config import (
        AgentConfig,
        LocalAppConfig,
        ManagementConfig,
        MemoryConfig,
        MessageHistoryConfig,
    )

    local_config = LocalAppConfig(
        agent=AgentConfig(unified_entry_enabled=True, checkpoint_backend="sqlite"),
        management=ManagementConfig(
            enabled=True, autonomous_enabled=False, safety_review_mode="skip"
        ),
        memory=MemoryConfig(enabled=False, background_enabled=False),
        message_history=MessageHistoryConfig(enabled=False, background_enabled=False),
    )
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        host="127.0.0.1",
        port=port,
        scheduling_mode=mode,
        db_timing_enabled=False,
        local_config_path=data_dir / "unused-local.toml",
    )
    Settings.load_local_config = lambda self: local_config

    import app.core.config as config_module

    config_module.get_settings = lambda: settings

    from app.core.runtime import LocalKnowledgeAgentRuntime

    original_start = LocalKnowledgeAgentRuntime.start

    def start_with_stub(runtime):
        def execute(run, guard):
            guard.check()
            if production_batches and run.metadata.get("session_input_id"):
                runtime.session_inputs.freeze_for_dispatch(run, guard)
                run = runtime.agent_run_manager.get_run(run.run_id)
            runtime.agent_run_manager.mark_running(run.run_id)
            time.sleep(0.05)
            guard.check()
            runtime.agent_run_manager.complete_run(
                run.run_id, result_snapshot={"benchmark_stub": True}
            )

        runtime.agent_dispatch.execute = execute
        runtime.agent_dispatch.resume = execute
        return original_start(runtime)

    LocalKnowledgeAgentRuntime.start = start_with_stub
    import uvicorn

    from app.api.main import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error", access_log=False)


def _readonly_db(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


def _audit_batch_membership(
    inputs: list[dict[str, Any]], runs: list[dict[str, Any]], dispatch_jobs: list[dict[str, Any]],
    *, production_batches: bool = False,
) -> dict[str, Any]:
    """Verify durable input-to-run batches, including ordered content identity."""
    input_by_id = {item["input_id"]: item for item in inputs}
    run_by_id = {item["run_id"]: item for item in runs}
    memberships: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    batch_ordered = True
    batch_same_session = True
    batch_fingerprints_match = True
    batch_consistent = True
    batch_widths: list[int] = []
    for run in runs:
        payload = run["record_payload"]
        metadata = payload.get("metadata", {})
        if production_batches:
            batch = metadata.get("session_input_batch")
            if not metadata.get("session_input_batch_frozen") or not isinstance(batch, list) or not batch:
                batch_consistent = False
                continue
        else:
            batch = [
                {
                    "input_id": item["input_id"], "message_id": item["message_id"],
                    "sequence": item["sequence"],
                    "content_sha256": hashlib.sha256(item["user_input"].encode()).hexdigest(),
                }
                for item in inputs if item.get("run_id") == run["run_id"]
            ]
        sequences = [item.get("sequence") for item in batch]
        batch_widths.append(len(batch))
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            batch_ordered = False
        for entry in batch:
            input_id = entry.get("input_id")
            source = input_by_id.get(input_id)
            if source is None or source.get("run_id") != run["run_id"]:
                batch_consistent = False
                continue
            memberships[input_id].append((run["run_id"], entry))
            if source["session_id"] != run["session_id"]:
                batch_same_session = False
            expected_hash = hashlib.sha256(source["user_input"].encode()).hexdigest()
            if (entry.get("message_id") != source["message_id"]
                or entry.get("sequence") != source["sequence"]
                or entry.get("content_sha256") != expected_hash):
                batch_fingerprints_match = False
        if payload.get("session_id", run["session_id"]) != run["session_id"]:
            batch_same_session = False
    each_input_once = all(len(memberships.get(key, [])) == 1 for key in input_by_id)
    duplicate_input_memberships = sum(max(0, len(items) - 1) for items in memberships.values())
    referenced_runs = {item.get("run_id") for item in inputs if item.get("run_id")}
    no_extra_runs = set(run_by_id) == referenced_runs
    job_run_ids = [item.get("payload", {}).get("run_id") for item in dispatch_jobs]
    one_job_per_run = (
        len(job_run_ids) == len(set(job_run_ids)) and set(job_run_ids) == set(run_by_id)
    )
    if not production_batches and any(width != 1 for width in batch_widths):
        batch_consistent = False
    return {
        "passed": all((batch_consistent, batch_ordered, batch_same_session,
                       batch_fingerprints_match, each_input_once, no_extra_runs, one_job_per_run)),
        "batch_consistent": batch_consistent,
        "batch_ordered": batch_ordered,
        "batch_same_session": batch_same_session,
        "batch_fingerprints_match": batch_fingerprints_match,
        "each_input_in_exactly_one_run": each_input_once,
        "duplicate_input_memberships": duplicate_input_memberships,
        "no_extra_runs": no_extra_runs,
        "one_dispatch_job_per_run": one_job_per_run,
        "unique_run_ids": len(run_by_id),
        "batch_widths": sorted(batch_widths),
        "single_input_protocol": not production_batches,
    }


def _measure_database(
    data_dir: Path, expected_unique: int, session_ids: list[str], *,
    production_batches: bool = False, timeout: float = 120,
) -> dict[str, Any]:
    db_path = data_dir / "lka.sqlite3"
    deadline = time.monotonic() + timeout
    last = {"inputs": 0, "completed": 0}
    while time.monotonic() < deadline:
        try:
            with closing(_readonly_db(db_path)) as conn:
                totals = conn.execute(
                    "SELECT COUNT(*) AS inputs, "
                    "SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS completed "
                    "FROM session_inputs i LEFT JOIN agent_runs r ON r.run_id=i.run_id"
                ).fetchone()
                last = {"inputs": totals["inputs"], "completed": totals["completed"] or 0}
                active_dispatches = conn.execute(
                    "SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch' "
                    "AND status IN ('queued','running','retry_wait')"
                ).fetchone()[0]
                last["active_dispatches"] = active_dispatches
                if (
                    last["inputs"] == expected_unique
                    and last["completed"] >= expected_unique
                    and active_dispatches == 0
                ):
                    rows = conn.execute(
                        "SELECT i.input_id,i.session_id,i.client_message_id,i.sequence,i.created_at,i.run_id,"
                        "i.message_id,json_extract(i.request_json,'$.user_input') AS user_input,"
                        "r.status,r.created_at AS run_created,r.record_payload,"
                        "json_extract(r.record_payload,'$.completed_at') AS run_completed "
                        "FROM session_inputs i LEFT JOIN agent_runs r ON r.run_id=i.run_id "
                        "WHERE i.run_id IS NOT NULL ORDER BY i.session_id,i.sequence"
                    ).fetchall()
                else:
                    rows = []
        except sqlite3.OperationalError:
            time.sleep(0.1)
            continue
        if rows:
            break
        time.sleep(0.1)
    else:
        raise TimeoutError(f"benchmark drain timed out; last counts={last}")

    input_records = [dict(row) for row in rows]
    placeholders = ",".join("?" for _ in session_ids)
    with closing(_readonly_db(db_path)) as conn:
        run_records = []
        if session_ids:
            for row in conn.execute(
                f"SELECT run_id,session_id,status,record_payload FROM agent_runs WHERE session_id IN ({placeholders})",
                session_ids,
            ):
                payload = json.loads(row["record_payload"])
                if payload.get("parent_run_id") is None:
                    run_records.append({**dict(row), "record_payload": payload})
        dispatch_jobs = [
            {"status": row["status"], "payload": json.loads(row["payload_json"])}
            for row in conn.execute(
                "SELECT status,payload_json FROM background_jobs WHERE kind='agent_dispatch'"
            )
        ]
    batch_audit = _audit_batch_membership(
        input_records, run_records, dispatch_jobs, production_batches=production_batches,
    )
    bind_delays: list[float] = []
    previous_completion: dict[str, datetime] = {}
    primary_rows = {}
    for row in rows:
        primary_rows.setdefault(row["run_id"], row)
    for row in sorted(primary_rows.values(), key=lambda item: (item["session_id"], item["sequence"])):
        accepted = datetime.fromisoformat(row["created_at"])
        prior = previous_completion.get(row["session_id"])
        eligible = max(accepted, prior) if prior else accepted
        bound = datetime.fromisoformat(row["run_created"])
        bind_delays.append(max(0.0, (bound - eligible).total_seconds() * 1000))
        completed = row["run_completed"]
        if completed:
            previous_completion[row["session_id"]] = datetime.fromisoformat(completed)
    with closing(_readonly_db(db_path)) as conn:
        pending_inputs = conn.execute(
            "SELECT COUNT(*) FROM session_inputs WHERE run_id IS NULL"
        ).fetchone()[0]
        dispatch_states = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM background_jobs WHERE kind='agent_dispatch' GROUP BY status"
            )
        }
    return {
        "persisted_inputs": len(rows),
        "unique_run_ids": batch_audit["unique_run_ids"],
        "duplicate_run_bindings": batch_audit["duplicate_input_memberships"],
        "batch_audit": batch_audit,
        "all_completed": (
            len(rows) == expected_unique
            and last["inputs"] == expected_unique
            and pending_inputs == 0
            and all(row["status"] == "completed" for row in rows)
        ),
        "queue_metrics": {
            "unbound_inputs_after_drain": pending_inputs,
            "dispatch_jobs_by_status": dispatch_states,
        },
        "eligible_to_bind_ms": {
            "definition": "only the primary (lowest-sequence) input per run: max(primary.created_at, prior same-session run.completed_at) to agent_runs.created_at; later batch members are excluded because acceptance can follow run creation",
            "samples": len(bind_delays),
            "p50": _percentile(bind_delays, 0.50),
            "p95": _percentile(bind_delays, 0.95),
            "p99": _percentile(bind_delays, 0.99),
        },
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.production_release import fingerprint, release_id

    source_identity = release_id(fingerprint(ROOT))
    output = validate_output_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    server_log = output.with_name(output.stem + ".server.log")
    process = None
    started_at = datetime.now().astimezone().isoformat()
    try:
        with (
            tempfile.TemporaryDirectory(prefix="lka-event-benchmark-") as temp,
            server_log.open("w", encoding="utf-8") as log,
        ):
            data_dir = Path(temp) / "data"
            data_dir.mkdir()
            port = _free_port()
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve",
                "--data-dir",
                str(data_dir),
                "--port",
                str(port),
                "--mode",
                args.mode,
            ]
            if args.production_batches:
                command.append("--production-batches")
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=ROOT,
                env=_clean_child_env(data_dir, args.mode),
                stdout=log,
                stderr=log,
            )
            base_url = f"http://127.0.0.1:{port}"
            request_latencies: list[float] = []
            responses: list[dict[str, Any]] = []
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30),
                    limits=httpx.Limits(
                        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
                    ),
                ) as client:
                    ready_deadline = time.monotonic() + 45
                    while time.monotonic() < ready_deadline:
                        if process.returncode is not None:
                            raise RuntimeError(
                                f"benchmark server exited with code {process.returncode}"
                            )
                        try:
                            response = await client.get(base_url + "/health")
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.1)
                    else:
                        raise TimeoutError("isolated benchmark server did not become ready")

                    labels = [f"benchmark-session-{index:04d}" for index in range(args.sessions)]
                    attempts = build_attempts(
                        labels,
                        args.requests,
                        seed=args.seed,
                        retry_fraction=args.retry_fraction,
                        text_bytes=args.text_bytes,
                    )
                    start = time.monotonic()
                    semaphore = asyncio.Semaphore(args.concurrency)

                    async def send(index: int, body: dict[str, Any], session_id: str):
                        target = start + index / args.rate
                        delay = target - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        async with semaphore:
                            sent = time.monotonic()
                            try:
                                response = await client.post(
                                    f"{base_url}/sessions/{session_id}/inputs",
                                    json={
                                        key: value
                                        for key, value in body.items()
                                        if key not in {"session_id", "session_label"}
                                    },
                                )
                                elapsed = (time.monotonic() - sent) * 1000
                                request_latencies.append(elapsed)
                                parsed = response.json()
                                responses.append(
                                    {
                                        "status": response.status_code,
                                        "input_id": parsed.get("input", {}).get("input_id"),
                                        "client_message_id": body["client_message_id"],
                                        "session_id": session_id,
                                        "detail": parsed.get("detail"),
                                    }
                                )
                            except (httpx.HTTPError, ValueError) as exc:
                                request_latencies.append((time.monotonic() - sent) * 1000)
                                responses.append(
                                    {
                                        "status": "error",
                                        "error": type(exc).__name__,
                                        "client_message_id": body["client_message_id"],
                                        "session_id": session_id,
                                    }
                                )

                    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for attempt in attempts:
                        grouped[attempt["session_id"]].append(attempt)
                    sessions: list[str] = []
                    attempt_index = 0
                    for session_index, label in enumerate(labels):
                        manager = await client.post(
                            base_url + "/managers",
                            json={
                                "title": f"Benchmark {args.seed} {session_index:03d}",
                                "make_default": True,
                            },
                        )
                        manager.raise_for_status()
                        session_id = manager.json()["session"]["session_id"]
                        sessions.append(session_id)
                        batch = grouped.get(label, [])
                        await asyncio.gather(
                            *(
                                send(attempt_index + offset, body, session_id)
                                for offset, body in enumerate(batch)
                            )
                        )
                        attempt_index += len(batch)
                    elapsed_seconds = time.monotonic() - start
                acceptance = Counter(str(item["status"]) for item in responses)
                receipts: dict[tuple[str, str], set[str]] = defaultdict(set)
                accepted_keys: set[tuple[str, str]] = set()
                for item in responses:
                    if item.get("input_id"):
                        receipts[(item["session_id"], item["client_message_id"])].add(
                            item["input_id"]
                        )
                        if item["status"] == 202:
                            accepted_keys.add((item["session_id"], item["client_message_id"]))
                expected_unique = len(accepted_keys)
                expected_unique_attempts = max(
                    1,
                    args.requests
                    - min(args.requests - 1, round(args.requests * args.retry_fraction)),
                )
                database = _measure_database(
                    data_dir, expected_unique, sessions, production_batches=args.production_batches,
                )
                result = {
                    "source_release_id": source_identity,
                    "source_unchanged": release_id(fingerprint(ROOT)) == source_identity,
                    "environment": {
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                        "cpu_count": os.cpu_count(),
                        "logical_memory_bytes": _memory_bytes(),
                    },
                    "mode": args.mode,
                    "configuration": {
                        "seed": args.seed,
                        "sessions": args.sessions,
                        "session_admission": "create each manager as default, submit its phase, then advance default; phases do not overlap HTTP submissions",
                        "concurrency": args.concurrency,
                        "target_rate_rps": args.rate,
                        "attempts": args.requests,
                        "text_bytes": args.text_bytes,
                        "retry_fraction": args.retry_fraction,
                        "execution_stub_ms": 50,
                        "input_run_protocol": (
                            "production-batches" if args.production_batches else "single-input-worst-case"
                        ),
                        "production_freeze_enabled": args.production_batches,
                    },
                    "observed": {
                        "elapsed_seconds": round(elapsed_seconds, 3),
                        "actual_rate_rps": round(len(responses) / elapsed_seconds, 3),
                        "status_counts": dict(acceptance),
                        "request_latency_ms": {
                            "samples": len(request_latencies),
                            "p50": _percentile(request_latencies, 0.50),
                            "p95": _percentile(request_latencies, 0.95),
                            "p99": _percentile(request_latencies, 0.99),
                        },
                        "receipt_idempotency_conflicts": sum(
                            len(ids) > 1 for ids in receipts.values()
                        ),
                        "receipt_keys_with_response": len(receipts),
                        "expected_unique_keys": expected_unique_attempts,
                        "all_unique_keys_accepted": expected_unique == expected_unique_attempts,
                    },
                    "database_correctness": database,
                    "passed": (
                        all(item["status"] == 202 for item in responses)
                        and expected_unique == expected_unique_attempts
                        and all(len(ids) == 1 for ids in receipts.values())
                        and database["all_completed"]
                        and database["duplicate_run_bindings"] == 0
                        and database["batch_audit"]["passed"]
                    ),
                    "quality_boundary": "50ms local execution stub; no provider, model, tool or real configuration. Results describe isolated HTTP/SQLite scheduling only.",
                }
                latency = database["eligible_to_bind_ms"]
                result["latency_targets"] = {
                    "bind_p95_below_100ms": latency["p95"] is not None and latency["p95"] < 100,
                    "bind_p99_below_500ms": latency["p99"] is not None and latency["p99"] < 500,
                }
                result["correctness_passed"] = result["passed"]
                result["passed"] = (
                    result["correctness_passed"]
                    and result["source_unchanged"]
                    and all(result["latency_targets"].values())
                )
                output.write_text(
                    json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                return result
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=15)
                    except TimeoutError:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=5)
    except Exception as exc:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=15)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
        try:
            log_tail = server_log.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        except OSError:
            log_tail = []
        output.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "passed": False,
                    "started_at": started_at,
                    "mode": args.mode,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                    "server_log": str(server_log),
                    "server_log_tail": log_tail,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        raise


def _memory_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("poll", "event", "shadow"), default="event")
    parser.add_argument("--sessions", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--rate", type=float, default=20)
    parser.add_argument("--requests", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--text-bytes", type=int, default=DEFAULT_TEXT_BYTES)
    parser.add_argument("--retry-fraction", type=float, default=0.1)
    parser.add_argument(
        "--production-batches", action="store_true",
        help="opt in to runtime freeze_for_dispatch batching; default remains single-input worst case",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.serve:
        if args.data_dir is None or args.port is None:
            raise SystemExit("internal server requires --data-dir and --port")
        _serve(args.data_dir, args.port, args.mode, args.production_batches)
        return 0
    if args.output is None:
        raise SystemExit("--output is required")
    if args.sessions < 1 or args.concurrency < 1 or args.rate <= 0 or args.requests < 1:
        raise SystemExit("sessions, concurrency, requests, and rate must be positive")
    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
