#!/usr/bin/env python3
"""Bounded, read-only runtime metrics sampler for systemd timers."""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import logging.handlers
import math
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

MAX_LOG_BYTES = 10 * 1024 * 1024
MAX_LOG_FILES = 4
HISTORY_MAX_BYTES = 10 * 1024 * 1024


def validate_health_url(value: str | None) -> str | None:
    if value is None:
        return None
    p = urlsplit(value)
    if p.port is not None and p.port < 1:
        raise ValueError("health port must be positive")
    if p.scheme != "http" or p.hostname not in {"127.0.0.1", "localhost", "::1"} or p.username or p.password or p.query or p.fragment or p.path != "/health":
        raise ValueError("health URL must be loopback HTTP /health without credentials, query, or fragment")
    return value


def _read_records(path: Path, errors: list[str]):
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > MAX_LOG_BYTES:
                f.seek(size - MAX_LOG_BYTES)
                f.readline()
                data = f.read(MAX_LOG_BYTES + 1)
                errors.append(f"{path.name}:tail_truncated")
            else:
                data = f.read(MAX_LOG_BYTES + 1)
        lines = data.splitlines()
        for line in lines:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    yield item
            except (ValueError, UnicodeDecodeError):
                errors.append(f"{path.name}:invalid_or_partial_line")
    except FileNotFoundError:
        errors.append(f"{path.name}:missing")
    except OSError:
        errors.append(f"{path.name}:unreadable")


def _timestamp(record):
    try:
        d = datetime.fromisoformat(record["timestamp"])
        return d.astimezone(UTC) if d.tzinfo else None
    except (KeyError, ValueError, TypeError):
        return None


