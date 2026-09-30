from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.domains.knowledge import KnowledgeService
from app.domains.workspace_knowledge import WorkspaceKnowledgeIndexer
from app.storage.db import connect, get_db_path, init_db


def _service(tmp_path: Path) -> tuple[KnowledgeService, Path]:
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    return KnowledgeService(lambda: connect(db_path)), db_path


def test_workspace_index_imports_supported_files_with_confined_scope(tmp_path):
    service, db_path = _service(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("workspace notes searchable", encoding="utf-8")
    (root / "skip.csv").write_text("not indexed", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "readme.TXT").write_text("nested workspace facts", encoding="utf-8")

    result = WorkspaceKnowledgeIndexer(service, [root]).index_workspace(root)

    assert result.discovered_files == 2
    assert result.imported_files == 2
    assert result.failed_files == 0
    assert result.skipped_files == 0
    assert len(service.search(query="searchable", source_types=["workspace_file"]).results) == 1
    connection = sqlite3.connect(db_path)
    try:
        scopes = connection.execute(
            "SELECT DISTINCT access_scope FROM knowledge_sources WHERE source_type='workspace_file'"
        ).fetchall()
        uris = connection.execute(
            "SELECT uri FROM knowledge_documents WHERE source_type='workspace_file' ORDER BY uri"
        ).fetchall()
    finally:
        connection.close()
    assert [json.loads(row[0]) for row in scopes] == [
        {"workspace_paths": [str(root.resolve())]}
    ]
    assert [row[0] for row in uris] == sorted(row[0] for row in uris)
    assert all(uri.startswith("workspace://") for (uri,) in uris)


def test_workspace_reindex_replaces_changed_file_deterministically(tmp_path):
    service, db_path = _service(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    note = root / "note.md"
    note.write_text("original workspace phrase", encoding="utf-8")
    indexer = WorkspaceKnowledgeIndexer(service, [root])

    first = indexer.index_workspace(root)
    note.write_text("updated workspace phrase", encoding="utf-8")
    second = indexer.index_workspace(root)

    assert first.imported_files == second.imported_files == 1
    assert len(service.search(query="updated", source_types=["workspace_file"]).results) == 1
    connection = sqlite3.connect(db_path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_documents WHERE source_type='workspace_file'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_workspace_reindex_prunes_deleted_file_only_after_complete_scan(tmp_path):
    service, _ = _service(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    first = root / "first.md"
    second = root / "second.md"
    first.write_text("alphaunique sentinel text", encoding="utf-8")
    second.write_text("betaunique sentinel text", encoding="utf-8")
    WorkspaceKnowledgeIndexer(service, [root]).index_workspace(root)

    first.unlink()
    limited = WorkspaceKnowledgeIndexer(service, [root], max_total_bytes=5).index_workspace(root)
    assert limited.errors
    assert service.search(query="alphaunique", source_types=["workspace_file"]).results

    complete = WorkspaceKnowledgeIndexer(service, [root]).index_workspace(root)
    assert complete.pruned_documents == 1
    assert service.search(query="alphaunique", source_types=["workspace_file"]).results == []
    assert service.search(query="betaunique", source_types=["workspace_file"]).results


def test_workspace_traversal_error_does_not_prune_existing_index(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    note = root / "note.md"
    note.write_text("retainedunique evidence", encoding="utf-8")
    indexer = WorkspaceKnowledgeIndexer(service, [root])
    indexer.index_workspace(root)
    note.unlink()
    monkeypatch.setattr(indexer, "_candidate_files", lambda *_: ([], False, [PermissionError()]))

    result = indexer.index_workspace(root)

    assert result.errors
    assert result.pruned_documents == 0
    assert service.search(query="retainedunique", source_types=["workspace_file"]).results


def test_workspace_index_rejects_unconfigured_root_and_skips_symlinks(tmp_path):
    service, _ = _service(tmp_path)
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("outside content", encoding="utf-8")
    (root / "escape.txt").symlink_to(secret)

    indexer = WorkspaceKnowledgeIndexer(service, [root])
    with pytest.raises(ValueError, match="configured allowed roots"):
        indexer.index_workspace(outside)

    result = indexer.index_workspace(root)
    assert result.discovered_files == 1
    assert result.imported_files == 0
    assert result.skipped_files == 1
    assert service.search(query="outside content", source_types=["workspace_file"]).results == []


def test_workspace_index_enforces_file_and_aggregate_byte_limits(tmp_path):
    service, _ = _service(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.md").write_text("a" * 12, encoding="utf-8")
    (root / "b.txt").write_text("b" * 12, encoding="utf-8")

    result = WorkspaceKnowledgeIndexer(
        service, [root], max_file_bytes=16, max_total_bytes=16
    ).index_workspace(root)

    assert result.discovered_files == 2
    assert result.imported_files == 1
    assert result.skipped_files == 1
    assert result.bytes_read == 12
    assert result.errors


def test_workspace_index_uses_existing_secret_redaction_gateway(tmp_path):
    service, _ = _service(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "credentials.txt").write_text(
        "ordinary text\napi_key = abcdefghijklmnop", encoding="utf-8"
    )

    result = WorkspaceKnowledgeIndexer(service, [root]).index_workspace(root)
    search = service.search(query="credentials api key", source_types=["workspace_file"])

    assert result.imported_files == 1
    assert search.results == []
    assert search.filtered_count == 1


def test_runtime_workspace_index_option_populates_rag(tmp_path, monkeypatch):
    from app.api.main import create_app
    from app.core.config import get_settings
    from app.core.multi_agent import ForkPolicy, ScopeGrant

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("workspace rag integration sentinel", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(root))
    get_settings.cache_clear()
    runtime = create_app().state.runtime

    response = runtime.index_workspace(str(root), options={"index_knowledge": True})

    assert response.status == "completed"
    assert response.indexed_files == 1
    assert response.indexed_chunks == 1
    assert response.knowledge_errors == []
    assert runtime.knowledge_service.search(
        query="integration sentinel", source_types=["workspace_file"], mode="keyword"
    ).results[0].title == "notes.md"
    source_ids = runtime.knowledge_service.list_authorized_source_ids(
        workspace_path=str(root)
    )
    assert len(source_ids) == 1
    assert runtime.knowledge_service.list_authorized_source_ids() == ()
    runtime.agent_turn_loop.fork_policy = ForkPolicy(
        max_depth=1, max_children=1, max_fork_size=1,
        allowed_scope=ScopeGrant(workspace_paths=(str(root),)),
    )
    assert source_ids[0] in runtime._live_fork_policy().allowed_scope.source_ids


def test_parent_rag_tool_results_become_bounded_child_evidence(tmp_path, monkeypatch):
    from app.api.main import create_app
    from app.core.config import get_settings

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("safeunique evidence", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(root))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    runtime.index_workspace(str(root), options={"index_knowledge": True})
    run = runtime.create_agent_run(session_id="parent_evidence", user_input="find evidence")
    result = runtime.knowledge_service.search(query="safeunique", mode="keyword")
    record = result.results[0].model_dump(mode="json")
    store = runtime.agent_run_store
    store.claim_tool_invocation(
        invocation_id="evidence_invocation", run_id=run.run_id,
        tool_name="knowledge.search", tool_input={"query": "safeunique"},
        claimed_at="2026-09-29T00:00:00+00:00",
    )
    store.complete_tool_invocation(
        invocation_id="evidence_invocation",
        result={"status": "completed", "output": {"results": [
            record,
            {**record, "chunk_id": "denied", "policy_decision": "denied"},
        ]}},
        completed_at="2026-09-29T00:00:01+00:00",
    )

    candidates = runtime._parent_knowledge_evidence_candidates(
        run.run_id, source_ids=(record["source_id"],), account_ids=("mail_account",),
    )

    assert len(candidates) == 1
    assert candidates[0].evidence.source_id == record["source_id"]
    assert candidates[0].evidence.source_ref == record["source_ref"]
    assert candidates[0].excerpt == record["snippet"][:420]
    (root / "notes.md").unlink()
    runtime.index_workspace(str(root), options={"index_knowledge": True})
    assert runtime._parent_knowledge_evidence_candidates(
        run.run_id, source_ids=(record["source_id"],), account_ids=("mail_account",),
    ) == ()
