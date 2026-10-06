"""Small offline integration checks; no platform or real provider traffic."""
from __future__ import annotations

import json

from test_message_analysis import IDENTITY, SyntheticModel, imports, pipeline

from app.core.local_config import MessageHistoryConfig
from app.core.message_analysis import MessageAnalysisCoordinator


def drain(worker):
    for _ in range(100):
        if not worker.run_one():
            return
    raise AssertionError("unbounded synthetic work")


def test_one_call_per_quantum_checkpoint_survives_restart(tmp_path):
    model = SyntheticModel()
    service, store, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1, chunk_bytes=1024)
    imports(service, 1, text="hello🙂" * 200)
    assert coordinator.worker.run_one()
    assert len(model.requests) == 1
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0
    job = store.list()[0]
    assert job["status"] == "queued" and job["attempts"] == 0
    restarted = MessageAnalysisCoordinator(service=service, store=store, config=coordinator.config, llm_client=model)
    drain(restarted.worker)
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1
    assert len(model.requests) == len(coordinator._chunks(service.history(policy["conversation_key"])["messages"]))


def test_pause_during_call_fences_output_and_resume_keeps_checkpoint(tmp_path):
    model = SyntheticModel()
    service, _store, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1, chunk_bytes=1024)
    imports(service, 1, text="message🙂" * 160)
    assert coordinator.worker.run_one()
    with service._connection() as conn:
        before = dict(conn.execute("SELECT * FROM message_reading_checkpoints").fetchone())
    model.hook = lambda: service.set_reading_paused(True, service.reading_status()["revision"])
    assert coordinator.worker.run_one()
    assert service.reading_status()["paused"]
    with service._connection() as conn:
        assert dict(conn.execute("SELECT * FROM message_reading_checkpoints").fetchone()) == before
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0
    assert imports(service, 2)["acknowledged"]  # Recording remains active and duplicates are harmless.
    model.hook = None
    service.set_reading_paused(False, service.reading_status()["revision"])
    drain(coordinator.worker)
    assert service.summary(policy["conversation_key"])["covered_seq"] >= 1


def test_format_recovery_requeues_and_is_counted_once(tmp_path):
    class FirstBad(SyntheticModel):
        async def complete_text(self, **kwargs):
            result = await super().complete_text(**kwargs)
            if len(self.requests) == 1:
                result.content = "not-json"
            return result
    model = FirstBad()
    service, store, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1)
    imports(service, 1)
    assert coordinator.worker.run_one() and len(model.requests) == 1
    assert store.list()[0]["status"] == "queued"
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0
    assert coordinator.worker.run_one() and len(model.requests) == 2
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1
    family = store.get(store.list()[0]["job_id"], include_payload=True)["payload"]["work_family_id"]
    assert coordinator.controller.quota_usage(family)["total_calls"] == 2


def test_second_chunk_cannot_obtain_a_second_format_recovery(tmp_path):
    class BadTwice(SyntheticModel):
        async def complete_text(self, **kwargs):
            result = await super().complete_text(**kwargs)
            if len(self.requests) in (1, 3):
                result.content = "not-json"
            return result
    model = BadTwice()
    service, store, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1, chunk_bytes=1024)
    imports(service, 1, text="fragment" * 300)
    drain(coordinator.worker)
    assert len(model.requests) == 3
    assert store.list(status="failed")[0]["error_class"] == "model_output_invalid"
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0


def test_retry_cannot_dispatch_an_exhausted_recovery_again(tmp_path):
    class AlwaysBad(SyntheticModel):
        async def complete_text(self, **kwargs):
            result = await super().complete_text(**kwargs)
            result.content = "not-json"
            return result
    model = AlwaysBad()
    service, store, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1)
    imports(service, 1)
    drain(coordinator.worker)
    assert len(model.requests) == 2
    failed = store.list(status="failed")[0]
    assert service.retry_analysis(policy["conversation_key"], failed["updated_at"])["status"] == "retried"
    assert coordinator.worker.run_one()
    assert len(model.requests) == 2 and store.list(status="failed")


def test_rolling_call_quota_defers_without_discarding_progress(tmp_path):
    model = SyntheticModel()
    service, store, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1, chunk_bytes=1024)
    coordinator.config.conversation_hourly_call_limit = 1
    imports(service, 1, text="fragment" * 300)
    assert coordinator.worker.run_one()
    assert coordinator.worker.run_one()
    assert len(model.requests) == 1
    assert store.list(status="retry_wait")[0]["error_class"] == "background_budget_deferred"
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0
    with service._connection() as conn:
        assert json.loads(conn.execute("SELECT cursor_json FROM message_reading_checkpoints").fetchone()[0]) == 1


def test_default_planner_bounds_full_input_and_shrinks_range(tmp_path):
    service, store, policy, _ = pipeline(tmp_path, batch_size=1)
    policy = service.set_policy({**IDENTITY, "expected_revision": policy["revision"], "max_batch_messages": 200})
    coordinator = MessageAnalysisCoordinator(service=service, store=store,
        config=MessageHistoryConfig(yield_delay_seconds=0), llm_client=SyntheticModel())
    imports(service, 20, text="data" * 150)
    queued = store.list()[0]
    payload = store.get(queued["job_id"], include_payload=True)["payload"]
    assert 0 < payload["end_seq"] < 20
    drain(coordinator.worker)
    assert service.summary(policy["conversation_key"])["covered_seq"] == payload["end_seq"]
