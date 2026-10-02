from app.core.memory_context import MemoryContextProvider, MemoryPreTurnGate
from app.core.memory_extraction import extract_user_memories
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput


def test_learn_reinforce_correct_and_recall_without_republishing_quote(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()

    def learn(ref: str, message: str):
        candidates = extract_user_memories(source_id=ref, content=message)
        source_id = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=ref, trusted_source=False,
        ))
        return [service.create(MemoryInput(
            content=candidate.claim,
            memory_type=candidate.kind,
            source_id=source_id,
            confidence=candidate.confidence,
            sensitivity=candidate.sensitivity,
            dedupe_key="short-answer-preference",
        )) for candidate in candidates]

    first = learn("one", "我希望以后回答时先给结论")
    assert len(first) == 1 and first[0].status == "candidate"
    second = learn("two", "我希望以后回答时先给结论")
    assert len(second) == 1 and second[0].memory_id == first[0].memory_id
    assert second[0].status == "active"

    view = MemoryContextProvider(service)("session", None, "先看结论")
    assert [item["memory_id"] for item in view["items"]] == [first[0].memory_id]
    gate = MemoryPreTurnGate(service)
    assert gate(None, "我之前说错了，忘记先看结论这个偏好") == [first[0].memory_id]
    assert MemoryContextProvider(service)("session", None, "先看结论")["items"] == []

    quoted = learn("three", '文档说：“以后回答先给结论”')
    assert quoted == []


def test_explicit_past_valid_until_is_not_returned_by_exact_active_recall(tmp_path):
    service = MemoryService(tmp_path / "memory-expiry.sqlite3")
    service.ensure_schema()
    candidate = extract_user_memories(
        source_id="dated-source",
        content="记住：旧发布窗口有效至 2000-01-01",
    )[0]
    assert candidate.claim == "旧发布窗口"
    assert candidate.expires_at == "2000-01-02T00:00:00+00:00"

    source_id = service.register_source(MemorySourceInput(
        source_type="user_message", source_ref="dated-source", trusted_source=False,
    ))
    record = service.create(MemoryInput(
        content=candidate.claim, memory_type=candidate.kind, source_id=source_id,
        confidence=candidate.confidence, sensitivity=candidate.sensitivity,
        expires_at=candidate.expires_at, user_confirmed=True,
    ))

    assert record.status == "active"
    assert record.expires_at == candidate.expires_at
    assert service.get_active(record.memory_id) is None
    assert service.search("旧发布窗口") == []


def test_same_claim_new_explicit_deadline_extends_expired_deduped_memory(tmp_path):
    service = MemoryService(tmp_path / "expiry-extension.sqlite3")
    service.ensure_schema()

    def publish(ref: str, message: str):
        candidate = extract_user_memories(source_id=ref, content=message)[0]
        source_id = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=ref, trusted_source=True,
        ))
        return service.create(MemoryInput(
            content=candidate.claim, memory_type=candidate.kind, source_id=source_id,
            confidence=candidate.confidence, sensitivity=candidate.sensitivity,
            expires_at=candidate.expires_at, user_confirmed=True,
        ))

    expired = publish("expired-source", "记住：回答先给结论，有效至 2000-01-01")
    assert expired.expires_at == "2000-01-02T00:00:00+00:00"
    assert service.get_active(expired.memory_id) is None
    refreshed = publish("refreshed-source", "记住：回答先给结论，有效至 2099-12-31")
    assert refreshed.memory_id == expired.memory_id
    assert refreshed.content == expired.content
    assert refreshed.version == expired.version + 1
    assert refreshed.expires_at == "2100-01-01T00:00:00+00:00"
    assert len(refreshed.source_ids) == 2
    assert set(expired.source_ids) < set(refreshed.source_ids)
    assert service.get_active(refreshed.memory_id) is not None
    event = service.events(refreshed.memory_id)[-1]
    assert event["event_type"] == "expiry_extended"
    assert event["payload"]["previous_expires_at"] == expired.expires_at


