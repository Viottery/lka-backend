"""Exact publication aliases; real remote-extraction worker and isolated SQLite."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.memory_extraction import MemoryCandidate, extract_user_memories
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.domains.memory import (
    MemoryInput,
    MemoryPublicationLeaseError,
    MemoryPublicationSuppressed,
    MemorySourceInput,
)

TEXT = "我倾向在比较方案时先看到取舍理由再看结论。"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = LocalAppConfig()
    config = config.model_copy(update={"memory": config.memory.model_copy(update={
        "enabled": True, "background_enabled": True, "allow_remote_extraction": True,
        "extraction_debounce_seconds": 0})})
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    instance = LocalKnowledgeAgentRuntime(Settings(LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path)))
    yield instance
    instance.stop()


def user_source(runtime, text=TEXT, *, trusted=False, source_type="user_message", role="user", checksum=None):
    session = runtime.session_service.create_session().session
    user = runtime.session_service.append_message(session_id=session.session_id, role=role, content=text)
    source = runtime.memory_service.register_source(MemorySourceInput(
        source_type=source_type, source_ref=user.message_id, trusted_source=trusted,
        checksum=checksum if checksum is not None else hashlib.sha256(text.encode()).hexdigest()))
    return user, source


def lease(runtime):
    store = runtime.background_job_store
    job = store.enqueue("memory_extract", "publication", "publication", {})
    claimed = store.claim("alias-test", 600)
    assert job["job_id"] == claimed["job_id"]
    return claimed["job_id"], claimed["lease_owner"], claimed["lease_epoch"]


def publish(runtime, source, claim, *, evidence=TEXT, publication_lease=None, **kwargs):
    scope, project = kwargs.get("scope", "global"), kwargs.get("project_id")
    payload = MemoryInput(content=claim, memory_type=kwargs.pop("memory_type", "preference"),
        confidence=.9, source_id=source, extraction_model=kwargs.pop("extraction_model", "configured_llm_v1"),
        metadata={"evidence": evidence},
        dedupe_key=hashlib.sha256(f"{scope}|{project}|{claim.casefold()}".encode()).hexdigest(), **kwargs)
    return runtime.memory_service.create(payload, publication_lease=publication_lease)


def exchange(runtime):
    session = runtime.session_service.create_session().session
    run = runtime.agent_run_manager.create_run(session_id=session.session_id, user_input=TEXT)
    runtime.agent_run_manager.mark_running(run.run_id)
    runtime.session_service.append_message(session_id=session.session_id, role="user", content=TEXT,
                                          payload={"trace_id": run.trace_id})
    runtime.session_service.append_message(session_id=session.session_id, role="agent", content="已了解。",
        payload={"trace_id": run.trace_id, "run_id": run.run_id},
        persisted_message_callback=runtime._enqueue_memory_answer)
    runtime.agent_run_manager.complete_run(run.run_id, result_snapshot={"answer": "已了解。"})


@pytest.mark.parametrize("first", [TEXT, TEXT[:-1]])
def test_actual_remote_worker_terminal_period_alias_promotes_same_old_id(runtime, first):
    assert extract_user_memories(source_id="precheck", content=TEXT) == []

    class Client:
        calls = 0

        def complete_text(self, **kwargs):
            self.calls += 1
            claim = first if self.calls == 1 else (TEXT[:-1] if first == TEXT else TEXT)
            return SimpleNamespace(content=json.dumps({"candidates": [{"claim": claim,
                "evidence": TEXT, "kind": "preference", "confidence": .9, "explicit": True}]}, ensure_ascii=False))

    client = Client()
    runtime.memory_background.llm_client = client
    exchange(runtime)
    assert runtime.memory_background.worker.run_one()
    old = runtime.memory_service.list(statuses=("candidate",))[0]
    exchange(runtime)
    assert runtime.memory_background.worker.run_one()
    records = runtime.memory_service.list(statuses=("active", "candidate"))
    assert len(records) == 1 and records[0].memory_id == old.memory_id and records[0].status == "active"
    assert records[0].content == first and records[0].metadata["evidence"] == TEXT
    assert len(records[0].source_ids) == 2 and client.calls == 2
    audit = runtime.memory_service.events(old.memory_id)[-1]["payload"]
    assert audit["incoming_claim"] == (TEXT[:-1] if first == TEXT else TEXT)
    assert audit["incoming_evidence"] == TEXT


def test_unique_legacy_candidate_id_and_display_survive_alias_and_same_source_retry(runtime):
    _, first = user_source(runtime)
    _, second = user_source(runtime)
    old = publish(runtime, first, TEXT)  # Unleased legacy publication; no alias lookup.
    owner = lease(runtime)
    new = publish(runtime, second, TEXT[:-1], publication_lease=owner)
    assert new.memory_id == old.memory_id and new.content == TEXT and new.status == "active"
    version = new.version
    again = publish(runtime, second, TEXT[:-1], publication_lease=owner)
    assert again.version == version and len(again.source_ids) == 2


def test_same_source_cannot_promote_by_period_variation(runtime):
    _, source = user_source(runtime)
    owner = lease(runtime)
    old = publish(runtime, source, TEXT, publication_lease=owner)
    repeat = publish(runtime, source, TEXT[:-1], publication_lease=owner)
    assert repeat.memory_id == old.memory_id and repeat.version == old.version and repeat.status == "candidate"


def test_concurrent_opposite_period_variants_create_one_identity(runtime):
    sources = [user_source(runtime)[1] for _ in range(2)]
    owner = lease(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, runtime, s, claim, publication_lease=owner)
                   for s, claim in zip(sources, (TEXT, TEXT[:-1]), strict=True)]
        records = [f.result() for f in futures]
    assert len({r.memory_id for r in records}) == 1
    final = runtime.memory_service.get(records[0].memory_id)
    assert final.status == "active" and len(final.source_ids) == 2


def test_two_legacy_candidates_are_ambiguous_not_migrated_or_third_created(runtime):
    ids = [publish(runtime, user_source(runtime)[1], c).memory_id for c in (TEXT, TEXT[:-1])]
    _, source = user_source(runtime)
    with pytest.raises(MemoryPublicationSuppressed, match="publication_alias_ambiguous"):
        publish(runtime, source, TEXT, publication_lease=lease(runtime))
    records = runtime.memory_service.list(statuses=("candidate",))
    assert {m.memory_id for m in records} == set(ids)
    assert all(m.version == 1 and len(m.source_ids) == 1 for m in records)


@pytest.mark.parametrize("action", ["retract", "supersede", "revoke", "ambiguous_with_tombstone"])
def test_terminal_alias_cannot_relearn_tombstone(runtime, action):
    _, first = user_source(runtime)
    old = publish(runtime, first, TEXT)
    if action == "ambiguous_with_tombstone":
        publish(runtime, user_source(runtime)[1], TEXT[:-1])
    if action == "revoke":
        runtime.memory_service.revoke_source(first)
    elif action == "supersede":
        runtime.memory_service.correct(old.memory_id, content="我倾向先核对来源。", expected_version=old.version)
    else:
        runtime.memory_service.retract(old.memory_id, expected_version=old.version)
    before = runtime.memory_service.export()["memories"]
    _, second = user_source(runtime)
    with pytest.raises(MemoryPublicationSuppressed, match="publication_alias_tombstone"):
        publish(runtime, second, TEXT[:-1], publication_lease=lease(runtime))
    assert runtime.memory_service.export()["memories"] == before


def test_exact_source_correction_fence_and_lease_win_before_alias(runtime):
    _, first = user_source(runtime)
    old = publish(runtime, first, TEXT)
    _, queued = user_source(runtime)
    correction, _ = user_source(runtime, "我不再保留这个偏好。")
    runtime.memory_service.fence_prior_publication(source_message_id=correction.message_id,
        user_input=correction.content, selectors=[TEXT], hints=[], project_id=None)
    owner = lease(runtime)
    with pytest.raises(MemoryPublicationSuppressed, match="Earlier source"):
        publish(runtime, queued, TEXT[:-1], publication_lease=owner)
    with pytest.raises(MemoryPublicationLeaseError):
        publish(runtime, queued, TEXT[:-1], publication_lease=(owner[0], "foreign", owner[2]))
    assert runtime.memory_service.get(old.memory_id).version == old.version


def test_actual_coordinator_forwards_publication_lease_not_ignored_input_extra(runtime):
    user, source = user_source(runtime)
    owner = lease(runtime)
    candidate = MemoryCandidate(claim=TEXT, evidence=TEXT, kind="preference",
                                source_id=user.message_id, explicit=True, confidence=.9)
    with pytest.raises(MemoryPublicationLeaseError, match="publication_lease_fenced"):
        runtime.memory_background._publish_candidate(candidate, {"content": TEXT}, False,
            "global", None, source, {"job_id": owner[0], "lease_owner": "foreign", "lease_epoch": owner[2]})
    assert runtime.memory_service.list(statuses=("active", "candidate")) == []


@pytest.mark.parametrize("left,right", [
    ("我倾向先核对来源；再给结论。", "我倾向先核对来源，再给结论"),
    ("我倾向不要自动执行。", "我倾向自动执行"),
    ("我倾向保留版本3.14。", "我倾向保留版本314"),
    ("我倾向采用预算10。", "我倾向采用预算100"),
    ("我倾向使用目录/a/b。", "我倾向使用目录/ab"),
    ('我倾向保留标签"alpha"。', "我倾向保留标签alpha"),
    ("我倾向先核对来源。。", "我倾向先核对来源。"),
])
def test_internal_punctuation_negation_numbers_paths_quotes_are_not_aliases(runtime, left, right):
    owner = lease(runtime)
    old = publish(runtime, user_source(runtime, left)[1], left, evidence=left, publication_lease=owner)
    current = publish(runtime, user_source(runtime, right)[1], right, evidence=right, publication_lease=owner)
    assert old.memory_id != current.memory_id
    assert runtime.memory_service.get(old.memory_id).content == left
    assert current.content == right and current.status == "candidate"


@pytest.mark.parametrize("boundary", ["scope", "project", "type", "trusted", "source_type", "origin", "evidence", "checksum", "foreign_role"])
def test_alias_never_crosses_scope_type_source_authority_or_evidence(runtime, boundary):
    first_kwargs, second_kwargs = {}, {}
    source_kwargs = {}
    if boundary == "scope":
        first_kwargs = {"scope": "project", "project_id": runtime.memory_service.resolve_project("/synthetic/a")}
    elif boundary == "project":
        first_kwargs = {"scope": "project", "project_id": runtime.memory_service.resolve_project("/synthetic/a")}
        second_kwargs = {"scope": "project", "project_id": runtime.memory_service.resolve_project("/synthetic/b")}
    elif boundary == "type":
        second_kwargs["memory_type"] = "user_fact"
    elif boundary in {"trusted", "source_type"}:
        source_kwargs = {"trusted": True} if boundary == "trusted" else {"source_type": "conversation"}
    elif boundary == "origin":
        first_kwargs["extraction_model"] = "local_direct_preference_v1"
    elif boundary == "evidence":
        second_kwargs["evidence"] = TEXT[:-1]
    elif boundary == "checksum":
        source_kwargs = {"checksum": "not_the_source_hash"}
    elif boundary == "foreign_role":
        source_kwargs = {"role": "agent"}
    old = publish(runtime, user_source(runtime, **source_kwargs)[1], TEXT, **first_kwargs)
    new = publish(runtime, user_source(runtime)[1], TEXT[:-1], publication_lease=lease(runtime), **second_kwargs)
    assert old.memory_id != new.memory_id and new.status == "candidate"
