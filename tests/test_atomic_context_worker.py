"""The atomic context worker stays isolated from legacy memory jobs by default."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.core.local_config import LocalAppConfig, MemoryConfig
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.sessions import SessionService
from app.storage.db import connect, init_db


def _coordinator(tmp_path, *, mode: str, profile: str):
    db_path = tmp_path / "atomic-context.sqlite3"
    init_db(db_path)
    factory = lambda: connect(db_path)
    sessions = SessionService(factory)
    session_id = sessions.create_session(title="worker test").session.session_id
    queue = BackgroundJobStore(db_path)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(
        db_path=str(db_path), memory=None, store=queue, session_service=sessions,
        atomic_context_mode=mode, atomic_context_profile=profile,
        consolidation_enabled=True,
    )
    return coordinator, queue, session_id


@pytest.mark.parametrize(
    ("mode", "profile", "expected_kinds"),
    [
        ("shadow", "general", {"context_records"}),
        ("active", "intent", {"context_records", "context_compact"}),
    ],
)
def test_context_only_worker_claims_only_atomic_context_jobs(
    tmp_path, monkeypatch, mode, profile, expected_kinds
):
    coordinator, queue, session_id = _coordinator(tmp_path, mode=mode, profile=profile)
    queue.enqueue("memory_extract", session_id, "extract", {})
    queue.enqueue("memory_consolidate", "global", "consolidate", {})
    queue.enqueue("context_compact", session_id, "compact", {"revision": 1, "target_seq": 2})
    queue.enqueue("context_records", session_id, "records", {"target_seq": 2})
    monkeypatch.setattr(
        coordinator, "recover_missing_jobs",
        lambda: pytest.fail("context-only startup must not recover legacy extraction jobs"),
    )
    recoveries = []
    original_recover = coordinator.recover_atomic_context
    monkeypatch.setattr(
        coordinator, "recover_atomic_context",
        lambda: (recoveries.append(True), original_recover())[1],
    )
    monkeypatch.setattr(coordinator.worker, "start", lambda: None)
    monkeypatch.setattr(coordinator, "_maintenance_loop", coordinator._maintenance_stop.wait)
    all_handlers = set(coordinator.worker.handlers)

    coordinator.start(context_only=True)
    coordinator.start(context_only=True)
    with pytest.raises(RuntimeError, match="different mode"):
        coordinator.start(context_only=False)
    assert set(coordinator.worker.handlers) == expected_kinds
    assert recoveries == [True]
    processed = []
    for kind in expected_kinds:
        coordinator.worker.handlers[kind] = lambda job, processed=processed: processed.append(job["kind"])
    for _ in expected_kinds:
        assert coordinator.worker.run_one()
    coordinator.stop()

    assert set(processed) == expected_kinds
    assert queue.list(kind="memory_extract", limit=1)[0]["status"] == "queued"
    assert queue.list(kind="memory_consolidate", limit=1)[0]["status"] == "queued"
    if "context_compact" not in expected_kinds:
        assert queue.list(kind="context_compact", limit=1)[0]["status"] == "queued"
    assert set(coordinator.worker.handlers) == all_handlers


def test_context_only_maintenance_runs_atomic_recovery_without_consolidation(tmp_path, monkeypatch):
    coordinator, _, _ = _coordinator(tmp_path, mode="shadow", profile="general")
    coordinator._context_only = True
    recovered = []
    monkeypatch.setattr(coordinator, "recover_atomic_context", lambda: recovered.append(True))
    monkeypatch.setattr(
        coordinator, "schedule_maintenance",
        lambda: pytest.fail("context-only maintenance must not schedule consolidation"),
    )

    class StopAfterOne:
        def __init__(self):
            self.waited = False

        def is_set(self):
            return self.waited

        def wait(self, _seconds):
            self.waited = True

    coordinator._maintenance_stop = StopAfterOne()
    coordinator._maintenance_loop()
    assert recovered == [True]


@pytest.mark.parametrize(
    ("enabled", "background", "mode", "atomic_background", "expected"),
    [
        (True, False, "active", False, None),
        (False, False, "active", True, None),
        (True, False, "off", True, None),
        (True, False, "active", True, True),
        (True, True, "active", False, False),
    ],
)
def test_runtime_starts_atomic_worker_only_when_separately_enabled(
    enabled, background, mode, atomic_background, expected
):
    config = LocalAppConfig()
    config.memory = MemoryConfig(
        enabled=enabled,
        background_enabled=background,
        atomic_context_mode=mode,
        atomic_context_background_enabled=atomic_background,
    )
    config.management.enabled = False
    config.message_history.background_enabled = False
    calls = []
    runtime = SimpleNamespace(
        local_app_config=config,
        settings=SimpleNamespace(scheduling_mode="poll"),
        cloud_replica_sync=None,
        remote_execution_service=None,
        remote_execution_worker=None,
        watch_scheduler=SimpleNamespace(start=lambda: None),
        memory_background=SimpleNamespace(start=lambda **kwargs: calls.append(kwargs.get("context_only", False))),
        message_analysis=SimpleNamespace(start=lambda: None),
        _run_startup_mail_sync=lambda: None,
        _start_background_mail_sync=lambda: None,
    )
    LocalKnowledgeAgentRuntime._start_services(runtime)
    if expected is None:
        assert calls == []
    else:
        assert calls == [expected]


def test_atomic_context_background_flag_defaults_off():
    assert MemoryConfig().atomic_context_background_enabled is False
