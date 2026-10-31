"""Bounded synthetic MySQL queue/storage stress run; no API, LLM, or tools."""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import tempfile
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.background_jobs import BackgroundJobStore
from app.storage.connections import configure_mysql, connect_database, unregister_mysql
from app.storage.db import init_db
from app.storage.mysql import (
    MySQLConfig,
    MySQLPoolTimeout,
    close_mysql_pools,
)
from app.storage.mysql_owner import MySQLCoordinatorOwner

POOL_SIZE = 8
JOBS_PER_WORKER_PHASE = 300
HOT_KEY_JOBS = 300
MIXED_JOBS = 300
HISTORY_JOBS = 10_000
GLOBAL_DEADLINE_SECONDS = 8 * 60
PHASE_DEADLINE_SECONDS = 120
MAX_SYNTHETIC_JOBS = 20_000


class SQLCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    @staticmethod
    def _kind(sql: str) -> str:
        normalized = sql.lstrip().split(None, 1)
        return normalized[0].upper() if normalized else "EMPTY"

    def add(self, sql: str) -> None:
        kind = self._kind(sql)
        with self._lock:
            self._counts[kind] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))


class CountedConnection:
    """Transparent connection proxy counting SQL sent through the real driver."""

    def __init__(self, connection, counter: SQLCounter) -> None:
        self._connection = connection
        self._counter = counter

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._connection.row_factory = value

    def execute(self, sql, parameters=()):
        self._counter.add(sql)
        return self._connection.execute(sql, parameters)

    def execute_native(self, sql, parameters=()):
        self._counter.add(sql)
        return self._connection.execute_native(sql, parameters)

    def executemany(self, sql, parameters):
        self._counter.add(sql)
        return self._connection.executemany(sql, parameters)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._connection.__exit__(exc_type, exc, traceback)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _percentile(samples: list[float], percentile: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _stats(samples: list[float]) -> dict[str, float | None]:
    return {
        "p50_ms": _percentile(samples, 0.50),
        "p95_ms": _percentile(samples, 0.95),
        "p99_ms": _percentile(samples, 0.99),
    }


def _cleanup_kind(identity, kind: str, scopes: list[str], *, remove_failed: bool = False) -> dict:
    """Delete only this run's successful rows; retain failed/nonterminal evidence."""
    deleted = 0
    preserved = 0
    with connect_database(identity) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        marks = ",".join("?" for _ in scopes)
        if scopes:
            rows = conn.execute(
                f"SELECT job_id,status FROM background_jobs WHERE kind=? AND scope_id IN ({marks})",
                (kind, *scopes),
            ).fetchall()
            preserved = sum(1 for row in rows if row["status"] != "succeeded")
            if remove_failed:
                deleted += conn.execute(
                    f"DELETE FROM background_jobs WHERE kind=? AND scope_id IN ({marks})",
                    (kind, *scopes),
                ).rowcount
                preserved = 0
            else:
                for table in ("background_job_inputs", "background_pending_inputs",
                              "background_pending_watermarks", "background_watermark_heads"):
                    conn.execute(f"DELETE FROM {table} WHERE kind=? AND scope_id IN ({marks})", (kind, *scopes))
                deleted = conn.execute(
                    f"DELETE FROM background_jobs WHERE kind=? AND scope_id IN ({marks}) AND status='succeeded'",
                    (kind, *scopes),
                ).rowcount
                conn.execute(
                    f"DELETE FROM background_job_scopes WHERE kind=? AND scope_id IN ({marks}) "
                    "AND NOT EXISTS (SELECT 1 FROM background_jobs j WHERE j.kind=? "
                    "AND j.scope_id=background_job_scopes.scope_id)",
                    (kind, *scopes, kind),
                )
        conn.commit()
    return {"deleted_successful_jobs": deleted, "preserved_non_success_jobs": preserved}


def _enqueue(store: BackgroundJobStore, kind: str, scopes: list[str], run_id: str) -> list[str]:
    job_ids = []
    for ordinal, scope in enumerate(scopes):
        record = store.enqueue(
            kind,
            scope,
            f"{run_id}-{ordinal:05d}",
            {"run_id": run_id, "ordinal_id": f"task-{ordinal:05d}"},
            max_attempts=1,
        )
        job_ids.append(record["job_id"])
    return job_ids


def _run_queue_phase(
    *,
    store: BackgroundJobStore,
    identity,
    counter: SQLCounter,
    run_id: str,
    phase_name: str,
    kind: str,
    scopes: list[str],
    workers: int,
    deadline_at: float,
    simulated_work_ms: int = 2,
    reader_workers: int = 0,
) -> dict:
    expected_job_ids = set(_enqueue(store, kind, scopes, run_id))
    phase_started = time.perf_counter()
    lock = threading.Lock()
    abort = threading.Event()
    condition = threading.Condition(lock)
    claimed_ids: Counter[str] = Counter()
    current_scope: set[str] = set()
    overlap_scopes: set[str] = set()
    claim_ms: list[float] = []
    complete_ms: list[float] = []
    cycle_ms: list[float] = []
    errors: Counter[str] = Counter()
    completed_ids: set[str] = set()
    claim_attempts = 0
    empty_claims = 0
    reader_queries = 0
    reader_ms: list[float] = []
    total_jobs = len(expected_job_ids)
    expire = min(deadline_at, phase_started + PHASE_DEADLINE_SECONDS)

    def worker(worker_index: int) -> None:
        nonlocal claim_attempts, empty_claims
        owner_name = f"stress-{run_id[:10]}-{phase_name}-{worker_index}"
        while not abort.is_set():
            if time.perf_counter() >= expire:
                with condition:
                    errors["phase_deadline"] += 1
                    abort.set()
                    condition.notify_all()
                return
            started = time.perf_counter()
            with condition:
                claim_attempts += 1
            try:
                item = store.claim(owner_name, 30, kinds=(kind,))
            except Exception as exc:  # noqa: BLE001 - report safe type only.
                with condition:
                    errors[type(exc).__name__] += 1
                    abort.set()
                    condition.notify_all()
                return
            claim_elapsed = (time.perf_counter() - started) * 1000
            if item is None:
                with condition:
                    empty_claims += 1
                    if len(completed_ids) >= total_jobs or abort.is_set():
                        return
                    condition.wait(timeout=0.02)
                continue

            job_id = item["job_id"]
            scope = item["scope_id"]
            with condition:
                claimed_ids[job_id] += 1
                claim_ms.append(claim_elapsed)
                if scope in current_scope:
                    overlap_scopes.add(scope)
                    errors["same_scope_overlap"] += 1
                    abort.set()
                    condition.notify_all()
                    return
                current_scope.add(scope)
            if simulated_work_ms:
                time.sleep(simulated_work_ms / 1000)
            finish_started = time.perf_counter()
            # A claim becomes available to the next same-scope worker only
            # when complete() commits. Remove the simulated active marker
            # immediately before that linearization point, avoiding a false
            # overlap if the next claim returns before complete() returns.
            with condition:
                current_scope.discard(scope)
            try:
                succeeded = store.complete(job_id, item["lease_owner"], item["lease_epoch"])
                finish_elapsed = (time.perf_counter() - finish_started) * 1000
                with condition:
                    complete_ms.append(finish_elapsed)
                    cycle_ms.append((time.perf_counter() - started) * 1000)
                    if not succeeded:
                        errors["finish_fenced"] += 1
                        abort.set()
                    else:
                        completed_ids.add(job_id)
                    condition.notify_all()
            except Exception as exc:  # noqa: BLE001 - no retry or message leakage.
                with condition:
                    current_scope.discard(scope)
                    errors[type(exc).__name__] += 1
                    abort.set()
                    condition.notify_all()
                return

    reader_stop = threading.Event()

    def reader(reader_index: int) -> None:
        nonlocal reader_queries
        while not reader_stop.is_set() and not abort.is_set() and time.perf_counter() < expire:
            try:
                query_started = time.perf_counter()
                with connect_database(identity) as raw:
                    conn = CountedConnection(raw, counter)
                    row = conn.execute(
                        "SELECT COUNT(*) FROM background_jobs WHERE kind=? AND status='succeeded'",
                        (kind,),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("reader returned no aggregate row")
                with condition:
                    reader_queries += 1
                    reader_ms.append((time.perf_counter() - query_started) * 1000)
            except Exception as exc:  # noqa: BLE001 - report safe type only.
                with condition:
                    errors[f"reader_{type(exc).__name__}"] += 1
                    abort.set()
                    condition.notify_all()
                return
            # Avoid converting this storage-focused phase into a client spin test.
            time.sleep(0.005 + (reader_index % 3) * 0.001)

    phase_start_sql = counter.snapshot()
    processing_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers + reader_workers, thread_name_prefix="mysql-stress") as pool:
        futures = [pool.submit(worker, index) for index in range(workers)]
        reader_futures = [pool.submit(reader, index) for index in range(reader_workers)]
        for future in futures:
            future.result()
        reader_stop.set()
        for future in reader_futures:
            future.result()
    elapsed = time.perf_counter() - processing_started
    duplicate_claims = sum(count - 1 for count in claimed_ids.values() if count > 1)
    missing = len(expected_job_ids - completed_ids)
    after_sql = counter.snapshot()
    phase_sql = {
        key: after_sql.get(key, 0) - phase_start_sql.get(key, 0)
        for key in sorted(after_sql.keys() | phase_start_sql.keys())
        if after_sql.get(key, 0) != phase_start_sql.get(key, 0)
    }
    cleanup = _cleanup_kind(identity, kind, scopes)
    result = {
        "phase": phase_name,
        "workers": workers,
        "reader_workers": reader_workers,
        "simulated_work_ms_outside_transaction": simulated_work_ms,
        "jobs_enqueued": len(expected_job_ids),
        "jobs_completed": len(completed_ids),
        "missing_jobs": missing,
        "duplicate_claims": duplicate_claims,
        "same_scope_overlap_count": len(overlap_scopes),
        "errors": dict(sorted(errors.items())),
        "claim_attempts": claim_attempts,
        "empty_claim_polls": empty_claims,
        "reader_queries": reader_queries,
        "reader_queries_per_second": round(reader_queries / elapsed, 3) if elapsed else None,
        "reader_latency_ms": _stats(reader_ms),
        "elapsed_seconds": round(elapsed, 4),
        "throughput_jobs_per_second": round(len(completed_ids) / elapsed, 3) if elapsed else None,
        "claim_latency_ms": _stats(claim_ms),
        "complete_latency_ms": _stats(complete_ms),
        "full_claim_to_complete_cycle_latency_ms": _stats(cycle_ms),
        "sql_counts": phase_sql,
        "cleanup": cleanup,
        "expected_pool_timeouts": 0,
    }
    result["pass"] = (
        not result["errors"] and not result["missing_jobs"] and not result["duplicate_claims"]
        and not result["same_scope_overlap_count"] and len(completed_ids) == len(expected_job_ids)
    )
    return result


def _pool_saturation(identity, config: MySQLConfig, owner: MySQLCoordinatorOwner,
                     deadline_at: float) -> dict:
    before = time.perf_counter()
    held = []
    errors: Counter[str] = Counter()
    timeout_ms: list[float] = []
    wait_config = replace(config, pool_wait_seconds=0.2)
    from app.storage.connections import unregister_mysql

    unregister_mysql(identity, expected=config)
    configure_mysql(identity, wait_config, transaction_guard=owner.guard_transaction)
    try:
        for _ in range(POOL_SIZE):
            conn = connect_database(identity)
            conn.execute("BEGIN")  # each held connection owns its coordinator fence too
            held.append(conn)
        def waiter() -> str:
            if time.perf_counter() >= deadline_at:
                return "global_deadline"
            started = time.perf_counter()
            try:
                conn = connect_database(identity)
                conn.close()
                return "unexpected_connection"
            except MySQLPoolTimeout:
                timeout_ms.append((time.perf_counter() - started) * 1000)
                return "expected_pool_timeout"
            except Exception as exc:  # noqa: BLE001 - classify without details.
                return type(exc).__name__

        with ThreadPoolExecutor(max_workers=16, thread_name_prefix="mysql-pool-waiter") as pool:
            outcomes = list(pool.map(lambda _index: waiter(), range(16)))
        for outcome in outcomes:
            errors[outcome] += 1
        for conn in held:
            conn.rollback()
            conn.close()
        held.clear()
        with connect_database(identity) as probe:
            probe.execute("SELECT 1").fetchone()
        return {
            "phase": "pool_saturation",
            "pool_size": POOL_SIZE,
            "connections_held": POOL_SIZE,
            "waiters": 16,
            "wait_deadline_ms": 200,
            "outcomes": dict(sorted(errors.items())),
            "expected_pool_timeouts": errors["expected_pool_timeout"],
            "unexpected_successes": errors["unexpected_connection"],
            "timeout_latency_ms": _stats(timeout_ms),
            "recovery_probe": "passed",
            "elapsed_seconds": round(time.perf_counter() - before, 4),
            "pass": errors["expected_pool_timeout"] == 16 and errors["unexpected_connection"] == 0,
        }
    finally:
        for conn in held:
            conn.close()
        unregister_mysql(identity, expected=wait_config)
        configure_mysql(identity, config, transaction_guard=owner.guard_transaction)
        close_mysql_pools(wait_config)


def _seed_history(identity, run_id: str, rows: int) -> tuple[str, int]:
    kind = f"stress.mysql.history.{run_id}"
    stamp = datetime.now(UTC).isoformat(timespec="microseconds")
    batch = []
    for index in range(rows):
        scope = f"{run_id}.history.{index:05d}"
        batch.append((
            f"{run_id}-history-{index:05d}", kind, scope, f"hist-{index:05d}", "{}", "succeeded",
            0, stamp, 1, 1, stamp, stamp, stamp, stamp,
        ))
    with connect_database(identity) as conn:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO background_jobs(job_id,kind,scope_id,idempotency_key,payload_json,status,priority,"
            "available_at,max_attempts,attempts,started_at,finished_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
    return kind, rows


def _cleanup_history(identity, run_id: str) -> int:
    kind = f"stress.mysql.history.{run_id}"
    with connect_database(identity) as conn:
        conn.execute("BEGIN")
        count = conn.execute("DELETE FROM background_jobs WHERE kind=?", (kind,)).rowcount
        conn.execute("DELETE FROM background_job_scopes WHERE kind=?", (kind,))
        return count


def _load_config(filename: Path) -> MySQLConfig:
    return MySQLConfig(**json.loads(filename.expanduser().resolve(strict=True).read_text(encoding="utf-8")))


def _progress(message: str) -> None:
    print(f"[stress-mysql] {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True,
                        help="Private isolated stress DB credentials; never printed")
    parser.add_argument("--output", type=Path, default=Path(
        "docs/_engineering/reports/mysql-storage-stress-20261029.json"))
    args = parser.parse_args()
    config = _load_config(args.connection_file)
    if re.search(r"(?:^|[_-])stress(?:$|[_-])", config.database.casefold()) is None:
        parser.error("refusing a database name without a distinct stress segment")
    if config.pool_size != POOL_SIZE:
        parser.error(f"stress tests require pool_size={POOL_SIZE}")
    planned = 5 * JOBS_PER_WORKER_PHASE + HOT_KEY_JOBS * 2 + MIXED_JOBS + HISTORY_JOBS + 2 * 300
    if planned > MAX_SYNTHETIC_JOBS:
        parser.error(f"planned synthetic jobs exceed the hard {MAX_SYNTHETIC_JOBS}-job cap")

    run_id = uuid4().hex
    identity = Path(tempfile.gettempdir()) / f"lka-mysql-stress-{run_id}.sqlite3"
    owner = MySQLCoordinatorOwner(config)
    if not owner.acquire():
        raise SystemExit("stress database coordinator is already owned; refusing parallel run")
    configure_mysql(identity, config, transaction_guard=owner.guard_transaction)
    counter = SQLCounter()
    store = BackgroundJobStore(identity)
    raw_store_connect = store._connect
    store._connect = lambda: CountedConnection(raw_store_connect(), counter)
    started_at = datetime.now(UTC)
    deadline_at = time.perf_counter() + GLOBAL_DEADLINE_SECONDS
    report = {
        "run_id": run_id,
        "database_classification": "dedicated synthetic stress database",
        "database_name": config.database,
        "started_at_utc": started_at.isoformat(),
        "limits": {
            "pool_size": POOL_SIZE,
            "workers": [1, 4, 8, 16, 32],
            "jobs_per_worker_phase": JOBS_PER_WORKER_PHASE,
            "max_synthetic_jobs": MAX_SYNTHETIC_JOBS,
            "max_phase_seconds": PHASE_DEADLINE_SECONDS,
            "max_total_seconds": GLOBAL_DEADLINE_SECONDS,
            "retry_policy": "none; failed writes are recorded and not replayed",
            "sql_counter_scope": (
                "counts application execute/execute_native/executemany calls through instrumented "
                "queue/read connections; excludes driver-internal transaction fences, pool pings, "
                "commit/rollback protocol and server-trigger statements"
            ),
            "phase_sql_counts_scope": (
                "per-phase counts cover claim/complete and reader statements, excluding that phase's "
                "enqueue and cleanup; sql_counts_total covers all instrumented calls"
            ),
        },
        "phases": [],
    }
    all_scopes: list[tuple[str, str, list[str]]] = []
    history_kind = None
    try:
        _progress("register MySQL route, verify V1-V9 schema, install transaction fencing")
        init_db(identity)
        store.ensure_schema()
        with connect_database(identity) as conn:
            version = conn.execute_native(
                "SELECT MAX(version) FROM lka_schema_migrations"
            ).fetchone()[0]
        if int(version) < 9:
            raise RuntimeError(f"stress DB schema is older than V9: {version}")

        for workers in (1, 4, 8, 16, 32):
            phase = f"worker_matrix_w{workers}"
            kind = f"stress.mysql.{phase}.{run_id}"
            scopes = [f"{run_id}.{phase}.scope-{index:05d}" for index in range(JOBS_PER_WORKER_PHASE)]
            _progress(f"start {phase}: {JOBS_PER_WORKER_PHASE} scopes/tasks")
            all_scopes.append((kind, phase, scopes))
            result = _run_queue_phase(
                store=store, identity=identity, counter=counter, run_id=run_id,
                phase_name=phase, kind=kind, scopes=scopes, workers=workers,
                deadline_at=deadline_at,
            )
            report["phases"].append(result)
            _progress(f"finish {phase}: pass={result['pass']} completed={result['jobs_completed']} "
                      f"elapsed={result['elapsed_seconds']}s")
            if not result["pass"] or time.perf_counter() >= deadline_at:
                break

        if len(report["phases"]) == 5 and all(phase["pass"] for phase in report["phases"]):
            for phase, scopes in (
                ("hot_one_scope", [f"{run_id}.hot-one"] * HOT_KEY_JOBS),
                ("hot_four_scopes", [f"{run_id}.hot-four-{index % 4}" for index in range(HOT_KEY_JOBS)]),
            ):
                kind = f"stress.mysql.{phase}.{run_id}"
                _progress(f"start {phase}: {HOT_KEY_JOBS} tasks over {len(set(scopes))} scope(s)")
                all_scopes.append((kind, phase, list(set(scopes))))
                result = _run_queue_phase(
                    store=store, identity=identity, counter=counter, run_id=run_id,
                    phase_name=phase, kind=kind, scopes=scopes, workers=16,
                    deadline_at=deadline_at, simulated_work_ms=3,
                )
                report["phases"].append(result)
                _progress(f"finish {phase}: pass={result['pass']} overlaps={result['same_scope_overlap_count']}")
                if not result["pass"] or time.perf_counter() >= deadline_at:
                    break

        if all(phase["pass"] for phase in report["phases"]) and time.perf_counter() < deadline_at:
            kind = f"stress.mysql.mixed_rw.{run_id}"
            scopes = [f"{run_id}.mixed.scope-{index % 4}" for index in range(MIXED_JOBS)]
            _progress("start mixed read/write: 8 queue writers + 4 SQL readers")
            all_scopes.append((kind, "mixed_read_write", list(set(scopes))))
            result = _run_queue_phase(
                store=store, identity=identity, counter=counter, run_id=run_id,
                phase_name="mixed_read_write", kind=kind, scopes=scopes, workers=8,
                deadline_at=deadline_at, simulated_work_ms=3, reader_workers=4,
            )
            report["phases"].append(result)
            _progress(f"finish mixed read/write: pass={result['pass']} reads={result['reader_queries']}")

        if all(phase["pass"] for phase in report["phases"]) and time.perf_counter() < deadline_at:
            kind = f"stress.mysql.history_comparison.{run_id}"
            baseline_scopes = [f"{run_id}.histbase.scope-{i:05d}" for i in range(300)]
            _progress("start 8-worker claim/complete baseline without historical rows")
            all_scopes.append((kind, "history_baseline", baseline_scopes))
            baseline = _run_queue_phase(
                store=store, identity=identity, counter=counter, run_id=run_id,
                phase_name="history_baseline_no_history", kind=kind, scopes=baseline_scopes,
                workers=8, deadline_at=deadline_at, simulated_work_ms=2,
            )
            report["phases"].append(baseline)
            if baseline["pass"] and time.perf_counter() < deadline_at:
                history_kind, inserted = _seed_history(identity, run_id, HISTORY_JOBS)
                comparison_scopes = [f"{run_id}.histafter.scope-{i:05d}" for i in range(300)]
                _progress(f"seeded {inserted} terminal history rows; start same 8-worker comparison")
                all_scopes.append((kind, "history_comparison", comparison_scopes))
                after = _run_queue_phase(
                    store=store, identity=identity, counter=counter, run_id=run_id,
                    phase_name="history_comparison_with_10000_history_rows", kind=kind,
                    scopes=comparison_scopes, workers=8, deadline_at=deadline_at,
                    simulated_work_ms=2,
                )
                after["background_terminal_history_rows"] = inserted
                after["history_kind_isolated"] = history_kind
                report["phases"].append(after)

        if all(phase["pass"] for phase in report["phases"]) and time.perf_counter() < deadline_at:
            _progress("start deterministic pool saturation: hold all 8 slots, run 16 bounded waiters")
            report["phases"].append(_pool_saturation(identity, config, owner, deadline_at))

    except Exception as exc:  # noqa: BLE001 - reports omit exception text/credentials.
        report["fatal_error_class"] = type(exc).__name__
        report["fatal_error_location"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-5:]
        ]
        _progress(f"aborted safely after {type(exc).__name__}")
    finally:
        # Each queue phase removes only its successful rows; retain non-success
        # job rows for diagnosis. The terminal-history fixture is fully synthetic
        # and removed by its unique run ID after comparisons.
        if history_kind is not None:
            try:
                report["terminal_history_cleanup_rows"] = _cleanup_history(identity, run_id)
            except Exception as exc:  # noqa: BLE001
                report["history_cleanup_error_class"] = type(exc).__name__
        residual = []
        for kind, phase_name, scopes in all_scopes:
            try:
                cleanup = _cleanup_kind(identity, kind, scopes)
                if cleanup["preserved_non_success_jobs"]:
                    residual.append({"phase": phase_name, **cleanup})
            except Exception as exc:  # noqa: BLE001
                residual.append({"phase": phase_name, "cleanup_error_class": type(exc).__name__})
        report["residual_failure_records"] = residual
        report["sql_counts_total"] = counter.snapshot()
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        report["elapsed_seconds"] = round(time.perf_counter() - (deadline_at - GLOBAL_DEADLINE_SECONDS), 3)
        report["within_global_deadline"] = report["elapsed_seconds"] <= GLOBAL_DEADLINE_SECONDS
        report["all_phases_passed"] = bool(report["phases"]) and all(
            phase.get("pass", False) for phase in report["phases"]
        ) and len(report["phases"]) == 11 and not report.get("fatal_error_class") \
            and not report.get("residual_failure_records") \
            and not report.get("history_cleanup_error_class") \
            and not report.get("phase_cleanup_errors")
        owner.release()
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)

    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report.get("all_phases_passed") and report.get("within_global_deadline") else 1


if __name__ == "__main__":
    raise SystemExit(main())