def test_opposite_preference_conflicts_are_reviewable_but_never_injected(tmp_path):
    service = MemoryService(tmp_path / "conflicts.sqlite3")
    service.ensure_schema()

    def learn(ref: str, text: str, *, user_confirmed=False):
        candidate = extract_user_memories(source_id=ref, content=text)[0]
        source_id = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=ref, trusted_source=True,
        ))
        return service.create(MemoryInput(
            content=candidate.claim, memory_type=candidate.kind, source_id=source_id,
            confidence=candidate.confidence, sensitivity=candidate.sensitivity,
            user_confirmed=user_confirmed,
            metadata={"conflict_hints": [hint.__dict__ for hint in candidate.conflict_hints]},
        ))

    concise = learn("concise", "我通常喜欢简洁回答", user_confirmed=True)
    assert concise.status == "active"
    detailed = learn("detailed", "我通常喜欢详细回答", user_confirmed=True)
    assert detailed.status == "active"
    assert detailed.metadata["needs_review"] is True
    assert detailed.metadata["conflict_ids"] == [concise.memory_id]
    assert service.get(concise.memory_id).metadata["conflict_ids"] == [detailed.memory_id]
    assert service.get_active(concise.memory_id) is None
    assert service.get_active(detailed.memory_id) is None
    assert {row.memory_id for row in service.list(statuses=("active",))} == {
        concise.memory_id, detailed.memory_id,
    }
    assert {concise.source_ids[0], detailed.source_ids[0]} <= {
        source["source_id"] for memory_id in (concise.memory_id, detailed.memory_id)
        for source in service.sources_for(memory_id)
    }
    assert any(event["event_type"] == "conflict_detected" for event in service.events(concise.memory_id))
    context = MemoryContextProvider(service)("s", None, "回答风格")
    assert context["items"] == []
    assert context["withheld_conflict_count"] == 2
    service.retract(detailed.memory_id, expected_version=detailed.version)
    assert service.get_active(concise.memory_id) is not None


def test_conflicts_require_same_scope_and_exact_task_condition(tmp_path):
    service = MemoryService(tmp_path / "conflict-scope.sqlite3")
    service.ensure_schema()

    def put(ref: str, text: str, *, scope="global", project_id=None):
        candidate = extract_user_memories(source_id=ref, content=text)[0]
        source = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=ref, trusted_source=True,
        ))
        return service.create(MemoryInput(
            content=candidate.claim, memory_type="preference", source_id=source,
            scope=scope, project_id=project_id,
            metadata={"conflict_hints": [hint.__dict__ for hint in candidate.conflict_hints]},
        ))

    project = service.resolve_project(tmp_path / "project")
    code = put("code", "写代码时我更喜欢简洁解释")
    mail = put("mail", "处理邮件时我更喜欢详细解释")
    project_detail = put("project", "我通常喜欢详细回答", scope="project", project_id=project)
    assert not code.metadata.get("needs_review")
    assert not mail.metadata.get("needs_review")
    assert not project_detail.metadata.get("needs_review")
    assert service.get_active(code.memory_id) is not None


def test_inferred_conflict_candidate_cannot_suppress_confirmed_preference_and_retry_is_idempotent(tmp_path):
    service = MemoryService(tmp_path / "inferred-conflict.sqlite3")
    service.ensure_schema()

    def candidate_for(ref: str, text: str, *, trusted: bool):
        candidate = extract_user_memories(source_id=ref, content=text)[0]
        source = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=ref, trusted_source=trusted,
        ))
        payload = MemoryInput(
            content=candidate.claim, memory_type="preference", source_id=source,
            confidence=candidate.confidence,
            metadata={"conflict_hints": [hint.__dict__ for hint in candidate.conflict_hints]},
        )
        return candidate, payload

    _confirmed_candidate, confirmed_payload = candidate_for(
        "confirmed", "我通常喜欢简洁回答", trusted=False,
    )
    confirmed = service.create(confirmed_payload.model_copy(update={"user_confirmed": True}))
    _inferred_candidate, inferred_payload = candidate_for(
        "model-inference", "我通常喜欢详细回答", trusted=False,
    )
    inferred = service.create(inferred_payload)
    assert inferred.status == "candidate"
    assert inferred.metadata["needs_review"] is True
    assert service.get(confirmed.memory_id).metadata.get("needs_review") is None
    assert service.get_active(confirmed.memory_id) is not None

    event_count = len(service.events(inferred.memory_id))
    retried = service.create(inferred_payload)
    assert retried.version == inferred.version
    assert len(service.events(inferred.memory_id)) == event_count
    assert service.get_active(confirmed.memory_id) is not None


def test_explicit_correction_retracts_old_then_new_preference_is_recallable(tmp_path):
    service = MemoryService(tmp_path / "correction-restores.sqlite3")
    service.ensure_schema()
    old_candidate = extract_user_memories(
        source_id="old", content="我通常喜欢简洁回答",
    )[0]
    old_source = service.register_source(MemorySourceInput(
        source_type="user_message", source_ref="old", trusted_source=False,
    ))
    old = service.create(MemoryInput(
        content=old_candidate.claim, memory_type="preference", source_id=old_source,
        user_confirmed=True,
        metadata={"conflict_hints": [hint.__dict__ for hint in old_candidate.conflict_hints]},
    ))
    assert MemoryPreTurnGate(service)(None, "我之前说错了，我现在更喜欢详细回答") == [old.memory_id]
    new_candidate = extract_user_memories(
        source_id="new", content="记住：我希望以后回答详细一些",
    )[0]
    new_source = service.register_source(MemorySourceInput(
        source_type="user_message", source_ref="new", trusted_source=False,
    ))
    new = service.create(MemoryInput(
        content=new_candidate.claim, memory_type="preference", source_id=new_source,
        user_confirmed=True,
        metadata={"conflict_hints": [hint.__dict__ for hint in new_candidate.conflict_hints]},
    ))
    assert new.status == "active"
    assert service.get_active(new.memory_id) is not None
    assert service.get(old.memory_id).status == "retracted"


