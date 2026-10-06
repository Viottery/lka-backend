"""Configuration activation fences analysis without changing capture consent."""
import pytest
from test_message_protocol import policy, v1

from app.core.background_jobs import BackgroundJobStore
from app.domains.message_history import MessageHistoryService
from app.domains.message_reading_configuration import bind_reading_configuration

PROVIDER = {"name": "local", "provider": "compatible", "base_url": "https://example.invalid/v1"}


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "configuration.sqlite"
    jobs = BackgroundJobStore(path)
    service = MessageHistoryService(path, jobs)
    service.ensure_schema()
    return service, jobs


def bind(service, provider=PROVIDER, model="model-a"):
    bind_reading_configuration(service, provider=provider,
                               processing={"model": model, "prompt_version": "v1", "schema_version": 1})


def test_first_bind_and_identical_restart_preserve_legacy_grants_and_jobs(store):
    service, _ = store
    p = policy(service, record_enabled=True, analysis_enabled=True, batch_size=1)
    service.import_messages([v1()])
    bind(service)
    bind(service)
    assert service.list_policies()[0] == p
    with service._connection() as conn:
        assert conn.execute("SELECT status FROM background_jobs").fetchone()[0] == "queued"
        row = dict(conn.execute("SELECT * FROM message_reading_configuration").fetchone())
    assert len(row["provider_hash"]) == len(row["processing_hash"]) == 64
    assert "example.invalid" not in str(row)


def test_model_change_cancels_old_work_but_preserves_capture_and_consent(store):
    service, _ = store
    p = policy(service, record_enabled=True, analysis_enabled=True, batch_size=1)
    service.import_messages([v1()])
    bind(service)
    bind(service, model="model-b")
    q = service.list_policies()[0]
    for name in ("capture_epoch", "analysis_epoch", "schedule_revision", "record_enabled", "analysis_enabled"):
        assert q[name] == p[name]
    assert q["processing_revision"] == p["processing_revision"] + 1
    assert q["revision"] == p["revision"] + 1
    with service._connection() as conn:
        assert conn.execute("SELECT status FROM background_jobs").fetchone()[0] == "cancelled"
    bind(service, model="model-b")
    assert service.list_policies()[0] == q


def test_provider_change_requires_new_analysis_consent_not_new_recording(store):
    service, _ = store
    p = policy(service, record_enabled=True, analysis_enabled=True, batch_size=1)
    service.import_messages([v1()])
    bind(service)
    bind(service, provider={**PROVIDER, "base_url": "https://other.invalid/v1"})
    q = service.list_policies()[0]
    assert q["record_enabled"] and not q["analysis_enabled"]
    assert q["capture_epoch"] == p["capture_epoch"]
    assert q["analysis_epoch"] == p["analysis_epoch"] + 1
    assert q["processing_revision"] == p["processing_revision"] + 1
    assert not service.schedule_pending(q["conversation_key"])
    assert service.import_messages([v1("new")])["acknowledged"]
    renewed = policy(service, q, analysis_enabled=True)
    assert renewed["analysis_epoch"] == q["analysis_epoch"] + 1
    assert renewed["capture_epoch"] == p["capture_epoch"]
