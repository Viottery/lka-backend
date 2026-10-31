"""Bounded synthetic MySQL job claim/finish benchmark; no LLM or tool calls."""
from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from app.core.background_jobs import BackgroundJobStore
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.db import init_db
from app.storage.mysql import MySQLConfig, close_mysql_pools


def _percentile(samples: list[float], percentile: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _schema_ready(conn) -> bool:
    count = conn.execute_native(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
        "AND table_name IN ('background_jobs','background_job_scopes')",
    ).fetchone()[0]
    return count == 2


def _cleanup(identity, kind: str, scopes: list[str]) -> int:
    if not scopes:
        return 0
    marks = ",".join("?" for _ in scopes)
    deleted = 0
    with connect_database(identity) as conn:
        conn.execute("BEGIN")
        for table in ("background_job_inputs", "background_pending_inputs",
                      "background_pending_watermarks", "background_watermark_heads"):
            conn.execute(f"DELETE FROM {table} WHERE kind=? AND scope_id IN ({marks})", (kind, *scopes))
        deleted = conn.execute(
            f"DELETE FROM background_jobs WHERE kind=? AND scope_id IN ({marks})", (kind, *scopes),
        ).rowcount
        conn.execute(
            f"DELETE FROM background_job_scopes WHERE kind=? AND scope_id IN ({marks})", (kind, *scopes),
        )
    return deleted


def _phase(store: BackgroundJobStore, identity, *, run_id: str, jobs: int, workers: int,
           task_ms: int) -> dict:
    kind = f"test.mysql.benchmark.{run_id}.{workers}"
    scopes = [f"{run_id}.{workers}.scope-{index:04d}" for index in range(jobs)]
    expected_ids: set[str] = set()
    started = time.perf_counter()
    result = None
    try:
        for index, scope in enumerate(scopes):
            item = store.enqueue(
                kind, scope, f"item-{index:04d}",
                {"run_id": run_id, "ordinal_id": f"item-{index:04d}"}, max_attempts=1,
            )
            expected_ids.add(item["job_id"])
        with connect_database(identity) as conn:
            if not _schema_ready(conn):
                raise RuntimeError("MySQL background-job schema is not installed")

        claim_latencies: list[float] = []
        claimed_counts: dict[str, int] = {}
        claim_attempts = 0
        empty_claims = 0
        completed = 0
        errors: list[str] = []
        condition = threading.Condition()
        abort = threading.Event()
        deadline = started + max(30.0, jobs * max(task_ms, 1) / 1000 * 4 + 10)

        def worker():
            nonlocal claim_attempts, empty_claims, completed
            while not abort.is_set():
                if time.perf_counter() >= deadline:
                    with condition:
                        errors.append("phase_timeout")
                        abort.set()
                        condition.notify_all()
                    return
                claim_started = time.perf_counter()
                with condition:
                    claim_attempts += 1
                try:
                    item = store.claim(
                        f"mysql-benchmark-{run_id}-{workers}-{threading.get_ident()}",
                        30, kinds=(kind,),
                    )
                except Exception as exc:  # noqa: BLE001 - report only the class, never credentials.
                    with condition:
                        errors.append(type(exc).__name__)
                        abort.set()
                        condition.notify_all()
                    return
                claim_elapsed_ms = (time.perf_counter() - claim_started) * 1000
                if item is None:
                    with condition:
                        empty_claims += 1
                        if completed >= jobs or abort.is_set():
                            return
                        # Workers with no ready row sleep until a peer finishes;
                        # the timeout only recovers from a missed notification.
                        condition.wait(timeout=1.0)
                    continue

                job_id = item["job_id"]
                with condition:
                    claimed_counts[job_id] = claimed_counts.get(job_id, 0) + 1
                    claim_latencies.append(claim_elapsed_ms)
                # The simulated execution delay intentionally happens after
                # claim's transaction and before the separate finish transaction.
                if task_ms:
                    time.sleep(task_ms / 1000)
                try:
                    if not store.complete(job_id, item["lease_owner"], item["lease_epoch"]):
                        with condition:
                            errors.append("finish_fenced")
                            abort.set()
                            condition.notify_all()
                        return
                except Exception as exc:  # noqa: BLE001 - do not echo exception payloads.
                    with condition:
                        errors.append(type(exc).__name__)
                        abort.set()
                        condition.notify_all()
                    return
                with condition:
                    completed += 1
                    condition.notify_all()

        processing_started = time.perf_counter()
        cpu_started = time.process_time()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mysql-bench") as pool:
            futures = [pool.submit(worker) for _ in range(workers)]
            for future in futures:
                future.result()
        elapsed = time.perf_counter() - processing_started
        cpu_seconds = time.process_time() - cpu_started
        duplicate_claims = sum(count - 1 for count in claimed_counts.values() if count > 1)
        missing = len(expected_ids - claimed_counts.keys())
        result = {
            "workers": workers,
            "jobs_enqueued": len(expected_ids),
            "jobs_completed": completed,
            "missing_jobs": missing,
            "duplicate_claims": duplicate_claims,
            "errors": errors,
            "claim_attempts": claim_attempts,
            "empty_claim_polls": empty_claims,
            "processing_elapsed_seconds": round(elapsed, 4),
            "process_cpu_seconds": round(cpu_seconds, 4),
            "process_cpu_percent_of_one_core": round(cpu_seconds / elapsed * 100, 2) if elapsed else None,
            "completed_jobs_per_second": round(completed / elapsed, 3) if elapsed else None,
            "successful_claim_latency_ms_p50": _percentile(claim_latencies, 0.50),
            "successful_claim_latency_ms_p95": _percentile(claim_latencies, 0.95),
        }
        return result
    except Exception as exc:  # noqa: BLE001 - keep a credential-free phase failure.
        result = {"workers": workers, "jobs_enqueued": len(expected_ids), "jobs_completed": 0,
                  "missing_jobs": jobs, "duplicate_claims": 0, "errors": [type(exc).__name__],
                  "claim_attempts": 0, "empty_claim_polls": 0, "processing_elapsed_seconds": None,
                  "process_cpu_seconds": None, "process_cpu_percent_of_one_core": None,
                  "completed_jobs_per_second": None, "successful_claim_latency_ms_p50": None,
                  "successful_claim_latency_ms_p95": None}
        return result
    finally:
        deleted = _cleanup(identity, kind, scopes)
        if result is not None:
            result["cleanup_deleted_jobs"] = deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True,
                        help="Private JSON with lab/test MySQL credentials; never printed")
    parser.add_argument("--jobs", type=int, default=64)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--task-ms", type=int, default=50,
                        help="Synthetic delay outside DB transactions (default: 50 ms)")
    parser.add_argument("--max-jobs", type=int, default=1000)
    parser.add_argument("--output", type=Path,
                        help="Optional local JSON report path; may be placed under ignored docs/_engineering/")
    args = parser.parse_args()
    if not 1 <= args.max_jobs <= 1000 or not 1 <= args.jobs <= args.max_jobs:
        parser.error("--jobs must be between 1 and --max-jobs, which is bounded to 1000")
    if not args.workers or any(not 1 <= count <= 32 for count in args.workers):
        parser.error("--workers values must be between 1 and 32")
    if len(set(args.workers)) != len(args.workers):
        parser.error("--workers values must be unique")
    if not 0 <= args.task_ms <= 1000:
        parser.error("--task-ms must be between 0 and 1000")

    config_path = args.connection_file.expanduser().resolve(strict=True)
    config = MySQLConfig(**json.loads(config_path.read_text(encoding="utf-8")))
    if re.search(r"(?<![a-z0-9])(?:test|lab)(?![a-z0-9])", config.database.casefold()) is None:
        parser.error("refusing a database without an explicit test/lab name")
    identity = Path(tempfile.gettempdir()) / f"lka-mysql-bench-{uuid4().hex}.sqlite3"
    configure_mysql(identity, config)
    store = BackgroundJobStore(identity)
    report = {
        "benchmark": "mysql_background_job_claim_finish_synthetic",
        "classification": "storage worker microbenchmark; not API throughput or production capacity",
        "mysql_pool_size": config.pool_size,
        "configured_max_workers": max(args.workers),
        "jobs_per_phase": args.jobs,
        "synthetic_task_delay_ms_outside_transaction": args.task_ms,
        "phases": [],
    }
    run_id = uuid4().hex
    try:
        # Validate immutable migration versions/checksums once before timing;
        # normal runtime startup establishes the same connection-router cache.
        init_db(identity)
        with connect_database(identity) as conn:
            if not _schema_ready(conn):
                raise SystemExit("MySQL business schema is not installed")
        for workers in args.workers:
            report["phases"].append(_phase(store, identity, run_id=run_id, jobs=args.jobs,
                                           workers=workers, task_ms=args.task_ms))
    finally:
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        try:
            output.chmod(0o600)
        except OSError:
            pass
    return int(any(phase["errors"] or phase["jobs_completed"] != args.jobs
                   or phase["duplicate_claims"] for phase in report["phases"]))


if __name__ == "__main__":
    raise SystemExit(main())
