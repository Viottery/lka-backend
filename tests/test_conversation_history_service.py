import pytest

from app.core.context_driver import ToolView
from app.core.tools import ToolContext
from app.domains.conversation_history import ConversationHistoryService
from app.storage.db import connect, init_db


@pytest.fixture
def history(tmp_path):
    path = tmp_path / "history.sqlite3"
    init_db(path)

    def factory():
        return connect(path)

    with factory() as conn:
        conn.execute(
            "INSERT INTO agent_sessions(session_id,title,status,kind,metadata,created_at,updated_at) "
            "VALUES('s','t','active','ordinary','{}','now','now')"
        )
        conn.execute(
            "INSERT INTO agent_sessions(session_id,title,status,kind,metadata,created_at,updated_at) "
            "VALUES('other','t','active','manager','{}','now','now')"
        )
        conn.executescript(
            """CREATE TABLE agent_session_context_messages(
                   session_id TEXT,seq INTEGER,role TEXT,content TEXT,created_at TEXT,
                   trace_id TEXT,PRIMARY KEY(session_id,seq));"""
        )
        messages = [
            ("user", "开会时讨论了amberwhale", "t1"),
            ("user", "补充要求是保密", "t1"),
            ("agent", "后来amberwhale的结论是保密", "t1"),
            ("tool", "内部工具数据", "t2"),
            ("agent", "后台摘要amberwhale", "t2"),
            ("user", "另外一个问题", "t3"),
        ]
        for seq, (role, content, trace) in enumerate(messages, 1):
            conn.execute(
                "INSERT INTO agent_session_context_messages VALUES(?,?,?,?,?,?)",
                ("s", seq, role, content, "now", trace),
            )
        conn.execute(
            "INSERT INTO agent_session_context_messages VALUES(?,?,?,?,?,?)",
            ("other", 1, "user", "amberwhale private", "now", "x"),
        )
    return ConversationHistoryService(factory, index_batch=2), factory


def test_chinese_and_ascii_substring_search_and_incremental_index(history):
    service, _ = history
    context = ToolContext(session_id="s")
    first = service.search(context, query="amberwhale")
    assert first["index"]["indexed"] == 2
    assert first["index"]["complete"] is False
    while not first["index"]["complete"]:
        first = service.search(context, query="amberwhale")
    assert first["messages"][0]["seq"] == 3
    assert service.search(context, query="berwh")["messages"][0]["seq"] == 3
    assert service.search(context, query="开会时")["messages"][0]["seq"] == 1
    assert service.search(context, query="开会")["messages"][0]["seq"] == 1
    assert service.search(context, query="am")["messages"][0]["seq"] == 3
    assert service.search(context, query="补充")["messages"][0]["seq"] == 2
    reopened = ConversationHistoryService(service._conn, index_batch=2)
    persisted = reopened.search(context, query="amberwhale")
    assert persisted["index"]["indexed"] == 0
    assert persisted["messages"][0]["seq"] == 3
    assert service.search(context, query="后台摘要")["messages"] == []


def test_read_pagination_around_and_source_hash(history):
    service, _ = history
    context = ToolContext(session_id="s")
    page = service.read(context, around_seq=3, limit=2)
    assert [m["seq"] for m in page["messages"]] == [1, 2]
    assert page["messages"][0]["content_hash"]
    assert page["cursor"] == {"anchor_seq": 3, "message_offset": 2, "char_offset": 0}
    page2 = service.read(context, cursor=page["cursor"], limit=2)
    assert [m["seq"] for m in page2["messages"]] == [3]

    first = service.read(context, around_seq=3, max_chars=8, limit=3)
    assert first["cursor"]["char_offset"] == 8
    second = service.read(context, cursor=first["cursor"], max_chars=8, limit=3)
    assert second["messages"][0]["seq"] == 1
    assert second["messages"][0]["start_char"] == 8
    boundary = service.read(context, around_seq=3, max_chars=len("开会时讨论了amberwhale"), limit=3)
    assert boundary["messages"][0]["seq"] == 1
    assert boundary["cursor"]["message_offset"] == 1
    after_boundary = service.read(context, cursor=boundary["cursor"], limit=3)
    assert after_boundary["messages"][0]["seq"] == 2

    single = service.read(context, seq=3, max_chars=5)
    assert single["messages"][0]["next_char"] == 5
    assert single["next_seq"] == 3 and single["next_offset"] == 5
    tail = service.read(context, seq=3, offset=5)
    assert tail["messages"][0]["content"] == "后来amberwhale的结论是保密"[5:]


