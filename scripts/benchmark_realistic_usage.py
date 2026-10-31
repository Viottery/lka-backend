#!/usr/bin/env python3
"""Run a bounded synthetic everyday-use workload against one isolated LKA source.

This is an application/service benchmark, not an HTTP latency benchmark. It makes
no provider calls; the fixture client accepts only the documented stages below.
Use separate data directories (and a dedicated test schema for MySQL) per arm.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import resource
import sys
import time
import traceback
import uuid
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
METRICS_FILE = REPO / "scripts/workload_metrics.py"
DEFAULT_FIXTURE_FILE = REPO / "scripts/benchmark_realistic_usage_fixture.py"


def load_file(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark helper: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--fixture-file", type=Path, default=DEFAULT_FIXTURE_FILE)
    parser.add_argument("--fast-background", action="store_true",
                        help="pilot only: set memory/context/consolidation debounce to zero")
    parser.add_argument("--capture-mysql-errors", action="store_true",
                        help="diagnostic only: record exception class/category/errno, callsite and SQL hash")
    parser.add_argument("--sync-profile", choices=("incremental", "repeat-first-page"),
                        default="incremental",
                        help="incremental cursor is the normal-use profile; repeat-first-page reproduces the old benchmark")
    parser.add_argument("--arm", choices=("old-sqlite", "new-sqlite", "new-mysql"), required=True)
    parser.add_argument("--mysql-connection-file", type=Path)
    parser.add_argument("--mysql-database", help="already-created unique disposable test schema")
    return parser.parse_args()


def write_config(data_dir: Path, *, fast_background: bool = False) -> Path:
    config = data_dir / "benchmark.toml"
    workspace = data_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    # Memory extraction, intent tracking, consolidation and hub/device service
    # exercise the same optional background paths on each source revision.
    intent_debounce = 0 if fast_background else 30
    extraction_debounce = 0 if fast_background else 5
    consolidation_debounce = 0 if fast_background else 60
    config.write_text(f'''[llm]\nprovider="mock"\n\n[agent]\norchestrator="legacy"\ncheckpoint_backend="sqlite"\nunified_entry_enabled=false\nanswer_review_enabled=true\n\n[auth]\nenabled=true\nrequire_https=false\nsecure_cookie=false\n\n[devices]\nenabled=true\n\n[sync]\nenabled=true\nrole="hub"\n\n[memory]\nenabled=true\nbackground_enabled=true\nallow_remote_extraction=true\natomic_context_mode="active"\natomic_context_profile="intent"\natomic_context_background_enabled=true\natomic_context_debounce_seconds={intent_debounce}\nbackground_worker_count=2\nextraction_debounce_seconds={extraction_debounce}\nconsolidation_enabled=true\nconsolidation_debounce_seconds={consolidation_debounce}\nconsolidation_interval_seconds=21600\nconsolidation_min_confidence=0.9\n\n[message_history]\nenabled=false\nbackground_enabled=false\n\n[management]\nenabled=true\nautonomous_enabled=true\ndefault_task_parent={json.dumps(str(workspace))}\n''', encoding="utf-8")
    return config


def clear_sensitive_environment():
    prefixes = ("LKA_", "OPENAI_", "ANTHROPIC_", "MS_GRAPH_", "MAIL_", "IMAP_", "SMTP_")
    for key in list(os.environ):
        if key.startswith(prefixes):
            os.environ.pop(key, None)


def fingerprint_source(root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted([*root.glob("app/**/*.py"), *root.glob("app/storage/*v*.json")])
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def install_mysql_error_capture(source_root: Path):
    """Capture only typed exception metadata; never retain SQL, binds or messages."""
    from app.storage import mysql

    original = mysql.MySQLCursor.execute
    counts = Counter()

    def execute(cursor, statement, parameters=()):
        try:
            return original(cursor, statement, parameters)
        except BaseException as exc:
            cause = getattr(exc, "original", None)
            cause_args = getattr(cause, "args", ())
            errno = getattr(exc, "mysql_errno", None)
            if errno is None and cause_args and type(cause_args[0]) is int:
                errno = cause_args[0]
            callsite = None
            for frame in reversed(traceback.extract_stack(limit=48)[:-1]):
                path = Path(frame.filename)
                try:
                    relative = path.resolve().relative_to(source_root)
                except (OSError, ValueError):
                    continue
                if (not relative.parts or relative.parts[0] != "app"
                        or relative.as_posix() == "app/storage/mysql.py"):
                    continue
                callsite = {"file": relative.as_posix(), "function": frame.name, "line": frame.lineno}
                break
            statement_bytes = statement.encode("utf-8", errors="replace") if isinstance(statement, str) else b""
            key = (
                type(exc).__name__, getattr(exc, "error_category", None), errno,
                type(cause).__name__ if cause is not None else None,
                (callsite or {}).get("file"), (callsite or {}).get("function"),
                (callsite or {}).get("line"), hashlib.sha256(statement_bytes).hexdigest()[:16],
            )
            counts[key] += 1
            raise

    mysql.MySQLCursor.execute = execute

    def snapshot():
        return [
            {"exception_type": key[0], "category": key[1], "errno": key[2],
             "cause_type": key[3], "callsite": ({"file": key[4], "function": key[5], "line": key[6]}
                                               if key[4] else None),
             "statement_hash": key[7], "count": count}
            for key, count in sorted(counts.items(), key=lambda item: repr(item[0]))
        ]

    return snapshot


def session_id(detail):
    return detail.session.session_id if hasattr(detail, "session") else detail.session_id


class SyncPullTracker:
    """Bounded per-device journal cursors for repeatable hub-sync workloads."""

    def __init__(self, profile: str, *, page_size: int = 20,
                 baseline_page_limit: int = 64, measurement_page_limit: int = 10):
        if profile not in {"incremental", "repeat-first-page"}:
            raise ValueError("unsupported sync profile")
        self.profile = profile
        self.page_size = page_size
        self.baseline_page_limit = baseline_page_limit
        self.measurement_page_limit = measurement_page_limit
        self.cursors: dict[str, int] = {}
        self.baseline_pages = self.baseline_changes = 0
        self.measurement_pages = self.measurement_changes = self.poll_rounds = 0
        self.has_more_rounds = Counter()
        self._devices: dict[str, dict[str, int]] = {}

    @staticmethod
    def _page_value(page, key):
        value = page.get(key)
        if key == "changes":
            if not isinstance(value, list):
                raise ValueError("sync page changes must be a list")
            return value
        if key == "has_more":
            if type(value) is not bool:
                raise ValueError("sync page has_more must be boolean")
            return value
        if type(value) is not int or value < 0:
            raise ValueError("sync page cursor must be a non-negative integer")
        return value

    def catch_up(self, device_key: str, pull_page) -> dict[str, int]:
        """Advance a device through seed history before the measured window."""
        if not device_key:
            raise ValueError("device key is required")
        cursor = self.cursors.get(device_key, 0)
        pages = changes = 0
        if self.profile == "repeat-first-page":
            self.cursors[device_key] = 0
            self._devices[device_key] = {"baseline_pages": 0, "baseline_changes": 0,
                                         "measurement_pages": 0, "measurement_changes": 0}
            return {"pages": 0, "changes": 0, "cursor": 0}
        device = self._devices.setdefault(device_key, {
            "baseline_pages": 0, "baseline_changes": 0,
            "measurement_pages": 0, "measurement_changes": 0,
        })
        for _ in range(self.baseline_page_limit):
            page = pull_page(after=cursor, limit=self.page_size)
            next_cursor = self._page_value(page, "cursor")
            has_more = self._page_value(page, "has_more")
            page_changes = self._page_value(page, "changes")
            if next_cursor < cursor or (has_more and next_cursor == cursor):
                raise RuntimeError("sync catch-up cursor made no forward progress")
            pages += 1
            changes += len(page_changes)
            cursor = next_cursor
            if not has_more:
                break
        else:
            raise RuntimeError("sync catch-up exceeded 64 pages; steady-state measurement refused")
        self.cursors[device_key] = cursor
        device["baseline_pages"] += pages
        device["baseline_changes"] += changes
        self.baseline_pages += pages
        self.baseline_changes += changes
        return {"pages": pages, "changes": changes, "cursor": cursor}

    def poll(self, device_key: str, pull_page) -> dict[str, int | bool]:
        """Read at most ten pages, retaining the cursor for the next poll."""
        if not device_key:
            raise ValueError("device key is required")
        device = self._devices.setdefault(device_key, {
            "baseline_pages": 0, "baseline_changes": 0,
            "measurement_pages": 0, "measurement_changes": 0,
        })
        self.poll_rounds += 1
        pages = changes = 0
        cursor = 0 if self.profile == "repeat-first-page" else self.cursors.get(device_key, 0)
        has_more = False
        for _ in range(1 if self.profile == "repeat-first-page" else self.measurement_page_limit):
            page = pull_page(after=0 if self.profile == "repeat-first-page" else cursor,
                             limit=self.page_size)
            next_cursor = self._page_value(page, "cursor")
            has_more = self._page_value(page, "has_more")
            page_changes = self._page_value(page, "changes")
            pages += 1
            changes += len(page_changes)
            if self.profile == "repeat-first-page":
                continue
            if next_cursor < cursor or (has_more and next_cursor == cursor):
                raise RuntimeError("sync poll cursor made no forward progress")
            cursor = next_cursor
            if not has_more:
                break
        if self.profile == "incremental":
            self.cursors[device_key] = cursor
            if has_more:
                self.has_more_rounds[device_key] += 1
        device["measurement_pages"] += pages
        device["measurement_changes"] += changes
        self.measurement_pages += pages
        self.measurement_changes += changes
        return {"pages": pages, "changes": changes,
                "cursor": 0 if self.profile == "repeat-first-page" else cursor,
                "has_more": has_more}

    def snapshot(self):
        return {
            "profile": self.profile,
            "page_size": self.page_size,
            "baseline_page_limit": self.baseline_page_limit,
            "measurement_page_limit": self.measurement_page_limit,
            "baseline": {"pages": self.baseline_pages, "changes": self.baseline_changes},
            "measurement": {"poll_rounds": self.poll_rounds, "pages": self.measurement_pages,
                            "changes": self.measurement_changes},
            "cursors": dict(sorted(self.cursors.items())),
            "has_more_rounds": dict(sorted(self.has_more_rounds.items())),
            "devices": {key: dict(value) for key, value in sorted(self._devices.items())},
        }


def seed(runtime, tag: str):
    # These fixtures are synthetic and created before the measurement window.
    for index in range(50):
        session = runtime.create_session(title=f"Synthetic history {index:02d}")
        for message_index in range(10):
            role = "user" if message_index % 2 == 0 else "agent"
            runtime.session_service.append_message(
                session_id=session_id(session), role=role,
                content=f"Synthetic benchmark history {index:02d}/{message_index:02d}; no personal data.",
            )
    source_id = runtime.memory_service.register_source(
        __import__("app.domains.memory", fromlist=["MemorySourceInput"]).MemorySourceInput(
            source_type="user_message", source_ref=f"synthetic-seed:{tag}",
            checksum=hashlib.sha256(tag.encode()).hexdigest(), trusted_source=False,
            metadata={"synthetic": True},
        )
    )
    MemoryInput = __import__("app.domains.memory", fromlist=["MemoryInput"]).MemoryInput
    for index in range(100):
        runtime.memory_service.create(MemoryInput(
            content=f"Synthetic low-confidence benchmark fact {index:03d}.",
            memory_type="fact", source_id=source_id, confidence=0.2,
            user_confirmed=False, sensitivity="low", metadata={"synthetic": True},
        ))


def run(args):
    source_root = args.source_root.expanduser().resolve(strict=True)
    data_dir = args.data_dir.expanduser().resolve()
    if data_dir == (REPO / "data/runtime").resolve() or (data_dir / "lka.sqlite3").exists():
        raise SystemExit("refusing an existing/everyday data directory")
    if args.duration < 20 or args.duration > 1800:
        raise SystemExit("duration must be between 20 and 1800 seconds")
    if args.arm == "new-mysql" and (not args.mysql_connection_file or not args.mysql_database):
        raise SystemExit("new-mysql requires private connection file and unique database name")
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    config_file = write_config(data_dir, fast_background=args.fast_background)
    clear_sensitive_environment()
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != REPO]
    sys.path.insert(0, str(source_root))
    os.environ.update(LKA_DATA_DIR=str(data_dir), LKA_LOCAL_CONFIG=str(config_file),
                      LKA_STORAGE_BACKEND="sqlite" if args.arm != "new-mysql" else "mysql",
                      LKA_WORKSPACE_ROOTS=str(data_dir / "workspace"),
                      LKA_DB_TIMING_ENABLED="true", LKA_DB_TIMING_SAMPLE_RATE="0")
    if args.arm == "new-mysql":
        secret = json.loads(args.mysql_connection_file.expanduser().resolve(strict=True).read_text())
        if secret.get("database") != args.mysql_database:
            raise SystemExit("connection file does not target the requested isolated schema")
        if not any(word in args.mysql_database.lower() for word in ("test", "lab")):
            raise SystemExit("refusing a MySQL schema without test/lab in its name")
        for name in ("host", "port", "user", "password", "database"):
            os.environ["LKA_MYSQL_" + name.upper()] = str(secret[name])
        os.environ["LKA_MYSQL_POOL_SIZE"] = "8"
        secret.clear()
    from app.core.config import Settings
    Settings.model_config["env_file"] = None
    settings = Settings()
    assert settings.data_dir.resolve() == data_dir and settings.local_config_path.resolve() == config_file
    if args.arm == "new-mysql":
        assert settings.storage_backend == "mysql" and settings.mysql_database == args.mysql_database
    else:
        assert getattr(settings, "storage_backend", "sqlite") == "sqlite"

    # Hooks are installed after importing the source tree and before runtime init,
    # then activated after startup + seed so their interval is comparable.
    probe_mod = load_file(METRICS_FILE, "lka_workload_metrics")
    probe = probe_mod.install(data_dir)
    mysql_error_snapshot = (install_mysql_error_capture(source_root)
                            if args.arm == "new-mysql" and args.capture_mysql_errors else None)
    from app.storage.sqlite_timing import configure_sqlite_timing
    timing_recorder = configure_sqlite_timing(
        data_dir / "benchmark-sqlite-timing.jsonl", enabled=True, sample_rate=0.0,
        slow_ms=1_000_000,
    )
    from app.core.runtime import LocalKnowledgeAgentRuntime
    runtime = LocalKnowledgeAgentRuntime(settings)
    fixture_mod = load_file(args.fixture_file.expanduser().resolve(strict=True), "lka_synthetic_fixture")
    fixture = fixture_mod.install_fixture(runtime, foreground_delay=0.45, background_delay=0.1)
    runtime.start()
    run_id = uuid.uuid4().hex[:10]
    seed(runtime, run_id)
    manager = runtime.management_service.create(make_default=True)
    manager_session_id = manager["session"]["session_id"]
    runtime.management_service.update(manager_session_id, expected_revision=1, status="active")

    # Two browser observers read the active conversation's real growing history.
    DeviceEnrollment = __import__("app.domains.devices", fromlist=["DeviceEnrollment"]).DeviceEnrollment
    principals = []
    for n in range(2):
        enrolled = runtime.device_service.enroll(DeviceEnrollment(
            device_id=f"bench-{run_id}-{n}", name=f"Synthetic bench device {n}", platform="linux",
            environment_id=f"bench-env-{n}", scopes=["heartbeat", "sync.read"],
        ))
        principals.append(runtime.device_service.authenticate(enrolled["token"]))
    sync_tracker = SyncPullTracker(args.sync_profile)
    for index, principal in enumerate(principals):
        sync_tracker.catch_up(
            f"device_{index}",
            lambda *, after, limit, node=principal: runtime.sync_service.pull(
                after=after, limit=limit, principal=node),
        )
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    probe.start()
    started = time.monotonic()
    end = started + args.duration
    turn_times = [started + args.duration * fraction for fraction in (0.04, 0.20, 0.40, 0.62, 0.82)]
    next_turn_index = 0
    turns = []
    read_count = 0
    heartbeat_count = 0
    sync_count = 0
    next_observation = started
    next_device_cycle = started
    errors = Counter()
    texts = [
        "回答技术问题时我希望先看到结论，再读关键依据。",
        "请解释一下数据库隔离级别的差异，用简洁的例子说明。",
        "回答技术问题时我希望关键依据简明，避免冗长背景。",
        "回答技术问题时我希望先看到结论，再读关键依据。",
        "解释为什么短事务有助于降低数据库锁竞争。",
    ]
    primary = runtime.create_session(title="Synthetic realistic workload")
    while time.monotonic() < end:
        now = time.monotonic()
        if next_turn_index < len(turn_times) and now >= turn_times[next_turn_index]:
            text = texts[next_turn_index]
            try:
                result = runtime.run_agent_turn(session_id=session_id(primary), user_input=text,
                                                client_request_id=f"bench-{run_id}-{next_turn_index}")
                turns.append({"index": next_turn_index, "status": getattr(result, "status", "completed"),
                              "run_id_present": bool(getattr(result, "run_id", None))})
                print(json.dumps({"progress": "turn", "arm": args.arm,
                                  "index": next_turn_index, "status": turns[-1]["status"]}), flush=True)
            except Exception as exc:  # noqa: BLE001 - benchmark must retain phase failures
                errors[type(exc).__name__] += 1
                turns.append({"index": next_turn_index, "status": "error"})
                print(json.dumps({"progress": "turn", "arm": args.arm,
                                  "index": next_turn_index, "status": "error",
                                  "error_type": type(exc).__name__}), flush=True)
            next_turn_index += 1
            if args.fast_background and next_turn_index == 5:
                runtime.memory_background.schedule_maintenance()
        if now >= next_observation:
            for _observer in range(2):
                try:
                    runtime.session_service.get_message_page(session_id=session_id(primary), limit=20)
                    read_count += 1
                except Exception as exc:  # noqa: BLE001 - benchmark must retain phase failures
                    errors[type(exc).__name__] += 1
            next_observation = now + 15
        if now >= next_device_cycle:
            for index, principal in enumerate(principals):
                try:
                    runtime.device_service.heartbeat(principal, status="idle")
                    heartbeat_count += 1
                    runtime.sync_service.status(principal)
                    sync_tracker.poll(
                        f"device_{index}",
                        lambda *, after, limit, node=principal: runtime.sync_service.pull(
                            after=after, limit=limit, principal=node),
                    )
                    sync_count += 1
                except Exception as exc:  # noqa: BLE001 - benchmark must retain phase failures
                    errors[type(exc).__name__] += 1
            next_device_cycle = now + 15
        time.sleep(min(0.25, max(0.01, end - time.monotonic())))
    window_seconds = time.monotonic() - started
    # Counts cover only the fixed-duration workload, not drain/verification.
    metrics = probe.stop()
    metric_execute_counts = {
        "sqlite_main_execute": sum(event["count"] for event in metrics["events"]
                                    if event["view"] == "sqlite_timing" and event["role"] == "main_db"
                                    and event["phase"] == "execute"),
        "sqlite_checkpoint_execute": sum(event["count"] for event in metrics["events"]
                                         if event["view"] == "sqlite_timing" and event["role"] == "checkpoint_db"
                                         and event["phase"] == "execute"),
        "mysql_main_execute": sum(event["count"] for event in metrics["events"]
                                   if event["view"] == "mysql_dbapi" and event["role"] == "main_db"
                                   and event["phase"] == "execute"),
        "mysql_checkpoint_execute": sum(event["count"] for event in metrics["events"]
                                          if event["view"] == "mysql_dbapi" and event["role"] == "checkpoint_db"
                                          and event["phase"] == "execute"),
    }
    from app.storage.sqlite_timing import close_sqlite_timing
    close_sqlite_timing(timing_recorder)
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    # Let already-enqueued jobs drain without extending the fixed workload window
    # by more than 30 seconds; this is reported separately from the window.
    drain_started = time.monotonic()
    drain_timed_out = False
    while time.monotonic() - drain_started < 30:
        conn = runtime._conn()
        try:
            pending = conn.execute("SELECT COUNT(*) FROM background_jobs WHERE status IN ('queued','running','retry_wait')").fetchone()[0]
            nonterminal_runs = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running','waiting_confirmation','waiting_user')"
            ).fetchone()[0]
        finally:
            conn.close()
        if not pending and not nonterminal_runs:
            break
        time.sleep(0.5)
    else:
        drain_timed_out = True
    drain_seconds = time.monotonic() - drain_started
    conn = runtime._conn()
    try:
        jobs = [{"kind": row[0], "status": row[1], "count": row[2]} for row in conn.execute(
            "SELECT kind,status,COUNT(*) FROM background_jobs GROUP BY kind,status ORDER BY kind,status"
        )]
        run_counts = {row[0]: row[1] for row in conn.execute(
            "SELECT status,COUNT(*) FROM agent_runs GROUP BY status"
        )}
        pending_jobs_by_status = {row[0]: row[1] for row in conn.execute(
            "SELECT status,COUNT(*) FROM background_jobs "
            "WHERE status IN ('queued','running','retry_wait') GROUP BY status"
        )}
        pending_runs = conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running','waiting_confirmation','waiting_user')"
        ).fetchone()[0]
        memory_count = conn.execute("SELECT COUNT(*) FROM memory_entries WHERE status='active'").fetchone()[0]
        intent_counts = []
        for table in ("context_records",):
            try:
                intent_counts = [row[0] for row in conn.execute(f"SELECT COUNT(*) FROM {table}")]
                break
            except Exception:  # noqa: BLE001, S112 - alternate optional table name
                continue
    finally:
        conn.close()
    runtime.stop()
    report = {
        "schema": 1, "arm": args.arm, "duration_seconds_requested": args.duration,
        "measurement_window_seconds": round(window_seconds, 3),
        "source_root_role": "old_release" if args.arm == "old-sqlite" else "current_worktree",
        "source_fingerprint_app_py_migrations": fingerprint_source(source_root),
        "data_boundary": "synthetic isolated data only; no external provider; seed excluded from metrics",
        "storage_backend": getattr(settings, "storage_backend", "sqlite"),
        "mysql_schema": args.mysql_database if args.arm == "new-mysql" else None,
        "workload": {"foreground_turns": turns, "history_reads": read_count,
                     "device_heartbeats": heartbeat_count, "sync_rounds": sync_count,
                     "seed_sessions": 50, "seed_messages": 500, "seed_memories": 100,
                     "browser_observers_on_active_session": 2,
                     "active_default_manager_session": True},
        "sync_profile": args.sync_profile,
        "sync_pages": sync_tracker.snapshot(),
        "effective_configuration": {
            "agent_orchestrator": "legacy", "agent_unified_entry_enabled": False,
            "memory_workers": 2, "extraction_debounce_seconds": 0 if args.fast_background else 5,
            "intent_debounce_seconds": 0 if args.fast_background else 30,
            "consolidation_debounce_seconds": 0 if args.fast_background else 60,
            "management_enabled": True, "management_autonomous_enabled": True,
            "default_manager_active": True,
            "history_observation_period_seconds": 15,
            "message_history_enabled": False,
        },
        "drain_seconds_after_window": round(drain_seconds, 3),
        "drain_timed_out": drain_timed_out,
        "pending_jobs_after_drain_by_status": pending_jobs_by_status,
        "pending_runs_after_drain": pending_runs,
        "python_process_resources": {
            "user_cpu_seconds_window": round(cpu_after.ru_utime - cpu_before.ru_utime, 3),
            "system_cpu_seconds_window": round(cpu_after.ru_stime - cpu_before.ru_stime, 3),
            "process_peak_rss_kib": int(cpu_after.ru_maxrss),
        },
        "mysql_server_resources": "not collected; MySQL may be shared by other isolated lab runs",
        "measurement_scope": "direct service calls only; no HTTP, browser, SSE or network latency claims",
        "final_runs_by_status": run_counts, "background_jobs_by_kind_status": jobs,
        "active_memory_count_including_seed": memory_count,
        "context_record_count": intent_counts[0] if intent_counts else None,
        "fixture_stage_counts": dict(fixture.stage_counts),
        "errors_by_type_only": dict(errors),
        "mysql_driver_exception_samples": mysql_error_snapshot() if mysql_error_snapshot else None,
        "sql_execute_counts_by_instrumentation_view": metric_execute_counts,
        "storage_metrics": metrics,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"arm": args.arm, "duration_seconds": args.duration, "turns": len(turns),
                      "stage_counts": dict(fixture.stage_counts), "runs": run_counts,
                      "jobs": jobs, "errors": dict(errors), "report": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    run(parse_args())