def aggregate_logs(data_dir: Path, window_seconds: int, now: datetime | None = None):
    now = now or datetime.now(UTC)
    start = now - timedelta(seconds=window_seconds)
    errors: list[str] = []
    files = [data_dir / "logs" / f"database_timing.jsonl{suffix}" for suffix in ("", ".1", ".2", ".3")]
    existing = [p for p in files if p.exists()]
    if not existing:
        errors.append("timing_logs_missing")
    seen = set()
    sqlite = {}; http = Counter(); accepted = 0; duplicate_count = 0; excluded = Counter()
    intervals = {}
    for path in existing[:MAX_LOG_FILES]:
        for r in _read_records(path, errors):
            if r.get("kind") not in {"sqlite_summary", "http"}:
                continue
            ts = _timestamp(r)
            if ts is None or ts > now:
                excluded["invalid_timestamp"] += 1; continue
            key = json.dumps(r, sort_keys=True, separators=(",", ":"))
            if key in seen:
                duplicate_count += 1; continue
            seen.add(key)
            if r.get("kind") == "sqlite_summary":
                try: interval = _finite_nonnegative(r["interval_seconds"], allow_zero=False)
                except (KeyError, ValueError, TypeError): excluded["invalid_interval"] += 1; continue
                if interval <= 0 or interval > window_seconds or ts - timedelta(seconds=interval) < start or ts < start:
                    excluded["outside_or_partial_window"] += 1; continue
                try:
                    count = _integer(r["count"]); errors_count = _integer(r["errors"])
                    total_ms = _finite_nonnegative(r["total_ms"])
                    max_ms = _finite_nonnegative(r["max_ms"])
                    bounds = r["bucket_bounds_ms"]
                    counts = r["bucket_counts"]
                    if not isinstance(bounds, list) or not isinstance(counts, list) or len(bounds) != len(counts) or not counts:
                        raise ValueError("invalid buckets")
                    bounds = [None if x is None else _finite_nonnegative(x) for x in bounds]
                    if bounds[-1] is not None or any(x is None for x in bounds[:-1]) or any(a >= b for a, b in itertools.pairwise(bounds[:-1])):
                        raise ValueError("invalid bounds")
                    counts = [_integer(x) for x in counts]
                    if sum(counts) != count or errors_count > count:
                        raise ValueError("inconsistent counts")
                except (KeyError, ValueError, TypeError):
                    excluded["invalid_summary"] += 1; continue
                accepted += 1
                group_key = (str(r.get("database", "unknown")), str(r.get("phase", "unknown")))
                group = sqlite.setdefault(group_key,
                                          {"count": 0, "errors": 0, "total_ms": 0.0, "interval_seconds": 0.0,
                                           "max_ms": 0.0,
                                           "bucket_bounds_ms": bounds, "bucket_counts": [0] * len(counts)})
                if group["bucket_bounds_ms"] != bounds:
                    excluded["incompatible_bucket_bounds"] += 1; accepted -= 1; continue
                group["count"] += count; group["errors"] += errors_count; group["total_ms"] += total_ms
                group["max_ms"] = max(group["max_ms"], max_ms)
                group["interval_seconds"] += interval
                intervals.setdefault(group_key, []).append((ts - timedelta(seconds=interval), ts))
                for i, c in enumerate(counts): group["bucket_counts"][i] += c
            elif r.get("kind") == "http" and start <= ts <= now:
                try:
                    status = _integer(r["status"]); elapsed = _finite_nonnegative(r["elapsed_ms"])
                    headers = _finite_nonnegative(r["headers_ms"]) if r.get("headers_ms") is not None else 0
                except (KeyError, ValueError, TypeError): excluded["invalid_http"] += 1; continue
                http["requests"] += 1
                http[f"status_{status // 100}xx"] += 1
                http["elapsed_ms_total"] += elapsed; http["headers_ms_total"] += headers
    for key, group in sqlite.items():
        ordered = sorted(intervals[key])
        # Small timestamp offsets arise while one flush writes its phase rows.
        overlaps = sum((end - begin).total_seconds() > .1
                       for (_, end), (begin, _) in itertools.pairwise(ordered))
        group["overlapping_intervals"] = overlaps
        group["calls_per_second"] = group["count"] / group["interval_seconds"] if group["interval_seconds"] and not overlaps else None
        group["rate_basis"] = "accepted_complete_summary_intervals"
        group["p50_upper_ms"] = _bucket_percentile(group["bucket_counts"], group["bucket_bounds_ms"], .50)
        group["p95_upper_ms"] = _bucket_percentile(group["bucket_counts"], group["bucket_bounds_ms"], .95)
        group["p99_upper_ms"] = _bucket_percentile(group["bucket_counts"], group["bucket_bounds_ms"], .99)
    return {"sqlite": {db: {phase: value for (d, phase), value in sqlite.items() if d == db} for db, _ in sqlite},
            "http_sampled_records": {**dict(http), "is_complete_request_count": False,
                                     "warning": "sampled/biased records; do not interpret as total requests or RPS"},
            "coverage": {"summary_intervals": accepted,
            "sqlite_summary_status": "available" if accepted else "unavailable",
            "deduplicated_records": duplicate_count,
            "excluded_records": dict(excluded), "log_files_considered": len(existing[:MAX_LOG_FILES]),
            "available_log_files": [p.name for p in existing[:MAX_LOG_FILES]],
            "absent_log_files": [p.name for p in files if not p.exists()]}, "errors": errors}


