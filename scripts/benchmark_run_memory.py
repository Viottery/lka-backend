"""Bounded synthetic stream-cache retention replay; no model or source data.

Compare each source in a separate process. At the maximum fixture, legacy
full-prefix event caching retains approximately 320 MiB of snapshot text. This
is a cache-retention experiment, not a reproduction of production RSS or IO.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--deltas', type=int, default=256)
    parser.add_argument('--delta-bytes', type=int, default=512)
    args = parser.parse_args()
    if not (1 <= args.runs <= 20 and 1 <= args.deltas <= 256 and 1 <= args.delta_bytes <= 512):
        parser.error('bounded fixture: runs<=20, deltas<=256, delta-bytes<=512; all positive')
    source = args.source_root.resolve(strict=True)
    args.data_dir.mkdir(parents=True, exist_ok=False)
    config = args.data_dir / 'mock.toml'
    config.write_text('[llm]\nprovider="mock"\n')
    for key in list(os.environ):
        if key.startswith(('LKA_', 'OPENAI_', 'ANTHROPIC_', 'MAIL_', 'IMAP_', 'SMTP_', 'MS_GRAPH_')):
            os.environ.pop(key, None)
    os.environ.update(LKA_DATA_DIR=str(args.data_dir), LKA_LOCAL_CONFIG=str(config))
    sys.path.insert(0, str(source))
    from app.core.config import Settings, get_settings
    Settings.model_config['env_file'] = None
    get_settings.cache_clear()
    from app.core.agent_runs import InMemoryAgentRunManager
    from app.core.agent_storage import SqliteAgentRunStore
    store = SqliteAgentRunStore(args.data_dir / 'synthetic.sqlite3')
    manager = InMemoryAgentRunManager(durable_store=store)
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    last_run = None
    try:
        for index in range(args.runs):
            run = manager.create_run(session_id=f'synthetic-{index}', user_input='Synthetic stream retention')
            manager.mark_running(run.run_id)
            snapshot = ''
            delta = 'x' * args.delta_bytes
            for _ in range(args.deltas):
                snapshot += delta
                manager.append_event(run.run_id, 'llm_delta', 'Synthetic delta', stage='answer',
                                     payload={'llm_call_id': f'call-{index}', 'delta': delta,
                                              'content_snapshot': snapshot})
            manager.flush_events()
            manager.complete_run(run.run_id)
            last_run = run.run_id
        after = resource.getrusage(resource.RUSAGE_SELF)
        elapsed = time.perf_counter() - started
        cached = [event for group in manager._events.values() for event in group]
        retained = sum(len(event.payload.get('content_snapshot', '').encode()) for event in cached)
        page = manager.list_events(last_run, after_sequence=args.deltas - 1, limit=1)
        assert len(page) == 1 and page[0].payload['content_snapshot'] == snapshot
        # Older snapshots expose only the full durable event reader. Keep the
        # compatibility read outside the measured CPU/RSS window in both arms.
        latest = getattr(store, 'latest_event_sequence', None)
        sequence = latest(last_run) if callable(latest) else max(
            event['sequence'] for event in store.list_events(last_run))
        assert sequence == args.deltas
        restored = InMemoryAgentRunManager(durable_store=store)
        try:
            assert restored.get_run(last_run) is not None
            durable_page = restored.list_events(last_run, after_sequence=args.deltas - 1, limit=1)
            assert len(durable_page) == 1
            assert durable_page[0].payload['content_snapshot'] == snapshot
        finally:
            restored.close()
        result = {'scope': 'synthetic SQLite stream-cache replay; independent process; no SQL probes or models',
                  'runs': args.runs, 'deltas_per_run': args.deltas, 'delta_bytes': args.delta_bytes,
                  'cached_events': len(cached), 'cached_snapshot_text_bytes': retained,
                  'completed_runs': args.runs, 'last_prefix_restored': True,
                  'peak_rss_kib': after.ru_maxrss,
                  'cpu_seconds': round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 3),
                  'seconds': round(elapsed, 3)}
    finally:
        manager.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
