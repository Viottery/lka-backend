"""Run an existing backend entry point with bounded, content-free memory diagnostics.

This opt-in operational wrapper never queries the database or records object values.
Use a private report path; tracing automatically stops at the configured deadline.
"""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import threading
import time
import tracemalloc
from pathlib import Path


def sample() -> dict:
    result = {"time": time.time(), "pid": os.getpid(), "threads": threading.active_count()}
    try:
        fields = Path('/proc/self/status').read_text().splitlines()
        result['process_kib'] = {line.split(':')[0]: int(line.split()[1]) for line in fields
                                 if line.startswith(('VmRSS:', 'VmSwap:', 'RssAnon:'))}
    except (OSError, ValueError):
        pass
    module = sys.modules.get('app.api.main')
    runtime = getattr(getattr(getattr(module, 'app', None), 'state', None), 'runtime', None)
    manager = getattr(runtime, 'agent_run_manager', None)
    if manager is not None:
        # Sizes only: no run identifiers, inputs, replies, tokens or credentials.
        with manager._lock:
            result['run_cache'] = {name: len(getattr(manager, name, {})) for name in
                                   ('_runs', '_events', '_terminal_cache', '_cancel_requests')}
            result['event_count'] = sum(map(len, manager._events.values()))
        reviews = manager.safety_reviews
        with reviews._condition:
            result['review_count'] = len(reviews._reviews)
    runner = getattr(runtime, 'agent_turn_runner', None)
    if runner is not None:
        result['runner_cache'] = {name: len(getattr(runner, name, {})) for name in
                                  ('_requests', '_results', '_errors', '_execution_locks')}
    if tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        result['python_bytes'] = current
        result['python_peak_bytes'] = peak
        result['tracer_bytes'] = tracemalloc.get_tracemalloc_memory()
        result['allocation_top'] = [
            {'file': str(item.traceback[0].filename), 'line': item.traceback[0].lineno,
             'bytes': item.size, 'blocks': item.count}
            for item in tracemalloc.take_snapshot().statistics('lineno')[:15]
        ]
    return result


def observe(report: Path, stop: threading.Event, *, interval: float, duration: float) -> None:
    deadline = time.monotonic() + duration
    while not stop.wait(interval):
        try:
            result = sample()
            # A single rotated file bounds disk use, without deleting business logs.
            if report.exists() and report.stat().st_size > 2 * 1024 * 1024:
                report.replace(report.with_suffix(report.suffix + '.previous'))
            fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + '\n')
            if result.get('tracer_bytes', 0) > 64 * 1024 * 1024:
                tracemalloc.stop()
        except Exception as exc:  # noqa: BLE001 - isolated operational diagnostics.
            print('memory observer sampling error: ' + type(exc).__name__, file=sys.stderr)
        if time.monotonic() >= deadline:
            tracemalloc.stop()
            break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=30)
    parser.add_argument('--duration', type=float, default=900)
    parser.add_argument('target_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.target.is_file() or args.interval < 5 or not 5 <= args.duration <= 3600:
        parser.error('target must exist, interval >= 5, duration between 5 and 3600 seconds')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    tracemalloc.start(1)
    stop = threading.Event()
    thread = threading.Thread(target=observe, args=(args.report, stop),
                              kwargs={'interval': args.interval, 'duration': args.duration},
                              name='lka-memory-observer', daemon=True)
    thread.start()
    target = args.target.resolve()
    sys.path.insert(0, str(target.parent))
    target_args = args.target_args[1:] if args.target_args[:1] == ['--'] else args.target_args
    sys.argv = [str(target), *target_args]
    try:
        runpy.run_path(str(target), run_name='__main__')
    finally:
        stop.set()
        thread.join(timeout=2)
        tracemalloc.stop()


if __name__ == '__main__':
    main()
