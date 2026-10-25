"""Idle management polling must not reserve SQLite's writer lock."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.local_config import ManagementConfig
from app.domains.management_goals import ManagementGoalService
from app.domains.management_queue import ManagementQueueService
from app.domains.management_schedules import ManagementScheduleService
from app.domains.management_supervision import ManagementSupervisionService
from tests.test_management_goals import state as goal_state


def test_idle_queue_schedule_and_supervision_ticks_skip_write_transactions(tmp_path):
    factory, _, management, goals, sid = goal_state.__wrapped__(tmp_path)
    statements = []

    def traced_factory():
        conn = factory()
        conn.set_trace_callback(statements.append)
        return conn

    goals = ManagementGoalService(traced_factory, management)
    goals.ensure_schema()
    config = ManagementConfig(enabled=True, autonomous_enabled=True,
                              attention_sweep_interval_seconds=60)
    tasks = SimpleNamespace(durable_execution=lambda: True)
    queue = ManagementQueueService(traced_factory, tasks=tasks, assignments=None,
                                   dispatcher=None, config=config)
    queue.ensure_schema()
    schedules = ManagementScheduleService(traced_factory, goals, management)
    schedules.ensure_schema()
    supervision = ManagementSupervisionService(traced_factory, goals=goals,
                                               management=management, config=config)
    supervision.ensure_schema()
    goals.create_goal(sid, objective="Active but not due")
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)

    # The first supervision pulse only initializes its interval watermark.
    assert supervision.tick(now) == 0
    statements.clear()
    assert queue.tick(now) == 0
    assert schedules.tick(now) == 0
    assert supervision.tick(now + timedelta(seconds=59)) == 0
    assert not [sql for sql in statements if sql.strip().upper().startswith("BEGIN IMMEDIATE")]
