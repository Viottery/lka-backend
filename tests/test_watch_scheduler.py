from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.instruction_files import InstructionFiles
from app.core.multi_agent import TaskResultStatus
from app.core.sessions import SessionService
from app.core.watch_execution import WatchExecutionResult
from app.core.watch_scheduler import WatchScheduler, latest_due_slot
from app.domains.watch import WatchInput, WatchPatch, WatchService
from app.storage.db import connect, get_db_path, init_db


class _Adapter:
    async def run_async(self, **kwargs):
        return WatchExecutionResult(
            status=TaskResultStatus.COMPLETED,
            summary="Official page reports that tickets are available.",
            run_id=kwargs["parent_run_id"],
            child_run_id="child",
            snapshot_id="snapshot",
            evidence_refs=("ev-1",),
            evidence_sources=("https://example.org/tickets",),
        )


def test_latest_due_slot_only_checks_latest_calendar_date(tmp_path):
    service = WatchService(lambda: connect(get_db_path(tmp_path / "data")))
    service.initialize()
    watch = service.create(
        WatchInput(
            title="Tickets",
            goal="Monitor tickets",
            timezone="Asia/Shanghai",
            daily_time="08:00",
            categories=["web"],
        ),
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert latest_due_slot(watch, datetime(2026, 10, 1, 1, tzinfo=UTC)) == datetime(
        2026, 10, 1, 0, tzinfo=UTC
    )
    assert latest_due_slot(watch, datetime(2026, 10, 1, 0, tzinfo=UTC)) == datetime(
        2026, 10, 1, 0, tzinfo=UTC
    )


def test_scheduler_creates_one_occurrence_and_briefing_without_blocking_api(tmp_path):
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    service = WatchService(lambda: connect(db_path))
    service.initialize()
    runtime = SimpleNamespace(
        agent_run_manager=InMemoryAgentRunManager(),
        session_service=SessionService(lambda: connect(db_path)),
    )
    now = datetime.now(UTC)
    watch = service.create(
        WatchInput(
            title="Tickets",
            goal="Monitor official tickets",
            timezone="UTC",
            daily_time=(now - timedelta(minutes=2)).strftime("%H:%M"),
            categories=["web"],
            scope={"web_enabled": True},
        ),
        now=now - timedelta(days=2),
    )
    scheduler = WatchScheduler(runtime, service, adapter=_Adapter(), worker_count=0)
    assert scheduler.enqueue_due(now=now) == 1
    assert scheduler.enqueue_due(now=now) == 1  # same pending slot, not a second row
    assert len(service.list_occurrences(watch["watch_id"])) == 1
    assert scheduler.run_one(owner="test-worker") is True
    assert scheduler.run_one(owner="test-worker") is False
    runs = service.list_occurrences(watch["watch_id"])
    assert runs[0]["status"] == "succeeded"
    briefings = service.list_briefings(watch_id=watch["watch_id"])
    assert len(briefings) == 1
    assert briefings[0]["evidence"][0]["ref"] == "https://example.org/tickets"
    assert briefings[0]["session_id"] == runs[0]["session_id"]
    detail = runtime.session_service.get_session(session_id=briefings[0]["session_id"])
    assert detail.messages[0].content.startswith("请跟进：")
    assert detail.messages[-1].content == briefings[0]["summary"]
    second = service.create_occurrence(watch["watch_id"], now - timedelta(days=1))
    assert scheduler.run_one(owner="test-worker") is True
    newer = service.list_briefings(watch_id=watch["watch_id"])
    assert len(newer) == 2
    assert second["session_id"] != briefings[0]["session_id"]
    assert {item["session_id"] for item in newer} == {
        second["session_id"],
        briefings[0]["session_id"],
    }


def test_unconfigured_web_provider_fails_watch_instead_of_fabricating_briefing(tmp_path):
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    service = WatchService(lambda: connect(db_path))
    service.initialize()
    runtime = SimpleNamespace(
        agent_run_manager=InMemoryAgentRunManager(),
        session_service=SessionService(lambda: connect(db_path)),
        local_app_config=SimpleNamespace(
            web_search=SimpleNamespace(resolved_api_key=lambda: None),
        ),
    )
    watch = service.create(
        WatchInput(
            title="News",
            goal="Check news",
            timezone="UTC",
            daily_time="08:00",
            categories=["news"],
        ),
        now=datetime.now(UTC) - timedelta(days=2),
    )
    scheduler = WatchScheduler(runtime, service, adapter=_Adapter(), worker_count=0)
    occurrence = scheduler.enqueue_now(watch["watch_id"])
    assert scheduler.run_one(owner="test-worker") is True
    assert service.list_occurrences(watch["watch_id"])[0]["status"] == "failed"
    assert service.list_briefings(watch_id=watch["watch_id"]) == []
    detail = runtime.session_service.get_session(session_id=occurrence["session_id"])
    assert detail.messages[-1].payload["status"] == "failed"


def test_watch_run_reads_latest_global_watch_guidance(tmp_path):
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    service = WatchService(lambda: connect(db_path))
    service.initialize()
    files = InstructionFiles(tmp_path / "data", [])
    files.initialize()
    initial = files.read("watch")
    files.update(
        "watch",
        content="# Watch\nPrefer primary sources.\n" + "x" * 20_000,
        expected_sha256=initial["sha256"],
    )
    runtime = SimpleNamespace(
        agent_run_manager=InMemoryAgentRunManager(),
        session_service=SessionService(lambda: connect(db_path)),
        instruction_files=files,
    )
    watch = service.create(
        WatchInput(
            title="Tickets",
            goal="Check tickets",
            timezone="UTC",
            daily_time="08:00",
            categories=["web"],
            scope={"web_enabled": True},
        ),
        now=datetime.now(UTC),
    )

    class CapturingAdapter(_Adapter):
        goal: str | None = None

        async def run_async(self, **kwargs):
            self.goal = kwargs["goal"]
            return await super().run_async(**kwargs)

    adapter = CapturingAdapter()
    scheduler = WatchScheduler(runtime, service, adapter=adapter, worker_count=0)
    scheduler.enqueue_now(watch["watch_id"])
    assert scheduler.run_one(owner="test-worker")
    assert "Prefer primary sources" in adapter.goal
    assert "Guidance continuation:" in adapter.goal


def test_watch_updated_after_enqueue_does_not_run_with_old_scope(tmp_path):
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    service = WatchService(lambda: connect(db_path))
    service.initialize()
    runtime = SimpleNamespace(
        agent_run_manager=InMemoryAgentRunManager(),
        session_service=SessionService(lambda: connect(db_path)),
    )
    watch = service.create(
        WatchInput(
            title="News",
            goal="Check source",
            timezone="UTC",
            daily_time="08:00",
            categories=["web"],
            scope={"web_enabled": True},
        ),
        now=datetime.now(UTC),
    )
    adapter = _Adapter()
    scheduler = WatchScheduler(runtime, service, adapter=adapter, worker_count=0)
    scheduler.enqueue_now(watch["watch_id"])
    service.update(
        watch["watch_id"], WatchPatch(scope={"web_enabled": False, "source_ids": ["source-1"]})
    )
    assert scheduler.run_one(owner="test-worker") is False
    assert service.list_briefings(watch_id=watch["watch_id"]) == []
