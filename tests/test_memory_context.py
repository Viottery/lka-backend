from app.core.memory_context import MemoryContextProvider, MemoryPreTurnGate
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput


def test_repeated_sources_and_long_content_remain_bounded_with_exact_read_handle(tmp_path):
    service = MemoryService(tmp_path / "bounded.sqlite3")
    service.ensure_schema()
    for index in range(12):
        source = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=f"message-{index}", trusted_source=True,
        ))
        record = service.create(MemoryInput(content="回答需注明来源" * 100, source_id=source,
                                           memory_type="preference", sensitivity="normal"))
    item = MemoryContextProvider(service)("new", None, "回答")["items"][0]
    assert len(item["source_ids"]) == 8 and item["source_count"] == 12
    assert item["sources_omitted"] == 4 and item["content_truncated"] is True
    assert len(item["content"]) == 500
    assert item["read_ref"] == f"memory:{record.memory_id}"
    assert len(service.get(record.memory_id).source_ids) == 12


def test_recall_is_scoped_and_retraction_is_immediate(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    project_a = service.resolve_project(tmp_path / "a")
    project_b = service.resolve_project(tmp_path / "b")
    source = service.register_source(MemorySourceInput(
        source_type="user_message", source_ref="msg1", trusted_source=True,
    ))
    global_memory = service.create(MemoryInput(
        content="用户喜欢简短回答", memory_type="preference", sensitivity="normal",
        source_id=source,
    ))
    secret = service.create(MemoryInput(
        content="project A secret decision", memory_type="project_decision",
        scope="project", project_id=project_a, sensitivity="normal", source_id=source,
    ))
    service.create(MemoryInput(
        content="project B secret decision", memory_type="project_decision",
        scope="project", project_id=project_b, sensitivity="normal", source_id=source,
    ))
    provider = MemoryContextProvider(service)
    view = provider("session", str(tmp_path / "a"), "decision")
    ids = {item["memory_id"] for item in view["items"]}
    assert secret.memory_id in ids and global_memory.memory_id in ids
    assert all(item["project_id"] != project_b for item in view["items"])
    service.retract(secret.memory_id, expected_version=secret.version)
    assert secret.memory_id not in {
        item["memory_id"] for item in provider("session", str(tmp_path / "a"), "decision")["items"]
    }


def test_personal_candidate_never_injected(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(source_type="user_message", source_ref="msg"))
    service.create(MemoryInput(content="Candidate", source_id=source, sensitivity="normal"))
    assert MemoryContextProvider(service)("s", None, "Candidate")["items"] == []


def test_unrelated_recent_facts_do_not_pollute_new_task_but_preferences_do(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(source_type="user_api", source_ref="manual", trusted_source=True))
    fact = service.create(MemoryInput(content="演出票价上限600元", memory_type="user_fact", source_id=source, sensitivity="normal"))
    preference = service.create(MemoryInput(content="回答先给结论", memory_type="preference", source_id=source, sensitivity="normal"))
    provider = MemoryContextProvider(service)
    unrelated = {row["memory_id"] for row in provider("new", None, "整理代码接口")["items"]}
    assert fact.memory_id not in unrelated and preference.memory_id in unrelated
    assert fact.memory_id in {row["memory_id"] for row in provider("other", None, "演出票价")["items"]}


def test_explicit_forget_id_applies_before_next_recall(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(
        source_type="user_api", source_ref="manual", trusted_source=True,
    ))
    record = service.create(MemoryInput(
        content="喜欢很长的回答", memory_type="preference", sensitivity="normal",
        source_id=source,
    ))
    gate = MemoryPreTurnGate(service)
    assert gate(None, "忘记这条") == []
    assert gate(None, f"忘记 {record.memory_id}") == [record.memory_id]
    assert MemoryContextProvider(service)("s", None, "回答")["items"] == []


def test_source_aware_pre_turn_hook_keeps_persisted_identity_and_legacy_contract():
    from app.core.agent_turn import AgentTurnLoop, _turn_inference_snapshot

    calls = []

    class SourceAware:
        def __call__(self, workspace, text):
            raise AssertionError("must use the persisted source callback")

        def on_persisted_user_message(self, workspace, text, message_id):
            calls.append((workspace, text, message_id))

    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.memory_pre_turn_callback = SourceAware()
    loop._apply_memory_pre_turn_gate("workspace", "same text", "exact-user-message")
    assert calls == [("workspace", "same text", "exact-user-message")]
    token = _turn_inference_snapshot.set(object())
    try:
        loop._apply_memory_pre_turn_gate("workspace", "child text", "child-message")
    finally:
        _turn_inference_snapshot.reset(token)
    assert len(calls) == 1
    loop.memory_pre_turn_callback = lambda workspace, text: calls.append((workspace, text))
    loop._apply_memory_pre_turn_gate(None, "legacy", "unused-identity")
    assert calls[-1] == (None, "legacy")


def test_recall_finds_relevant_memory_older_than_recent_pool(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(
        source_type="user_api", source_ref="manual", trusted_source=True,
    ))
    target = service.create(MemoryInput(
        content="特殊偏好：表格结论要附来源", memory_type="user_fact",
        sensitivity="normal", source_id=source,
    ))
    for index in range(35):
        service.create(MemoryInput(
            content=f"无关记录 {index}", memory_type="user_fact", source_id=source,
        ))
    view = MemoryContextProvider(service)("s", None, "特殊偏好")
    assert target.memory_id in {item["memory_id"] for item in view["items"]}


def test_natural_correction_retracts_only_a_matching_memory(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(
        source_type="user_api", source_ref="manual", trusted_source=True,
    ))
    long = service.create(MemoryInput(
        content="以后回答时详细解释", memory_type="preference", sensitivity="normal",
        source_id=source,
    ))
    brief = service.create(MemoryInput(
        content="以后邮件标题附上日期", memory_type="preference", sensitivity="normal",
        source_id=source,
    ))
    gate = MemoryPreTurnGate(service)
    assert gate(None, "我之前说错了，忘记详细解释这个偏好，以后请简洁回答") == [long.memory_id]
    assert gate(None, "忘记这个") == []
    assert service.get(brief.memory_id).status == "active"
