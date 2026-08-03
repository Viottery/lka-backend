from __future__ import annotations

from types import SimpleNamespace

from app.api.routes.workspaces import index_workspace
from app.api.schemas import WorkspaceIndexRequest
from app.core.config import get_settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.platform import FilesystemScanner, PathResolver, ScanOptions, detect_platform


def test_detect_platform_supports_explicit_windows_mode():
    platform = detect_platform("windows")

    assert platform.name == "windows"
    assert platform.path_style == "windows"
    assert platform.default_shell == "powershell"
    assert platform.case_sensitive_paths is False


def test_filesystem_scanner_skips_hidden_files_and_limits_recursion(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Demo\n", encoding="utf-8")
    (workspace / ".hidden").write_text("hidden\n", encoding="utf-8")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child\n", encoding="utf-8")

    platform = detect_platform("linux")
    resolver = PathResolver(platform)
    resolved = resolver.resolve_workspace(str(workspace))
    scanner = FilesystemScanner(platform)

    result = scanner.scan_workspace(
        resolved,
        options=ScanOptions(recursive=False, skip_hidden=True),
    )

    assert result.indexed_files == 1
    assert result.sampled_files[0].relative_path == "README.md"
    assert result.sampled_files[0].role == "readme"


def test_workspace_index_uses_platform_scan_options(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Demo\n", encoding="utf-8")
    (workspace / ".hidden").write_text("hidden\n", encoding="utf-8")

    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_PLATFORM", "linux")
    monkeypatch.setenv("LKA_SKIP_HIDDEN", "true")
    get_settings.cache_clear()

    runtime = LocalKnowledgeAgentRuntime(get_settings())
    response = index_workspace(
        WorkspaceIndexRequest(
            workspace=str(workspace),
            source_frontend="linux-native",
            options={"recursive": True},
        ),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime))),
    )

    assert response.indexed_files == 1
    assert response.indexed_chunks == 4
    assert response.workspace_id.startswith("ws_")
