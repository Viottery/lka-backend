from __future__ import annotations

import threading

import pytest

from app.storage.mysql import MySQLConfig, MySQLOperationalError
from app.storage.mysql_owner import MySQLCoordinatorOwner


class _Cursor:
    def __init__(self, raw):
        self.raw = raw
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, parameters=()):
        self.raw.statements.append((statement, parameters))
        if statement.startswith("SELECT GET_LOCK"):
            self.result = (1,)
        elif statement == "SELECT CONNECTION_ID()":
            self.result = (self.raw.connection_id,)
        elif statement == "SELECT @@SESSION.wait_timeout":
            self.result = (self.raw.wait_timeout,)
        elif statement.startswith("SET SESSION wait_timeout"):
            self.raw.wait_timeout = parameters[0]
            self.result = (1,)
        elif statement.startswith("SELECT IS_USED_LOCK"):
            self.raw.heartbeat_started.set()
            if self.raw.block_heartbeat:
                self.raw.allow_heartbeat.wait(timeout=2)
            if self.raw.fail_heartbeat:
                raise OSError("simulated owner-session loss")
            self.raw.heartbeat_count += 1
            self.result = (1,)
        else:
            self.result = (1,)
        return 1

    def fetchone(self):
        return self.result


class _RawConnection:
    def __init__(self, connection_id=41):
        self.connection_id = connection_id
        self.wait_timeout = 300
        self.open = True
        self.statements = []
        self.heartbeat_count = 0
        self.heartbeat_started = threading.Event()
        self.allow_heartbeat = threading.Event()
        self.block_heartbeat = False
        self.fail_heartbeat = False

    def cursor(self):
        return _Cursor(self)

    def begin(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.open = False


class _Driver:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        return self.raw


def _config():
    return MySQLConfig(host="127.0.0.1", port=3306, user="owner", password="secret", database="test_lka")


def test_acquire_sets_bounded_owner_timeouts_and_starts_heartbeat(monkeypatch):
    raw = _RawConnection()
    driver = _Driver(raw)
    monkeypatch.setattr("app.storage.mysql_owner._load_driver", lambda: driver)
    owner = MySQLCoordinatorOwner(_config(), heartbeat_interval_seconds=0.01)
    try:
        assert owner.acquire()
        assert owner.is_active
        assert not owner.ownership_lost
        assert owner.fence_identity() == (owner._token, raw.connection_id)
        connect_args = driver.calls[0]
        assert connect_args["connect_timeout"] <= 5
        assert connect_args["read_timeout"] <= 5
        assert connect_args["write_timeout"] <= 5
        assert ("SET SESSION wait_timeout=%s", (300,)) in raw.statements
        assert raw.heartbeat_started.wait(timeout=0.5)
        assert raw.heartbeat_count >= 1
    finally:
        owner.release()


def test_heartbeat_network_io_does_not_hold_state_lock(monkeypatch):
    raw = _RawConnection()
    raw.block_heartbeat = True
    driver = _Driver(raw)
    monkeypatch.setattr("app.storage.mysql_owner._load_driver", lambda: driver)
    owner = MySQLCoordinatorOwner(_config(), heartbeat_interval_seconds=0.01)
    assert owner.acquire()
    try:
        assert raw.heartbeat_started.wait(timeout=0.5)
        identity_ready = threading.Event()
        result = []

        def snapshot():
            result.append(owner.fence_identity())
            identity_ready.set()

        snapshot_thread = threading.Thread(target=snapshot)
        snapshot_thread.start()
        assert identity_ready.wait(timeout=0.25), "state snapshot blocked behind heartbeat I/O"
        snapshot_thread.join(timeout=0.2)
        assert result == [(owner._token, raw.connection_id)]
    finally:
        raw.allow_heartbeat.set()
        owner.release()


def test_heartbeat_failure_is_fail_closed_without_reconnect(monkeypatch):
    raw = _RawConnection()
    driver = _Driver(raw)
    monkeypatch.setattr("app.storage.mysql_owner._load_driver", lambda: driver)
    owner = MySQLCoordinatorOwner(_config(), heartbeat_interval_seconds=0.01)
    assert owner.acquire()
    raw.fail_heartbeat = True
    try:
        for _ in range(100):
            if owner.ownership_lost:
                break
            threading.Event().wait(0.01)
        assert owner.ownership_lost
        assert not owner.is_active
        assert not raw.open
        assert not owner.acquire()
        assert len(driver.calls) == 1
        with pytest.raises(MySQLOperationalError, match="not active"):
            owner.fence_identity()
    finally:
        owner.release()


def test_release_stops_heartbeat_thread_without_leak(monkeypatch):
    raw = _RawConnection()
    driver = _Driver(raw)
    monkeypatch.setattr("app.storage.mysql_owner._load_driver", lambda: driver)
    owner = MySQLCoordinatorOwner(_config(), heartbeat_interval_seconds=0.01)
    assert owner.acquire()
    thread = owner._heartbeat_thread
    assert raw.heartbeat_started.wait(timeout=0.5)
    owner.release()
    assert thread is not None and not thread.is_alive()
    assert not owner.is_active
    assert not owner.ownership_lost


def test_mysql_health_refuses_inactive_owner_without_network_access():
    from types import SimpleNamespace

    from app.api.schemas import HealthResponse
    from app.core.runtime import LocalKnowledgeAgentRuntime

    runtime = object.__new__(LocalKnowledgeAgentRuntime)
    runtime.settings = SimpleNamespace(version='test', app_name='local-knowledge-agent-os',
                                       deployment_id=None, storage_backend='mysql')
    runtime._mysql_owner = SimpleNamespace(is_active=False)
    response = HealthResponse(**runtime.health())
    assert response.status == 'unavailable'
    assert response.storage_backend == 'mysql'
    runtime._mysql_owner.is_active = True
    assert runtime.health()['status'] == 'ok'


def test_owner_heartbeat_rejects_renewal_at_the_expiry_boundary():
    import pytest

    from app.storage.mysql import MySQLConfig
    from app.storage.mysql_owner import MySQLCoordinatorOwner

    config = MySQLConfig(host='127.0.0.1', port=3307, user='synthetic',
                         password='synthetic', database='synthetic_lab')
    with pytest.raises(ValueError, match='margin'):
        MySQLCoordinatorOwner(config, heartbeat_interval_seconds=300)
