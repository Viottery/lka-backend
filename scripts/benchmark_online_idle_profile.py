"""Synthetic HTTP consumer mix derived from normalized online route counts.

Uses loopback accounts/devices, advancing cursors and empty task claims. No
personal history, provider calls or production credentials. SQL probes mean
latencies are diagnostic, not clean end-to-end SLO measurements.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import resource
import socket
import threading
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import benchmark_access_latency as latency
import benchmark_realistic_usage as usage
import workload_metrics


async def measure(args, runtime, fixture, origin):
    import httpx

    from app.storage.sqlite_timing import close_sqlite_timing, configure_sqlite_timing

    tag = uuid4().hex[:10]
    password = 'isolated-synthetic-profile-password'
    runtime.account_service.create_admin('profile-' + tag, password)
    usage.seed(runtime, 'profile-' + tag)
    manager = runtime.management_service.create(make_default=True)
    sid = manager['session']['session_id']
    for index in range(80):
        runtime.session_service.append_message(session_id=sid, role='user' if index % 2 == 0 else 'agent',
                                              content=f'Synthetic historical message {index}')
    run = runtime.agent_run_manager.create_run(session_id=sid, user_input='Synthetic completed state')
    runtime.agent_run_manager.mark_running(run.run_id)
    runtime.agent_run_manager.complete_run(run.run_id)
    nodes = []
    for index in range(2):
        environment = f'profile-env-{index}'
        node = runtime.device_service.enroll({
            'device_id': f'profile-{tag}-{index}', 'name': 'Synthetic profile node', 'platform': 'linux',
            'environment_id': environment,
            'scopes': ['heartbeat', 'sync.read', 'connections.poll',
                       'execution.poll', 'execution.run', 'execution.result'],
        })
        nodes.append({'headers': {'Authorization': 'Bearer ' + node['token']}, 'env': environment, 'cursor': 0})
    routes, errors, times = Counter(), Counter(), {}
    async with httpx.AsyncClient(base_url=origin, timeout=10, trust_env=False,
                                 limits=httpx.Limits(max_connections=16)) as client:
        login = await client.post('/auth/login', json={'username': 'profile-' + tag, 'password': password},
                                  headers={'Origin': origin})
        login.raise_for_status()
        client.headers.update({'Origin': origin, 'X-CSRF-Token': login.json()['csrf_token']})
        for node in nodes:
            page = await client.get('/node/sync/status', headers=node['headers'])
            page.raise_for_status()
            node['cursor'] = page.json()['cursor']
        jobs = []
        def job(name, interval, method, path, node=None, body=None):
            jobs.append({'name': name, 'period': interval, 'method': method, 'path': path,
                         'node': node, 'body': body})
        for node in nodes:
            for name, method, path, body in [
                ('node_status', 'GET', '/node/sync/status', None),
                ('node_changes', 'GET', '/node/sync/changes', None),
                ('node_heartbeat', 'POST', '/node/heartbeat', {}),
                ('node_grants', 'GET', '/node/connection-grants', None),
            ]:
                job(name, 9, method, path, node, body)
            job('node_empty_claim', 15.3, 'POST', '/node/execution/claim', node, {'environment_id': node['env']})
        for _ in range(2):
            job('input_queue', 5.08, 'GET', f'/sessions/{sid}/inputs')
        for part in ['events', 'deliveries', 'executions']:
            job('manager_' + part, 8, 'GET', f'/managers/{sid}/{part}')
        job('session_detail', 5.6, 'GET', f'/sessions/{sid}')
        job('session_runs', 12.6, 'GET', f'/sessions/{sid}/runs')
        job('run_detail', 9.6, 'GET', f'/agent/runs/{run.run_id}')
        job('safety_queue', 10.4, 'GET', '/agent/safety-reviews')
        job('run_safety', 18.3, 'GET', f'/agent/runs/{run.run_id}/safety-reviews')
        job('run_snapshot', 302.5, 'GET', f'/agent/runs/{run.run_id}/snapshot')
        job('terminal_stream', 302.5, 'GET', f'/agent/runs/{run.run_id}/stream')

        async def request(item, warm=False):
            node = item['node']
            params = {'after': node['cursor'], 'limit': 100} if item['name'] == 'node_changes' else None
            started = time.perf_counter()
            response = await client.request(item['method'], item['path'], params=params,
                                            headers=node['headers'] if node else None, json=item['body'])
            if warm:
                response.raise_for_status()
            else:
                routes[item['name']] += 1
                times.setdefault(item['name'], []).append((time.perf_counter() - started) * 1000)
                if response.status_code != 200:
                    errors[f"{item['name']}:HTTP{response.status_code}"] += 1
            if item['name'] == 'node_changes' and response.status_code == 200:
                result = response.json()
                assert result['after'] == node['cursor']
                assert result['cursor'] >= node['cursor']
                node['cursor'] = result['cursor']
        # Establish queues and validate every selected endpoint before counting.
        for item in jobs:
            await request(item, warm=True)
        recorder = configure_sqlite_timing(args.data_dir / 'profile-timing.jsonl', enabled=True,
                                           sample_rate=0, slow_ms=1_000_000)
        probe = workload_metrics.install(args.data_dir)
        before = resource.getrusage(resource.RUSAGE_SELF)
        probe.start()
        started = time.monotonic()
        for index, item in enumerate(jobs):
            item['due'] = started + ((index * .61803398875) % 1) * min(item['period'], args.duration / 2)
        while time.monotonic() - started < args.duration:
            now = time.monotonic()
            due = [item for item in jobs if item['due'] <= now]
            for item in due:
                item['due'] += item['period']
            if due:
                results = await asyncio.gather(*(request(item) for item in due), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        errors[type(result).__name__] += 1
            await asyncio.sleep(min(.05, max(0, args.duration - (time.monotonic() - started))))
        elapsed = time.monotonic() - started
        metrics = probe.stop()
        after = resource.getrusage(resource.RUSAGE_SELF)
        close_sqlite_timing(recorder)
    return {'scope': 'real loopback HTTP with synthetic accounts and two nodes; empty queues; no model work',
            'online_reference': '2026-10-29 normalized 605s access log; approximate route periods, not raw private replay',
            'excluded': 'no 3GB history, production IO/swap, TLS or tailnet; manager other approximated by executions',
            'seconds': round(elapsed, 3), 'request_counts': dict(routes), 'errors': dict(errors),
            'latency_instrumented': {name: latency.stats(values) for name, values in times.items()},
            'cpu_seconds': round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 3),
            'model_stage_counts': dict(fixture.stage_counts), 'storage_metrics': metrics,
            'all_runs': dict(Counter(record.status.value for record in runtime.agent_run_manager._runs.values())),
            'source_fingerprint': usage.fingerprint_source(args.source_root), 'backend': args.backend}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend', choices=['sqlite', 'mysql'], default='sqlite')
    parser.add_argument('--mysql-connection-file', type=Path)
    parser.add_argument('--duration', type=int, default=120)
    args = parser.parse_args()
    if not 30 <= args.duration <= 600:
        parser.error('duration must be 30..600 seconds')
    args.orchestrator, args.samples, args.model_delay_ms, args.online_idle_profile = 'langgraph', 4, 0, True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    import uvicorn
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', 0)); sock.listen(128)
        app, fixture, origin = latency.prepare(args, sock.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 60
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError('isolated profile server did not start')
                time.sleep(.02)
            result = asyncio.run(measure(args, app.state.runtime, fixture, origin))
        finally:
            server.should_exit = True
            thread.join(60)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'backend': args.backend, 'requests': sum(result['request_counts'].values()),
                          'errors': result['errors'], 'models': result['model_stage_counts']}), flush=True)
        return int(bool(result['errors']) or bool(result['model_stage_counts']))


if __name__ == '__main__':
    raise SystemExit(main())
