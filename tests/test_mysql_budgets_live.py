"""Persistent quota safety across independent controllers and worker threads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from app.core.llm_workloads import (
    BudgetQuota,
    LLMWorkloadController,
    UsageTicket,
    WorkBudgetExceeded,
    Workload,
)
from app.storage.connections import configure_mysql, unregister_mysql
from app.storage.db import init_db
from tests.test_mysql_owner_live import _config


@pytest.fixture
def controllers(tmp_path):
    config = _config()
    path = tmp_path / "logical-mysql.sqlite3"
    configure_mysql(path, config)
    try:
        init_db(path)
        first, second = LLMWorkloadController(path), LLMWorkloadController(path)
        assert not path.exists()
        yield first, second
    finally:
        unregister_mysql(path, expected=config)


def test_shared_quota_never_oversells_across_pools_and_controllers(controllers):
    scope = "test-quota-" + uuid4().hex
    quota = BudgetQuota(scope, max_tokens=45, max_calls=3)
    barrier = Barrier(8, timeout=5)
    def reserve(index):
        controller = controllers[index % 2]
        ticket = UsageTicket("test-call-" + uuid4().hex,
                             Workload(pool="interactive" if index % 2 else "background_agent", quotas=(quota,)), 10, 5)
        barrier.wait()
        try:
            controller._reserve(ticket)
            return controller, ticket
        except WorkBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=8) as workers:
        accepted = [item for item in workers.map(reserve, range(8)) if item is not None]
    assert len(accepted) == 3
    totals = controllers[0].quota_usage(scope)
    assert (totals["total_tokens"], totals["total_calls"]) == (45, 3)
    def finish(item):
        controller, ticket = item
        ticket.dispatched = True
        ticket.usage = {"prompt_tokens": 6, "completion_tokens": 3}
        controller._finish(ticket)
    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(finish, accepted))
    totals = controllers[1].quota_usage(scope)
    assert (totals["total_tokens"], totals["total_calls"]) == (27, 3)
    # Settlement retry must not charge the same call twice.
    finish(accepted[0])
    assert controllers[0].quota_usage(scope)["total_tokens"] == 27


def test_aborted_undispatched_reservation_returns_its_allowance(controllers):
    controller = controllers[0]
    scope = "test-quota-" + uuid4().hex
    ticket = UsageTicket("test-call-" + uuid4().hex,
                         Workload(quotas=(BudgetQuota(scope, max_calls=1),)), 7, 3)
    controller._reserve(ticket)
    controller._finish(ticket)
    totals = controller.quota_usage(scope)
    assert (totals["total_tokens"], totals["total_calls"]) == (0, 0)
