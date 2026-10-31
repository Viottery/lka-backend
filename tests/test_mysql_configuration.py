"""Configuration must never turn an isolated MySQL test into a default SQLite run."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.storage.connections import (
    configure_mysql,
    connect_database,
    mysql_config_for,
    unregister_mysql,
)


def test_settings_field_names_and_environment_aliases_honor_isolation(tmp_path, monkeypatch):
    for name in ("LKA_DATA_DIR", "LKA_STORAGE_BACKEND", "LKA_MYSQL_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    named = Settings(_env_file=None, data_dir=tmp_path, storage_backend="sqlite")
    aliased = Settings(_env_file=None, LKA_DATA_DIR=tmp_path, LKA_STORAGE_BACKEND="sqlite")
    assert named.data_dir == aliased.data_dir == tmp_path
    assert named.storage_backend == "sqlite"


def test_mysql_password_required_and_redacted(tmp_path):
    with pytest.raises(ValidationError, match="requires LKA_MYSQL_PASSWORD"):
        Settings(_env_file=None, storage_backend="mysql", mysql_password=None)
    settings = Settings(_env_file=None, data_dir=tmp_path, storage_backend="mysql",
                        mysql_password="synthetic-private-value", mysql_database="test_lka")
    assert settings.mysql_config().database == "test_lka"
    assert "synthetic-private-value" not in repr(settings)
    assert "synthetic-private-value" not in repr(settings.mysql_config())


def test_mysql_router_never_creates_sqlite_when_provider_fails(tmp_path, monkeypatch):
    from app.storage import mysql
    logical_path = tmp_path / "lka.sqlite3"
    config = object()
    configure_mysql(logical_path, config)
    def unavailable(*args, **kwargs):
        raise mysql.MySQLOperationalError("synthetic unavailable")
    monkeypatch.setattr(mysql, "connect_mysql", unavailable)
    try:
        with pytest.raises(mysql.MySQLOperationalError, match="synthetic unavailable"):
            connect_database(logical_path)
        assert not logical_path.exists()
        assert mysql_config_for(Path(logical_path).as_uri() + "?mode=ro") is config
    finally:
        unregister_mysql(logical_path, expected=config)


def test_default_router_retains_sqlite(tmp_path):
    path = tmp_path / "default.sqlite3"
    with connect_database(path) as conn:
        conn.execute("CREATE TABLE example(value INTEGER)")
        conn.execute("INSERT INTO example VALUES(7)")
        assert conn.execute("SELECT value FROM example").fetchone()[0] == 7
    assert path.exists()


def test_schema_cache_requires_full_signature_and_invalidates_on_new_owner(tmp_path):
    from app.storage.connections import mark_mysql_schema_verified, mysql_schema_is_verified

    path, config = tmp_path / "mysql.sqlite3", object()
    first_guard, second_guard = object(), object()
    configure_mysql(path, config, transaction_guard=first_guard)
    try:
        assert not mysql_schema_is_verified(path)
        with pytest.raises(ValueError):
            mark_mysql_schema_verified(path, config, ())
        with pytest.raises(RuntimeError):
            mark_mysql_schema_verified(path, object(), ((1, "schema", "hash"),))
        mark_mysql_schema_verified(path, config, ((1, "schema", "hash"),))
        assert mysql_schema_is_verified(path)
        configure_mysql(path, config, transaction_guard=first_guard)
        assert mysql_schema_is_verified(path)
        configure_mysql(path, config, transaction_guard=second_guard)
        assert not mysql_schema_is_verified(path)
    finally:
        unregister_mysql(path, expected=config)
    assert not mysql_schema_is_verified(path)
