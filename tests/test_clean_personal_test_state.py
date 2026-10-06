import importlib.util
import sqlite3
from pathlib import Path

import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.instruction_files import GLOBAL_TEMPLATE
from app.domains.memory import MemoryInput, MemorySourceInput

spec = importlib.util.spec_from_file_location("clean_personal_test_state", Path(__file__).parents[1] / "scripts/clean_personal_test_state.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    user = runtime.session_service.append_message(session_id="test", role="user", content="我希望以后回答简洁")
    runtime.session_service.append_message(session_id="qq-keep", role="user", content="我的 QQ 消息如何？")
    source = runtime.memory_service.register_source(MemorySourceInput(source_type="user_message", source_ref=user.message_id))
    memory = runtime.memory_service.create(MemoryInput(content="以后回答简洁", source_id=source, user_confirmed=True))
    runtime.memory_files.generate(scope="global")
    runtime.instruction_files.update("global", content=GLOBAL_TEMPLATE + "\nTest preference.\n",
                                     expected_sha256=runtime.instruction_files.read("global")["sha256"])
    with runtime._conn() as conn:
        conn.execute("CREATE TABLE message_test_guard(id TEXT PRIMARY KEY,payload TEXT)")
        conn.execute("INSERT INTO message_test_guard VALUES('qq','keep content')")
        conn.commit()
    try:
        yield runtime, memory
    finally:
        runtime.stop()
        get_settings.cache_clear()


def test_preview_is_read_only_and_unknown_ids_fail(state):
    runtime, memory = state
    plan = cleanup.clean(runtime.db_path, sessions=["test"], memories=[memory.memory_id])
    assert plan["mode"] == "preview"
    assert runtime.session_service.get_session_or_none(session_id="test") is not None
    assert runtime.memory_service.get_active(memory.memory_id) is not None
    assert not (runtime.db_path.parent.parent / "deployment_backups").exists()
    with pytest.raises(ValueError, match="Unknown session"):
        cleanup.clean(runtime.db_path, sessions=["unknown"], memories=[], apply=True)


def test_backed_up_scoped_cleanup_preserves_message_and_qq_conversation(state):
    runtime, memory = state
    sha = runtime.instruction_files.read("global")["sha256"]
    plan = cleanup.clean(runtime.db_path, sessions=["test"], memories=[memory.memory_id],
                         apply=True, guidance_sha=sha)
    assert plan["protected_content_unchanged"]
    assert runtime.session_service.get_session_or_none(session_id="test") is None
    assert runtime.session_service.get_session_or_none(session_id="qq-keep") is not None
    assert runtime.memory_service.get(memory.memory_id).status == "retracted"
    assert runtime.instruction_files.read("global")["content"] == GLOBAL_TEMPLATE
    with runtime._conn() as conn:
        assert conn.execute("SELECT payload FROM message_test_guard").fetchone()[0] == "keep content"
        assert conn.execute("SELECT COUNT(*) FROM agent_session_messages WHERE session_id='test'").fetchone()[0] == 1
    with sqlite3.connect(Path(plan["backup_path"]) / "lka.sqlite3") as saved:
        assert saved.execute("SELECT status FROM agent_sessions WHERE session_id='test'").fetchone()[0] == "active"
        assert saved.execute("SELECT status FROM memory_entries WHERE memory_id=?", (memory.memory_id,)).fetchone()[0] == "active"


def test_stale_guidance_hash_refuses_cleanup_before_database_changes(state):
    runtime, memory = state
    with pytest.raises(ValueError, match="Guidance changed"):
        cleanup.clean(runtime.db_path, sessions=["test"], memories=[memory.memory_id], apply=True, guidance_sha="stale")
    assert runtime.memory_service.get_active(memory.memory_id) is not None


def test_session_only_cleanup_refreshes_all_affected_memory_views(state):
    runtime, memory = state
    cleanup.clean(runtime.db_path, sessions=["test"], memories=[], apply=True)
    assert runtime.memory_service.get(memory.memory_id).status == "retracted"
    assert memory.content not in runtime.memory_files.path_for(scope="global").read_text(encoding="utf-8")
