from __future__ import annotations

import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.tools import ToolContext
from app.integrations.outlook import OutlookConfigError, OutlookSyncResult


def test_runtime_start_runs_configured_outlook_startup_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    runtime = app.state.runtime
    config = runtime.local_app_config.mail.outlook
    config.enabled = True
    config.startup_sync_enabled = True
    config.background_sync_enabled = False
    config.sync_folder = "Inbox"
    config.sync_limit = 7
    config.sync_max_pages = 2
    calls: list[dict] = []

    def fake_sync_outlook_mail(*, folder, limit, max_pages, trigger):
        calls.append(
            {
                "folder": folder,
                "limit": limit,
                "max_pages": max_pages,
                "trigger": trigger,
            }
        )
        return OutlookSyncResult(
            account_id="mail_account_startup",
            folder=folder or "Inbox",
            imported_messages=1,
            imported_attachments=0,
        )

    monkeypatch.setattr(runtime, "sync_outlook_mail", fake_sync_outlook_mail)

    runtime.start()
    runtime.stop()

    assert calls == [
        {
            "folder": "Inbox",
            "limit": 7,
            "max_pages": 2,
            "trigger": "startup",
        }
    ]


def test_disabled_outlook_sync_never_reaches_provider_from_runtime_or_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    provider_calls: list[dict] = []

    def should_not_sync(**kwargs):
        provider_calls.append(kwargs)
        raise AssertionError("disabled Outlook sync must not call the provider")

    monkeypatch.setattr(runtime.outlook_service, "sync_messages", should_not_sync)

    with pytest.raises(OutlookConfigError, match="disabled"):
        runtime.sync_outlook_mail()

    result = runtime.tool_executor.execute(
        invocation_id="disabled_outlook_sync_tool",
        tool_name="mail.sync",
        tool_input={},
        context=ToolContext(
            session_id="session_disabled_outlook_sync",
            trace_id="trace_disabled_outlook_sync",
            safety_review_approved=True,
        ),
    )

    assert result.status == "failed"
    assert "disabled" in (result.error or "").lower()
    assert provider_calls == []
