from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.api.routes.health import health
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
    called = threading.Event()

    def fake_sync_outlook_mail(*, folder, limit, max_pages, trigger):
        called.set()
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
    assert called.wait(2)
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


def test_runtime_start_and_health_route_do_not_wait_for_startup_mail_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    config = runtime.local_app_config.mail.outlook
    config.enabled = True
    config.startup_sync_enabled = True
    config.background_sync_enabled = False
    started = threading.Event()
    release = threading.Event()

    def blocked_sync(**_kwargs):
        started.set()
        assert release.wait(30)
        return OutlookSyncResult(
            account_id="mail_account_blocked_startup",
            folder="Inbox",
            imported_messages=0,
            imported_attachments=0,
        )

    monkeypatch.setattr(runtime, "sync_outlook_mail", blocked_sync)
    runtime.start()
    try:
        assert started.wait(2)
        worker = runtime._mail_sync_thread
        assert worker is not None and worker.is_alive()
        response = health(SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(runtime=runtime))
        ))
        assert response.status == "ok"
        assert worker.is_alive()
    finally:
        release.set()
        runtime.stop()


def test_background_only_and_disabled_sync_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    config = runtime.local_app_config.mail.outlook
    config.enabled = True
    config.startup_sync_enabled = False
    config.background_sync_enabled = True
    config.sync_interval_seconds = 1
    triggers: list[str] = []
    called = threading.Event()

    def fake_sync_outlook_mail(*, trigger, **_kwargs):
        triggers.append(trigger)
        called.set()
        return OutlookSyncResult(
            account_id="mail_account_background_only",
            folder="Inbox",
            imported_messages=0,
            imported_attachments=0,
        )

    monkeypatch.setattr(runtime, "sync_outlook_mail", fake_sync_outlook_mail)
    runtime.start()
    assert triggers == []
    assert called.wait(3)
    runtime.stop()
    assert triggers[0] == "background"

    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "disabled-data"))
    get_settings.cache_clear()
    disabled_runtime = create_app().state.runtime
    disabled = disabled_runtime.local_app_config.mail.outlook
    disabled.enabled = False
    disabled.startup_sync_enabled = True
    disabled.background_sync_enabled = True
    disabled_runtime.start()
    assert disabled_runtime._mail_sync_thread is None
    assert disabled_runtime.last_mail_sync_result["status"] == "skipped"
    disabled_runtime.stop()


def test_stop_timeout_retains_mail_worker_and_runtime_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    config = runtime.local_app_config.mail.outlook
    config.enabled = True
    config.startup_sync_enabled = True
    config.background_sync_enabled = False
    started = threading.Event()
    release = threading.Event()

    def blocked_sync(**_kwargs):
        started.set()
        assert release.wait(5)
        return OutlookSyncResult(
            account_id="mail_account_stop_timeout",
            folder="Inbox",
            imported_messages=0,
            imported_attachments=0,
        )

    monkeypatch.setattr(runtime, "sync_outlook_mail", blocked_sync)
    runtime.start()
    assert started.wait(2)
    worker = runtime._mail_sync_thread
    assert worker is not None
    join = worker.join
    worker.join = lambda timeout=None: join(0.01)
    with pytest.raises(RuntimeError, match="Mail synchronization did not drain"):
        runtime.stop()
    assert runtime._mail_sync_thread is worker
    assert runtime._runtime_started and not runtime._runtime_closed

    release.set()
    del worker.join
    runtime.stop()
    assert runtime._mail_sync_thread is None
    assert runtime._runtime_closed
