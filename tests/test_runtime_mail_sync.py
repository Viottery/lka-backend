from __future__ import annotations

from app.api.main import create_app
from app.core.config import get_settings
from app.integrations.outlook import OutlookSyncResult


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
