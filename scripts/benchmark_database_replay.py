"""Synthetic database replay with optional process and run-lock diagnostics.

All normal arguments are forwarded to benchmark_realistic_usage.py. This tool
adds no provider calls or production connections. SQL probes make this a cost
and recovery experiment, not a clean HTTP latency benchmark.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from pathlib import Path

import benchmark_realistic_usage as benchmark


def process_sample(pid: int) -> dict:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        io = dict(line.split(": ", 1) for line in Path(f"/proc/{pid}/io").read_text().splitlines())
        return {
            "pid": pid, "start_ticks": int(fields[19]),
            "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
            "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
            "read_bytes": int(io["read_bytes"]), "write_bytes": int(io["write_bytes"]),
        }
    except (OSError, ValueError, IndexError, KeyError):
        return {"available": False}


class LockDiagnostics:
    """Measure outermost RLock scopes; never sum nested holds as extra work."""
    def __init__(self, inner, active):
        self.inner, self.active = inner, active
        self.local = threading.local()
        self.guard = threading.Lock()
        self.waits: list[float] = []
        self.holds: list[float] = []
        self.truncated = False

    def __enter__(self):
        depth = getattr(self.local, "depth", 0)
        before = time.perf_counter()
        tracked = depth == 0 and self.active()
        self.inner.acquire()
        self.local.depth = depth + 1
        if depth == 0:
            self.local.tracked = tracked
            self.local.wait = (time.perf_counter() - before) * 1000
            self.local.started = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.local.depth -= 1
        if self.local.depth == 0 and self.local.tracked:
            elapsed = (time.perf_counter() - self.local.started) * 1000
            with self.guard:
                if len(self.waits) < 100_000:
                    self.waits.append(self.local.wait)
                    self.holds.append(elapsed)
                else:
                    self.truncated = True
        self.inner.release()

    @staticmethod
    def stats(values):
        ordered = sorted(values)
        if not ordered:
            return {"count": 0}
        return {"count": len(ordered), "total_ms": round(sum(ordered), 3),
                "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
                "max_ms": round(ordered[-1], 3)}

    def snapshot(self):
        with self.guard:
            return {"outer_wait": self.stats(self.waits), "outer_hold": self.stats(self.holds),
                    "truncated": self.truncated,
                    "scope": "manager lock only; hold includes IO; diagnostic instrumentation"}


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mysql-server-pid", type=int)
    parser.add_argument("--measure-run-lock", action="store_true")
    extra, forwarded = parser.parse_known_args()
    sys.argv = [sys.argv[0], *forwarded]
    args = benchmark.parse_args()
    original_load = benchmark.load_file

    def load(path, name):
        module = original_load(path, name)
        if path != benchmark.METRICS_FILE:
            return module
        original_install = module.install

        def install(data_dir):
            probe = original_install(data_dir)
            locks = []
            if extra.measure_run_lock:
                from app.core.agent_runs import InMemoryAgentRunManager
                original_init = InMemoryAgentRunManager.__init__

                def initialize(manager, *positional, **named):
                    original_init(manager, *positional, **named)
                    measured = LockDiagnostics(manager._lock, lambda: probe._active)
                    manager._lock = measured
                    locks.append(measured)
                InMemoryAgentRunManager.__init__ = initialize
            start, stop = probe.start, probe.stop
            initial = {}
            started = 0.0

            def begin():
                nonlocal initial, started
                initial = process_sample(extra.mysql_server_pid) if extra.mysql_server_pid else {}
                started = time.monotonic()
                return start()

            def finish():
                elapsed = time.monotonic() - started
                final = process_sample(extra.mysql_server_pid) if extra.mysql_server_pid else {}
                result = stop()
                result["manager_lock_diagnostics"] = [lock.snapshot() for lock in locks]
                server = {"start": initial, "end": final,
                          "scope": "whole MySQL process; other clients may contribute; not exclusively attributed"}
                if initial.get("start_ticks") is not None and initial.get("start_ticks") == final.get("start_ticks"):
                    delta = final["cpu_seconds"] - initial["cpu_seconds"]
                    server.update(cpu_seconds=round(delta, 3), mean_one_core_percent=round(delta / elapsed * 100, 3),
                                  read_bytes=final["read_bytes"] - initial["read_bytes"],
                                  write_bytes=final["write_bytes"] - initial["write_bytes"])
                result["mysql_process_diagnostics"] = server
                return result
            probe.start, probe.stop = begin, finish
            return probe
        module.install = install
        return module
    benchmark.load_file = load
    benchmark.run(args)


if __name__ == "__main__":
    main()