def test_edits_deletes_sessions_child_and_cross_session_are_rechecked(history):
    service, db = history
    context = ToolContext(session_id="s")
    first = service.search(context, query="amberwhale")
    while not first["index"]["complete"]:
        first = service.search(context, query="amberwhale")
    with db() as conn:
        conn.execute(
            "UPDATE agent_session_context_messages SET content='changed new phrase' "
            "WHERE session_id='s' AND seq=3"
        )
    assert all(item["seq"] != 3 for item in service.search(context, query="amberwhale")["messages"])
    assert service.search(context, query="new phrase")["messages"][0]["seq"] == 3
    with db() as conn:
        conn.execute(
            "INSERT INTO agent_session_context_messages VALUES('s',7,'tool','tool output','now','t1')"
        )
    assert all(item["seq"] != 3 for item in service.search(context, query="new phrase")["messages"])
    with db() as conn:
        conn.execute("DELETE FROM agent_session_context_messages WHERE session_id='s' AND seq=7")
    assert service.search(context, query="new phrase")["messages"][0]["seq"] == 3
    manager = service.search(ToolContext(session_id="other"), query="amberwhale")
    assert manager["messages"][0]["seq"] == 1
    root_view = ToolView(
        snapshot_id="root-view", full_data_authority=True, side_effect_level="read"
    )
    assert service.search(context.model_copy(update={"tool_view": root_view}), query="new phrase")["messages"]
    child_view = ToolView(
        snapshot_id="child-view", child_run_id="child", full_data_authority=True,
        side_effect_level="read",
    )
    with pytest.raises(PermissionError):
        service.search(context.model_copy(update={"tool_view": child_view}))
    restricted_view = ToolView(
        snapshot_id="restricted-view", full_data_authority=False, side_effect_level="read"
    )
    with pytest.raises(PermissionError):
        service.read(context.model_copy(update={"tool_view": restricted_view}), seq=3)
    with db() as conn:
        conn.execute("DELETE FROM agent_session_context_messages WHERE session_id='s' AND seq IN (1,2)")
    assert service.search(context, query="new phrase")["messages"] == []
    with db() as conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id='s'")
    with pytest.raises(KeyError):
        service.read(context, seq=1)


def test_tool_contract_has_no_model_selected_session(history):
    from app.tool_packages.conversation_history import conversation_history_tools

    service, _ = history
    tools = conversation_history_tools(service)
    assert {tool.spec.name for tool in tools} == {"history.search", "history.read"}
    for tool in tools:
        assert "session_id" not in tool.spec.input_schema["properties"]
        assert tool.spec.read_only is True
        assert tool.spec.scope_filtering_required is True
        assert tool.spec.output_selected_content is True


def test_hot_search_does_not_write_index_state(history):
    _service, factory = history
    context = ToolContext(session_id="s")
    statements = []

    def tracked_factory():
        conn = factory()
        conn.set_trace_callback(statements.append)
        return conn

    tracked = ConversationHistoryService(tracked_factory, index_batch=16)
    tracked.search(context, query="amberwhale")
    statements.clear()
    tracked.search(context, query="amberwhale")
    dml = [statement for statement in statements if statement.lstrip().upper().startswith((
        "INSERT", "UPDATE", "DELETE", "REPLACE"
    ))]
    assert dml == []


def test_cursor_traverses_turn_without_duplicate_or_missing_text(history):
    service, _ = history
    context = ToolContext(session_id="s")
    collected = []
    result = service.read(context, around_seq=3, limit=2, max_chars=8)
    while True:
        collected.extend((item["seq"], item["content"]) for item in result["messages"])
        if result["cursor"] is None:
            break
        result = service.read(context, cursor=result["cursor"], limit=2, max_chars=8)
    merged = {}
    for seq, content in collected:
        merged[seq] = merged.get(seq, "") + content
    assert merged == {
        1: "开会时讨论了amberwhale",
        2: "补充要求是保密",
        3: "后来amberwhale的结论是保密",
    }