def _finite_nonnegative(value, *, allow_zero=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)): raise TypeError("not a JSON number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("numeric value too large") from exc
    if not math.isfinite(number) or number < 0 or (not allow_zero and number == 0): raise ValueError("invalid numeric value")
    return number


def _integer(value):
    number = _finite_nonnegative(value)
    if not number.is_integer(): raise ValueError("not an integer")
    return int(number)


def _bucket_percentile(counts, bounds, percentile):
    total = sum(counts)
    if not total: return None
    target = total * percentile
    cumulative = 0
    for count, bound in zip(counts, bounds):
        cumulative += count
        if cumulative >= target: return bound
    return None


def database_state(path: Path):
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        deadline = time.monotonic() + 2
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("BEGIN")
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {"agent_runs", "session_inputs", "session_input_queues"}
                missing = sorted(required - tables)
                if missing:
                    return {"status": "unavailable", "reason": "required_tables_missing", "missing_tables": missing}
                runs = {str(k): int(v) for k, v in conn.execute("SELECT status,count(*) FROM agent_runs GROUP BY status")}
                inputs = int(conn.execute("SELECT count(*) FROM session_inputs WHERE run_id IS NULL AND cancelled=0").fetchone()[0])
                queue = {"queues": int(conn.execute("SELECT count(*) FROM session_input_queues").fetchone()[0]),
                         "paused_queues": int(conn.execute("SELECT count(*) FROM session_input_queues WHERE paused=1").fetchone()[0])}
                return {"status": "available", "runs_by_status": runs,
                        "pending_unbound_inputs": inputs, "input_queues": queue}
            finally:
                conn.rollback()
    except (sqlite3.Error, OSError):
        return {"status": "unavailable", "reason": "database_read_failed"}


def health(url):
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        with opener.open(url, timeout=1) as response:
            body = response.read(1025)
            if len(body) > 1024: return {"status": "unavailable", "reason": "body_too_large"}
            try: payload = json.loads(body)
            except (ValueError, UnicodeDecodeError): return {"status": "unavailable", "reason": "invalid_health_response"}
            if (response.status != 200 or not isinstance(payload, dict) or payload.get("status") != "ok"
                    or payload.get("service") != "local-knowledge-agent-os"):
                return {"status": "unavailable", "reason": "unhealthy_response", "http_status": response.status}
            return {"status": "available", "http_status": response.status}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"status": "unavailable"}


def atomic_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True, separators=(",", ":")); f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


class _PrivateHistory(logging.handlers.RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def handleError(self, record):
        raise RuntimeError("metrics_history_write_failed") from None


def append_history(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _PrivateHistory(path, maxBytes=HISTORY_MAX_BYTES, backupCount=3, encoding="utf-8")
    try:
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        handler.emit(logging.LogRecord("lka.metrics", logging.INFO, "", 0, line, (), None))
    finally:
        handler.close()


def sample(args):
    data = args.data_dir
    for path in (args.output, getattr(args, "history", None)):
        if path is None:
            continue
        output = path.resolve()
        data_resolved = data.resolve()
        if output == data_resolved or data_resolved in output.parents:
            raise ValueError("output cannot be inside data directory")
    now = datetime.now(UTC)
    log = aggregate_logs(data, args.window_seconds, now)
    db_path = data / "lka.sqlite3"
    try: disk = {"available_bytes": shutil.disk_usage(data).free}
    except OSError: disk = {"available_bytes": None}
    result = {"schema": "lka.runtime_metrics", "version": 1, "generated_at": now.isoformat(),
              "window_seconds": args.window_seconds, "window_start": (now-timedelta(seconds=args.window_seconds)).isoformat(),
              "coverage": log.pop("coverage"), "errors": log.pop("errors"), "metrics": log,
              "storage": {"database_bytes": _file_size(db_path),
                          "wal_bytes": _file_size(Path(str(db_path)+"-wal"), missing=0),
                          **disk}}
    if args.database_state: result["database_state"] = database_state(db_path)
    if args.health_url: result["health"] = health(args.health_url)
    return result


def _file_size(path, *, missing=None):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return missing
    except OSError:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--window-seconds", type=int, default=300)
    p.add_argument("--output", type=Path)
    p.add_argument("--history", type=Path, help="Append private JSONL; rotate at 10 MiB, retain three backups. Single collector only.")
    p.add_argument("--database-state", action="store_true")
    p.add_argument("--health-url", type=validate_health_url)
    args = p.parse_args()
    if args.window_seconds <= 0: p.error("--window-seconds must be positive")
    if args.output and args.history and args.output.resolve() == args.history.resolve():
        p.error("--output and --history must use different files")
    result = sample(args)
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    if args.output: atomic_json(args.output, result)
    else: print(encoded)
    if args.history: append_history(args.history, result)


if __name__ == "__main__": main()
