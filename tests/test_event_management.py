"""Management's event-mode timer and post-commit signal contracts."""
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.experts.management import ManagementCoordinator
from app.storage.commit_signals import notify_after_commit


class Scheduler:
    def __init__(self):
        self.registrations = {}
        self.timers = []
        self.notifications = []
        self.reconcile_seconds = 30

    def register(self, namespace, handler):
        self.registrations[namespace] = handler

    def schedule(self, namespace, key, delay):
        self.timers.append((namespace, key, delay))

    def notify(self, namespace, key="*"):
        self.notifications.append((namespace, key))


def test_attach_event_scheduler_registers_handler_and_does_not_start_polling():
    scheduler = Scheduler()
    coordinator = ManagementCoordinator.__new__(ManagementCoordinator)
    coordinator.management = coordinator.goals = object()
    coordinator.worker = SimpleNamespace(configure_scheduling=lambda mode, _seconds: setattr(coordinator, "worker_mode", mode))
    coordinator.attach_scheduler(scheduler, "event")
    assert coordinator._scheduling_mode == "event"
    assert coordinator.worker_mode == "event"
    assert "management" in scheduler.registrations


def test_tick_notifies_worker_only_after_tick_result_reports_committed_work():
    coordinator = ManagementCoordinator.__new__(ManagementCoordinator)
    seen = []
    coordinator.worker = SimpleNamespace(notify=lambda: seen.append("worker"))
    coordinator._tick_impl = lambda _now: {"scheduled": 1}
    coordinator._schedule_next_due = lambda _now: None
    assert coordinator.tick(datetime.now(UTC))["scheduled"] == 1
    assert seen == ["worker"]
    coordinator._tick_impl = lambda _now: {"scheduled": 0}
    coordinator.tick(datetime.now(UTC))
    assert seen == ["worker"]


def test_due_timer_uses_utc_delay_and_skips_already_due_to_prevent_spin():
    now = datetime(2026, 10, 26, tzinfo=UTC)
    scheduler = Scheduler()
    coordinator = ManagementCoordinator.__new__(ManagementCoordinator)
    coordinator._scheduler, coordinator._scheduling_mode = scheduler, "event"
    coordinator.config = SimpleNamespace(min_wake_interval_seconds=0, max_daily_rounds=None)
    due = now + timedelta(seconds=45)
    coordinator._conn = lambda: _Connection({"manager_goals": due.isoformat()})
    coordinator.goals = SimpleNamespace(_has_table=lambda _conn, name: name == "manager_goals")
    coordinator._schedule_next_due(now)
    assert scheduler.timers == [("management", "*", 45.0)]
    scheduler.timers.clear()
    coordinator._conn = lambda: _Connection({"manager_goals": (now - timedelta(seconds=1)).isoformat()})
    coordinator._schedule_next_due(now)
    assert scheduler.timers == []


def test_old_blocked_goal_does_not_mask_a_future_goal_due():
    now = datetime(2026, 10, 26, tzinfo=UTC)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE manager_goals(status TEXT, next_wake_at TEXT)")
    conn.executemany("INSERT INTO manager_goals VALUES('active', ?)", [
        ((now - timedelta(days=1)).isoformat(),),
        ((now + timedelta(minutes=3)).isoformat(),),
    ])
    coordinator = ManagementCoordinator.__new__(ManagementCoordinator)
    coordinator.goals = SimpleNamespace(_has_table=lambda _conn, name: name == "manager_goals")
    coordinator.config = SimpleNamespace(min_wake_interval_seconds=0, max_daily_rounds=None)
    assert coordinator._future_management_due(conn, now) == now + timedelta(minutes=3)
    conn.close()


class _Connection:
    def __init__(self, values):
        self.values = values

    def execute(self, query, _params=()):
        table = "manager_goals" if "manager_goals" in query else (
            "manager_schedules" if "manager_schedules" in query else "manager_attention_sweep_state")
        return _Row(self.values.get(table))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Row:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


def test_notify_after_commit_skips_external_connection_methods():
    seen = []

    class Service:
        on_change = lambda self: seen.append("notify")

        @notify_after_commit
        def own_transaction(self):
            return "committed"

        @notify_after_commit
        def caller_transaction(self, conn):
            return "pending"

        @notify_after_commit
        def optional_connection(self, conn=None):
            return "committed"

    service = Service()
    assert service.own_transaction() == "committed"
    assert service.caller_transaction(object()) == "pending"
    assert seen == ["notify"]
    assert service.optional_connection() == "committed"
    assert seen == ["notify", "notify"]


def test_post_commit_notifier_failure_does_not_change_committed_result():
    class Service:
        def on_change(self):
            raise RuntimeError("signal unavailable")

        @notify_after_commit
        def write(self):
            return "accepted"

    assert Service().write() == "accepted"


def test_run_state_notification_is_memory_only_and_keyed_by_run():
    scheduler = Scheduler()
    coordinator = ManagementCoordinator.__new__(ManagementCoordinator)
    coordinator._scheduler, coordinator._scheduling_mode = scheduler, "event"
    coordinator._conn = lambda: (_ for _ in ()).throw(AssertionError("notification must not query storage"))
    coordinator.notify_run_state(SimpleNamespace(run_id="run-1"))
    assert scheduler.notifications == [("management", "run:run-1")]


def test_targeted_run_handler_limits_capture_and_enqueue_to_affected_manager(tmp_path):
    path = tmp_path / "targeted-management.sqlite3"

    def connection():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    with closing(connection()) as conn:
        conn.executescript("""
            CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY, kind TEXT, status TEXT);
            CREATE TABLE agent_runs(run_id TEXT PRIMARY KEY, session_id TEXT);
            CREATE TABLE managed_session_links(manager_session_id TEXT, task_session_id TEXT, run_id TEXT);
            CREATE TABLE manager_goals(session_id TEXT, wait_run_id TEXT, status TEXT);
            CREATE TABLE manager_delegates(parent_session_id TEXT, session_id TEXT, state TEXT);
        """)
        conn.executemany("INSERT INTO agent_sessions VALUES(?, 'manager', 'active')",
                         [(f"manager-{i}",) for i in range(100)])
        conn.execute("INSERT INTO agent_sessions VALUES('task-session', 'ordinary', 'active')")
        conn.execute("INSERT INTO agent_runs VALUES('run-target', 'task-session')")
        conn.execute("INSERT INTO managed_session_links VALUES('manager-37', 'task-session', 'run-target')")
        conn.commit()

    calls = []
    coordinator = ManagementCoordinator.__new__(ManagementCoordinator)
    coordinator._conn = connection
    coordinator.goals = SimpleNamespace(
        _has_table=lambda conn, table: conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None,
        capture_events=lambda **kwargs: calls.append(("goals", kwargs["session_ids"])),
    )
    coordinator.delegates = SimpleNamespace(
        capture_events=lambda **kwargs: calls.append(("delegates", kwargs["session_ids"]))
    )
    coordinator.work_queue = SimpleNamespace(tick=lambda **_kwargs: calls.append(("queue", None)))
    coordinator.reconcile = lambda *, session_ids: calls.append(("reconcile", session_ids))
    coordinator._enqueue_pending_sessions = lambda session_ids, _now: calls.append(("enqueue", session_ids)) or 0
    coordinator._handle_run_state("run-target")
    assert calls == [
        ("queue", None),
        ("goals", {"manager-37"}),
        ("delegates", {"manager-37"}),
        ("reconcile", {"manager-37"}),
        ("enqueue", {"manager-37"}),
    ]
