"""Real SQLite cursor pages and HTTP compression; no models or personal data."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.api.routes.sessions import router
from app.core.sessions import SessionHistoryCursorError, SessionService
from app.storage.db import connect, init_db


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "test.sqlite3"
    init_db(path)
    service = SessionService(lambda: connect(path))
    sid = service.create_session(title="Synthetic history").session.session_id
    for n in range(85):
        service.append_message(session_id=sid, role="user", content=f"message {n}", message_id=f"m_{n}")
    # Deliberately equal timestamps exercise the stable tie breaker.
    with connect(path) as conn:
        conn.execute("UPDATE agent_session_messages SET created_at='2026-10-24T00:00:00Z'")
    return service, sid, path


def ids(page):
    return [item.message_id for item in page.messages]


def test_recent_older_and_incremental_pages_have_no_gaps_or_duplicates(store):
    service, sid, _ = store
    recent = service.get_message_page(session_id=sid, limit=30)
    assert ids(recent) == [f"m_{n}" for n in range(55, 85)]
    assert recent.message_page["has_more"]
    older = service.get_message_page(session_id=sid, limit=30, before_message_id="m_55")
    oldest = service.get_message_page(session_id=sid, limit=30, before_message_id="m_25")
    assert ids(oldest) + ids(older) + ids(recent) == [f"m_{n}" for n in range(85)]
    assert not oldest.message_page["has_more"]
    assert ids(service.get_message_page(session_id=sid, limit=7, after_message_id="m_77")) == [f"m_{n}" for n in range(78, 85)]
    service.append_message(session_id=sid, role="agent", content="new answer", message_id="new")
    incremental = service.get_message_page(session_id=sid, limit=1, after_message_id="m_83")
    assert ids(incremental) == ["m_84"] and incremental.message_page["has_more"]
    assert ids(service.get_message_page(session_id=sid, after_message_id="m_84")) == ["new"]
    empty = service.get_message_page(session_id=sid, after_message_id="new")
    assert ids(empty) == [] and not empty.message_page["has_more"]


def test_cursor_never_crosses_session_or_survives_cleared_history(store):
    service, sid, path = store
    other = service.create_session(title="Other").session.session_id
    for cursor in ("missing", "m_84"):
        with pytest.raises(SessionHistoryCursorError):
            service.get_message_page(session_id=other, after_message_id=cursor)
    with connect(path) as conn:
        conn.execute("DELETE FROM agent_session_messages WHERE session_id=?", (sid,))
        conn.execute("UPDATE agent_sessions SET metadata=? WHERE session_id=?",
                     (json.dumps({"history_reset_at": "2026-10-24T01:00:00Z"}), sid))
    with pytest.raises(SessionHistoryCursorError):
        service.get_message_page(session_id=sid, after_message_id="m_84")
    page = service.get_message_page(session_id=sid)
    assert ids(page) == [] and page.message_page["history_reset_at"] == "2026-10-24T01:00:00Z"
    with connect(path) as conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (sid,))
    with pytest.raises(KeyError):
        service.get_message_page(session_id=sid)


def test_conversation_pages_count_visible_messages_before_limit(store):
    service, sid, path = store
    with connect(path) as conn:
        conn.execute("DELETE FROM agent_session_messages WHERE session_id=?", (sid,))
    for n in range(35):
        service.append_message(session_id=sid, role="agent" if n % 2 else "user",
                               content=f"conversation {n}", message_id=f"visible_{n}")
        service.append_message(session_id=sid, role="tool", content="internal wakeup",
                               payload={"input_origin": "background"}, message_id=f"hidden_{n}")
    for n in range(200):
        service.append_message(session_id=sid, role="tool", content="internal event",
                               payload={"input_origin": "background"}, message_id=f"tail_{n}")
    # Match the real failure: the raw latest page contains only internal inputs.
    assert all(item.role == "tool" for item in service.get_message_page(session_id=sid).messages)
    recent = service.get_message_page(session_id=sid, message_view="conversation")
    assert ids(recent) == [f"visible_{n}" for n in range(5, 35)]
    assert recent.message_page["view"] == "conversation"
    assert recent.message_page["has_more"]
    older = service.get_message_page(session_id=sid, message_view="conversation",
                                     before_message_id=recent.message_page["first_message_id"])
    assert ids(older) == [f"visible_{n}" for n in range(5)]
    assert not older.message_page["has_more"]
    assert not service.get_message_page(session_id=sid, message_view="conversation",
                                        after_message_id="visible_34").message_page["has_more"]
    service.append_message(session_id=sid, role="agent", content="new answer", message_id="new")
    assert ids(service.get_message_page(session_id=sid, message_view="conversation",
                                       after_message_id="visible_34")) == ["new"]
    # Audit/full history and the context source retain every internal input.
    assert len(service.get_session(session_id=sid).messages) == 271


@pytest.mark.parametrize(("role", "payload", "visible"), [
    ("agent", {"input_origin": "background"}, True),
    ("agent", {"user_visible": False}, False),
    ("user", {"metadata": {"user_visible": False}}, False),
    ("agent", {"user_visible": 0}, True),
    ("tool", {"input_origin": "background"}, False),
    ("user", {"input_origin": "background"}, False),
    ("tool", {"input_envelope": {"origin": "background"}}, False),
    ("tool", {"input_origin": "user"}, True),
    ("tool", {}, True),
    ("system", {}, False),
    ("tool", {"input_origin": "manager"}, False),
    ("tool", {"input_envelope": {"origin": "manager", "purpose": "do the task"}}, True),
    ("tool", {"input_envelope": {"origin": "manager", "purpose": " "}}, False),
    ("tool", {"input_envelope": {"origin": "manager", "purpose": 123}}, False),
    ("tool", {"input_origin": "background", "input_envelope": {"origin": "manager", "purpose": "task"}}, False),
])
def test_conversation_projection_preserves_visibility_and_provenance(store, role, payload, visible):
    service, sid, _ = store
    service.append_message(session_id=sid, role=role, content="synthetic", payload=payload, message_id="probe")
    page = service.get_message_page(session_id=sid, message_view="conversation", after_message_id="m_84")
    assert ids(page) == (["probe"] if visible else [])


def test_http_pagination_is_opt_in_and_validates_bounds(store):
    service, sid, _ = store
    app = FastAPI()
    app.state.runtime = SimpleNamespace(session_service=service, get_session=service.get_session)
    app.include_router(router)
    with TestClient(app) as client:
        url = f"/sessions/{sid}"
        legacy = client.get(url).json()
        assert len(legacy["messages"]) == 85 and "message_page" not in legacy
        recent = client.get(url, params={"message_limit": 3})
        assert recent.status_code == 200 and len(recent.json()["messages"]) == 3
        assert recent.json()["message_page"]["last_message_id"] == "m_84"
        conversation = client.get(url, params={"message_limit": 3, "message_view": "conversation"})
        assert conversation.status_code == 200
        assert conversation.json()["message_page"]["view"] == "conversation"
        assert client.get(url, params={"message_limit": 3, "after_message_id": "missing"}).status_code == 409
        for query in ({"message_limit": 0}, {"message_limit": 101}, {"after_message_id": "m_84"},
                      {"message_view": "conversation"}, {"message_limit": 3, "message_view": "unknown"},
                      {"message_limit": 3, "before_message_id": "m_1", "after_message_id": "m_2"}):
            assert client.get(url, params=query).status_code == 422


def test_product_compresses_history_but_excludes_sse(tmp_path, monkeypatch):
    from app.api.main import create_app
    from app.core.config import get_settings

    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    try:
        app = create_app()
        detail = app.state.runtime.session_service.create_session(title="Compressed history", initial_message="synthetic " * 10000)

        @app.get("/synthetic-stream")
        async def events():
            async def body():
                yield "event: token\ndata: " + "chunk " * 1000 + "\n\n"
                yield "event: complete\ndata: done\n\n"
            return StreamingResponse(body(), media_type="text/event-stream")

        # No lifespan: the test does not start background workers or providers.
        client = TestClient(app)
        response = client.get(f"/sessions/{detail.session.session_id}", headers={"Accept-Encoding": "gzip"})
        assert response.headers["content-encoding"] == "gzip"
        assert int(response.headers["content-length"]) < len(response.content) / 10
        assert response.json()["messages"][0]["content"] == "synthetic " * 10000
        stream = client.get("/synthetic-stream", headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in stream.headers
        assert stream.text.startswith("event: token\n") and stream.text.endswith("data: done\n\n")
    finally:
        get_settings.cache_clear()
