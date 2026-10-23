"""Direct-client recycle-bin authorization remains bound to durable workspace scope."""
from contextlib import closing

import pytest

from tests.test_distributed_runtime import connect_browser
from tests.test_distributed_runtime import connected as connected_fixture


@pytest.fixture
def connected(tmp_path, monkeypatch, request):
    yield from connected_fixture.__wrapped__(tmp_path, monkeypatch, request)


def test_deleted_session_scope_fences_listing_and_restore(connected):
    hub, _cloud, node, runtime, workspace_a = connected
    workspace_b = workspace_a.parent / "private-workspace"
    workspace_b.mkdir()
    runtime.device_connection_service.config.workspace_roots["private"] = str(workspace_b)

    def deleted(title, workspace):
        detail = runtime.create_session(title=title, initial_message=title + " private message")
        identity = detail.session.session_id
        runtime.set_session_workspace(session_id=identity, path=str(workspace), platform=runtime.platform.name)
        assert runtime.delete_session(session_id=identity)
        return identity

    granted = deleted("Granted workspace", workspace_a)
    outside = deleted("Excluded workspace", workspace_b)
    assistant = deleted("Assistant execution", workspace_a)
    remote = deleted("Remote execution", workspace_a)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET creation_source='assistant',creator_session_id='manager' WHERE session_id=?", (assistant,))
        conn.execute("UPDATE agent_sessions SET metadata=json_set(metadata,'$.remote_execution',json(?)) WHERE session_id=?",
                     ('{"task_id":"cloud-task"}', remote))

    connect_browser(hub, node, runtime)  # Grant contains workspace A only.
    listing = node.get("/sessions/deleted?limit=100")
    assert listing.status_code == 200, listing.text
    assert [item["session_id"] for item in listing.json()["sessions"]] == [granted]
    for identity in (outside, assistant, remote, "unknown-session"):
        restored = node.post(f"/sessions/{identity}/restore")
        assert restored.status_code == 403, restored.text
        assert "private message" not in restored.text
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT status FROM agent_sessions WHERE session_id=?", (outside,)).fetchone()[0] == "deleted"
    restored = node.post(f"/sessions/{granted}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["session"]["session_id"] == granted
    assert restored.json()["messages"][0]["content"] == "Granted workspace private message"
