#!/usr/bin/env python3
"""Isolated loopback API latency, with synthetic models and no SQL tracing.

Run each revision/engine in its own process and fresh data directory. MySQL
requires a disposable schema and is never silently reset. Results are finite
sample measurements, not production/network/model latency or capacity claims.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import socket
import sys
import threading
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def stats(values):
    ordered = sorted(values)
    if not ordered:
        return {"n": 0}
    return {
        "n": len(ordered),
        **{name: round(ordered[max(0, math.ceil(len(ordered) * q) - 1)], 3)
           for name, q in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99))},
        "max_ms": round(ordered[-1], 3),
    }


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def answer_delta(event, body):
    """A control event or final answer is not a streamed answer token."""
    if event != "llm_delta":
        return False
    payload = body.get("payload", {})
    return (payload.get("content_role") == "final_answer"
            and isinstance(payload.get("delta"), str) and bool(payload["delta"]))


def prepare(args, port):
    source = args.source_root.resolve(strict=True)
    data = args.data_dir.resolve()
    if data.exists():
        raise ValueError("data directory must be new; no existing data is opened")
    if not 4 <= args.samples <= 100 or not 0 <= args.model_delay_ms <= 1000:
        raise ValueError("samples must be 4..100; model delay 0..1000ms")
    data.mkdir(parents=True, mode=0o700)
    workspace = data / "workspace"
    workspace.mkdir()
    origin = f"http://127.0.0.1:{port}"
    config = data / "latency.toml"
    config.write_text(f'''[llm]
provider="mock"
[agent]
orchestrator="{args.orchestrator}"
unified_entry_enabled=false
checkpoint_backend="sqlite"
answer_review_enabled=true
[auth]
enabled=true
require_https=false
secure_cookie=false
allowed_origins=["{origin}"]
[devices]
enabled=true
[sync]
enabled=true
role="hub"
[management]
enabled=true
autonomous_enabled=false
default_task_parent={json.dumps(str(workspace))}
[memory]
enabled=true
background_enabled=false
atomic_context_background_enabled=false
consolidation_enabled=false
[message_history]
enabled=false
background_enabled=false
[embedding]
enabled=false
[reranker]
enabled=false
''')
    if getattr(args, "online_idle_profile", False):
        # The separate read-profile harness exercises empty background workers
        # and node control endpoints, without submitting business/model work.
        value = config.read_text().replace("[memory]\nenabled=true\nbackground_enabled=false",
                                           "[memory]\nenabled=true\nbackground_enabled=true")
        value = value.replace("atomic_context_background_enabled=false", "atomic_context_background_enabled=true")
        value = value.replace("consolidation_enabled=false", "consolidation_enabled=true")
        config.write_text(value + '\n[connections]\nenabled=true\n[remote_execution]\nenabled=true\n')
    for key in list(os.environ):
        if key.startswith(("LKA_", "OPENAI_", "ANTHROPIC_", "MS_GRAPH_", "MAIL_", "IMAP_", "SMTP_")):
            os.environ.pop(key)
    os.environ.update(LKA_DATA_DIR=str(data), LKA_LOCAL_CONFIG=str(config),
                      LKA_STORAGE_BACKEND=args.backend, LKA_DB_TIMING_ENABLED="false",
                      LKA_WORKSPACE_ROOTS=str(workspace))
    if args.backend == "mysql":
        if args.mysql_connection_file is None:
            raise ValueError("mysql requires a private lab connection file")
        secret = json.loads(args.mysql_connection_file.read_text())
        if "lab" not in secret["database"].lower() and "test" not in secret["database"].lower():
            raise ValueError("mysql schema name must identify a test/lab")
        import pymysql

        with pymysql.connect(**secret, autocommit=True) as conn, conn.cursor() as cursor:
            for table in ("agent_sessions", "agent_runs", "memory_entries"):
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                if cursor.fetchone()[0]:
                    raise ValueError("MySQL benchmark requires an empty migrated business schema")
        for key in ("host", "port", "user", "password", "database"):
            if key in secret:
                os.environ["LKA_MYSQL_" + key.upper()] = str(secret[key])
        secret.clear()
    sys.path.insert(0, str(source))
    from app.core.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.data_dir.resolve() == data
    # Archived pre-MySQL revisions expose SQLite only and lack this setting.
    assert getattr(settings, "storage_backend", "sqlite") == args.backend
    from app.api.main import app

    fixture = load(ROOT / "scripts/benchmark_realistic_usage_fixture.py", "latency_fixture")
    client = fixture.install_fixture(app.state.runtime, foreground_delay=args.model_delay_ms / 1000,
                                     background_delay=0)
    return app, client, origin


async def measure(args, runtime, fixture, origin):
    import httpx

    groups = {}
    failures = Counter()
    outcomes = []
    sse_first, sse_text, sse_final = [], [], []
    observed_stages = {}
    username = "latency-" + uuid4().hex[:10]
    runtime.account_service.create_admin(username, "isolated-synthetic-password-2026")
    seed = load(ROOT / "scripts/benchmark_realistic_usage.py", "latency_seed")
    seed.seed(runtime, "latency-fixture")
    manager = runtime.management_service.create(make_default=True)
    history_id = manager["session"]["session_id"]
    for i in range(80):
        runtime.session_service.append_message(session_id=history_id,
                                              role="user" if i % 2 == 0 else "agent",
                                              content=f"Synthetic historical text {i}")
    session = runtime.create_session(title="Synthetic latency session")
    sid = seed.session_id(session)
    device = runtime.device_service.enroll({
        "device_id": "latency-" + uuid4().hex[:10], "name": "Synthetic latency node",
        "platform": "linux", "environment_id": "synthetic-latency-env",
        "scopes": ["sync.read", "heartbeat"],
    })
    node_headers = {"Authorization": "Bearer " + device["token"]}
    async with httpx.AsyncClient(base_url=origin, timeout=60, trust_env=False,
                                 limits=httpx.Limits(max_connections=16)) as web:
        login = await web.post("/auth/login", json={"username": username,
                               "password": "isolated-synthetic-password-2026"}, headers={"Origin": origin})
        login.raise_for_status()
        web.headers.update({"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]})
        sync_status = await web.get("/node/sync/status", headers=node_headers)
        sync_status.raise_for_status()
        high = sync_status.json()["cursor"]

        async def request(name, method, path, **kwargs):
            start = time.perf_counter()
            try:
                response = await web.request(method, path, **kwargs)
                elapsed = (time.perf_counter() - start) * 1000
                if response.status_code != 200:
                    failures[f"{name}:HTTP{response.status_code}"] += 1
                    return None
                groups.setdefault(name, []).append(elapsed)
                return response.json()
            except Exception as exc:  # noqa: BLE001 - retain failures without bodies or tokens
                failures[f"{name}:{type(exc).__name__}"] += 1
                return None

        async def turn(name, session_id):
            result = await request(name, "POST", "/agent/turn", json={
                "session_id": session_id, "user_input": "请简要解释为什么数据库应使用短事务。",
                "safety_review_mode": "llm",
            })
            try:
                if result is not None:
                    run = runtime.agent_run_manager.get_run(result["run_id"])
                    outcomes.append(str(run.status.value if hasattr(run.status, "value") else run.status))
                    if not result.get("answer"):
                        failures[f"{name}:empty_answer"] += 1
                    if outcomes[-1] != "completed":
                        failures[f"{name}:run_not_completed"] += 1
            except Exception as exc:  # noqa: BLE001 - retain verification failures
                failures[f"{name}:verification:{type(exc).__name__}"] += 1

        # Warm all routes and the actual inference path outside sample windows.
        await turn("warmup", sid)
        await request("warmup", "GET", f"/sessions/{history_id}?message_limit=20")
        groups.clear()
        outcomes.clear()
        measured_start = time.time()
        print(json.dumps({"progress": "latency_samples_start", "unix_seconds": measured_start}), flush=True)
        before = Counter(fixture.stage_counts)
        for _ in range(args.samples):
            await turn("turn_serial", sid)
        observed_stages["turn_serial"] = dict(Counter(fixture.stage_counts) - before)
        for name, path in (
            ("history_20", f"/sessions/{history_id}?message_limit=20"),
            ("sync_empty", f"/node/sync/changes?after={high}&limit=100"),
            ("sync_initial_page", "/node/sync/changes?after=0&limit=100"),
        ):
            if name == "sync_empty":
                current = await web.get("/node/sync/status", headers=node_headers)
                current.raise_for_status()
                high = current.json()["cursor"]
                path = f"/node/sync/changes?after={high}&limit=100"
            for _ in range(args.samples * 2):
                page = await request(name, "GET", path,
                                     **({"headers": node_headers} if name.startswith("sync_") else {}))
                if page is not None and name == "sync_empty" and page["changes"]:
                    groups.setdefault("sync_incremental_changes", []).append(groups[name].pop())
                    high = page["cursor"]
                    path = f"/node/sync/changes?after={high}&limit=100"

        sessions = [seed.session_id(runtime.create_session(title="Concurrent synthetic session"))
                    for _ in range(4)]
        before = Counter(fixture.stage_counts)

        async def observer():
            for _ in range(args.samples * 2):
                await request("history_during_turns", "GET", f"/sessions/{history_id}?message_limit=20")
                await asyncio.sleep(.01)

        async def lane(index):
            for _ in range(index, args.samples, 4):
                await turn("turn_concurrent_4", sessions[index])

        await asyncio.gather(observer(), *(lane(i) for i in range(4)))
        observed_stages["turn_concurrent_4"] = dict(Counter(fixture.stage_counts) - before)
        for _ in range(6):
            start = time.perf_counter()
            first = text = final = False
            event = ""
            try:
                async with web.stream("POST", "/agent/turn/stream", json={
                    "session_id": sid, "user_input": "请简要解释短事务。", "safety_review_mode": "llm",
                }) as response:
                    if response.status_code != 200:
                        failures[f"sse:HTTP{response.status_code}"] += 1
                        continue
                    async for line in response.aiter_lines():
                        if line.startswith("event: "):
                            event = line[7:]
                            elapsed = (time.perf_counter() - start) * 1000
                            if not first:
                                sse_first.append(elapsed)
                                first = True
                            if event == "final_answer" and not final:
                                sse_final.append(elapsed)
                                final = True
                            if event in {"error", "run_error"}:
                                failures[f"sse:{event}"] += 1
                        elif line.startswith("data: ") and not text:
                            if answer_delta(event, json.loads(line[6:])):
                                sse_text.append((time.perf_counter() - start) * 1000)
                                text = True
                    groups.setdefault("sse_complete", []).append((time.perf_counter() - start) * 1000)
                    if not final:
                        failures["sse:missing_final_answer"] += 1
            except Exception as exc:  # noqa: BLE001 - report incomplete streams
                failures[f"sse:{type(exc).__name__}"] += 1
    with closing(runtime._conn()) as conn:
        nonterminal = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE status NOT IN "
                                   "('completed','failed','cancelled')").fetchone()[0]
        states = {row[0]: row[1] for row in conn.execute("SELECT status,COUNT(*) FROM agent_runs GROUP BY status")}
    result = {"latency": {key: stats(value) for key, value in groups.items()},
            "sample_window_unix_seconds": {"start": measured_start, "end": time.time()},
            "sse_first_event": stats(sse_first), "sse_first_answer_delta": {
                **stats(sse_text), "available": bool(sse_text),
                "note": "fixture has no token streaming; absent samples cannot establish TTFT",
            },
            "sse_final_answer": stats(sse_final), "errors": dict(failures),
            "foreground_outcomes": dict(Counter(outcomes)), "all_runs": states,
            "nonterminal_runs": nonterminal, "model_stage_counts": observed_stages}
    if getattr(args, "raw_samples", False):
        result["latency_samples_ms"] = {name: [round(value, 3) for value in values]
                                        for name, values in groups.items()}
        result["sse_first_event_samples_ms"] = [round(value, 3) for value in sse_first]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("sqlite", "mysql"), default="sqlite")
    parser.add_argument("--mysql-connection-file", type=Path)
    parser.add_argument("--orchestrator", choices=("legacy", "langgraph"), default="legacy")
    parser.add_argument("--samples", type=int, default=24)
    parser.add_argument("--model-delay-ms", type=int, default=100)
    parser.add_argument("--raw-samples", action="store_true",
                        help="retain bounded numeric samples for valid repeated-run aggregation")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        app, fixture, origin = prepare(args, sock.getsockname()[1])
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 60
        try:
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("isolated server did not start")
                time.sleep(.02)
            try:
                result = asyncio.run(measure(args, app.state.runtime, fixture, origin))
            except Exception as exc:  # noqa: BLE001 - never lose a failed measurement receipt
                result = {"errors": {f"measurement:{type(exc).__name__}": 1},
                          "latency": {}, "nonterminal_runs": None, "all_runs": {}}
        finally:
            server.should_exit = True
            thread.join(timeout=60)
        result.update(backend=args.backend, orchestrator=args.orchestrator, samples=args.samples,
                      model_delay_ms_per_call=args.model_delay_ms,
                      scope="loopback HTTP; synthetic models; background LLM disabled; normal schedulers active; no SQL timing/stack probes",
                      percentile_method="nearest rank; p99 equals max for samples under 100; not an SLO",
                      model_note="fixed sleep is synthetic, not real model latency; SSE first event is not TTFT",
                      source_fingerprint=load(ROOT / "scripts/benchmark_realistic_usage.py", "latency_hash").fingerprint_source(args.source_root))
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return int(bool(result["errors"]) or result["nonterminal_runs"] != 0
                   or result["all_runs"] != {"completed": 2 * args.samples + 7})


if __name__ == "__main__":
    raise SystemExit(main())
