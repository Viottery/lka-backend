from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.storage.keyed_write_admission import KeyedWriteAdmission, KeyedWriteAdmissionError


def wait_for(predicate):
    deadline = time.monotonic()+2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_fifo_waiters_do_not_enter_the_connection_scope_before_the_owner_exits():
    gate = KeyedWriteAdmission(capacity=4)
    release = threading.Event()
    entered = []

    def write(index):
        with gate.admit('hot-session'):
            entered.append(index)
            if index == 0:
                assert release.wait(2)

    with ThreadPoolExecutor(max_workers=3) as pool:
        owner = pool.submit(write, 0)
        wait_for(lambda: entered == [0])
        second = pool.submit(write, 1)
        wait_for(lambda: gate.snapshot()['tickets'] == 2)
        third = pool.submit(write, 2)
        wait_for(lambda: gate.snapshot()['tickets'] == 3)
        assert entered == [0]
        release.set()
        for future in (owner, second, third):
            future.result(timeout=2)
    assert entered == [0, 1, 2]
    assert gate.snapshot() == {'keys': 0, 'tickets': 0}


def test_timeout_and_full_never_release_an_executing_owner():
    gate = KeyedWriteAdmission(capacity=2, wait_seconds=0.2)
    with gate.admit('hot'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(lambda: gate.admit('hot').__enter__())
            wait_for(lambda: gate.snapshot()['tickets'] == 2)
            with pytest.raises(KeyedWriteAdmissionError, match='full'), gate.admit('other'):
                pytest.fail('full gate entered')
            with pytest.raises(KeyedWriteAdmissionError, match='timeout'):
                waiting.result(timeout=1)
        assert gate.snapshot() == {'keys': 1, 'tickets': 1}
    with gate.admit('hot'):
        pass
    assert gate.snapshot() == {'keys': 0, 'tickets': 0}


def test_distinct_sessions_are_parallel_and_exception_releases_only_its_owner():
    gate = KeyedWriteAdmission(capacity=3)
    with gate.admit('session-a'):
        with pytest.raises(ValueError), gate.admit('session-b'):
            assert gate.snapshot() == {'keys': 2, 'tickets': 2}
            raise ValueError('transaction rolled back')
        assert gate.snapshot() == {'keys': 1, 'tickets': 1}
    assert gate.snapshot() == {'keys': 0, 'tickets': 0}