def test_correct_clears_old_conflict_refs_and_does_not_copy_old_hints(tmp_path):
    service = MemoryService(tmp_path / "correct-conflict-lifecycle.sqlite3")
    service.ensure_schema()

    def add(ref: str, text: str, *, notes=None):
        candidate = extract_user_memories(source_id=ref, content=text)[0]
        source = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=ref, trusted_source=False,
        ))
        return service.create(MemoryInput(
            content=candidate.claim, memory_type="preference", source_id=source,
            user_confirmed=True,
            metadata={"notes": notes, "conflict_hints": [hint.__dict__ for hint in candidate.conflict_hints]},
        ))

    original = add("original", "我通常喜欢简洁回答", notes="keep-me")
    peer = add("peer", "我通常喜欢详细回答")
    corrected = service.correct(
        original.memory_id, content="我希望以后回答时先给结论",
        expected_version=service.get(original.memory_id).version,
    )
    assert corrected.status == "active"
    assert corrected.metadata.get("notes") == "keep-me"
    assert not corrected.metadata.get("needs_review")
    assert not corrected.metadata.get("conflict_ids")
    assert not corrected.metadata.get("conflict_hints")
    assert service.get(peer.memory_id).metadata.get("needs_review") is None
    assert service.get(peer.memory_id).metadata.get("conflict_ids") is None
    assert service.get_active(peer.memory_id) is not None


def test_revoking_last_source_of_conflict_peer_restores_other_active_memory(tmp_path):
    service = MemoryService(tmp_path / "revoke-conflict-peer.sqlite3")
    service.ensure_schema()

    def add(ref: str, text: str):
        candidate = extract_user_memories(source_id=ref, content=text)[0]
        source = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=ref, trusted_source=False,
        ))
        record = service.create(MemoryInput(
            content=candidate.claim, memory_type="preference", source_id=source,
            user_confirmed=True,
            metadata={"conflict_hints": [hint.__dict__ for hint in candidate.conflict_hints]},
        ))
        return record, source

    kept, _kept_source = add("kept", "我通常喜欢简洁回答")
    withdrawn, withdrawn_source = add("withdrawn", "我通常喜欢详细回答")
    assert service.get_active(kept.memory_id) is None
    assert service.revoke_source(withdrawn_source) == 1
    assert service.get(withdrawn.memory_id).status == "retracted"
    assert service.get(kept.memory_id).metadata.get("needs_review") is None
    assert service.get_active(kept.memory_id) is not None


def test_expired_conflict_peer_is_ignored_even_before_metadata_compaction(tmp_path):
    service = MemoryService(tmp_path / "expired-conflict-peer.sqlite3")
    service.ensure_schema()

    def add(ref: str, text: str):
        candidate = extract_user_memories(source_id=ref, content=text)[0]
        source = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=ref, trusted_source=False,
        ))
        return service.create(MemoryInput(
            content=candidate.claim, memory_type="preference", source_id=source,
            user_confirmed=True,
            metadata={"conflict_hints": [hint.__dict__ for hint in candidate.conflict_hints]},
        ))

    kept = add("kept", "我通常喜欢简洁回答")
    expired = add("expired", "我通常喜欢详细回答")
    conn = service._connect()
    try:
        conn.execute("UPDATE memory_entries SET expires_at='2000-01-02T00:00:00+00:00' WHERE memory_id=?",
                     (expired.memory_id,))
        conn.commit()
    finally:
        conn.close()

    assert service.get(expired.memory_id).status == "active"
    assert service.get(kept.memory_id).metadata.get("needs_review") is None
    assert service.get_active(kept.memory_id) is not None


def test_explicit_positive_correction_retracts_opposite_preference_before_recall(tmp_path):
    service = MemoryService(tmp_path / "conflict-correction.sqlite3")
    service.ensure_schema()
    candidate = extract_user_memories(source_id="old", content="我通常喜欢简洁回答")[0]
    source = service.register_source(MemorySourceInput(
        source_type="user_message", source_ref="old", trusted_source=True,
    ))
    old = service.create(MemoryInput(
        content=candidate.claim, memory_type="preference", source_id=source,
        metadata={"conflict_hints": [hint.__dict__ for hint in candidate.conflict_hints]},
    ))
    retracted = MemoryPreTurnGate(service)(None, "我之前说错了，我现在更喜欢详细回答")
    assert retracted == [old.memory_id]
    assert service.get(old.memory_id).status == "retracted"
