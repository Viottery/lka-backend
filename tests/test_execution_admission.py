import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.core.execution_admission import (
    ExecutionAdmission,
    ExecutionCapacityError,
    admitted_execution,
    child_execution_scope,
    suspend_execution,
)


def test_reserved_capacity_reentrant_scope_and_release_after_failure():
    governor = ExecutionAdmission()
    background = [governor.try_acquire('background') for _ in range(2)]
    assert governor.try_acquire('background') is None
    assert all(background)
    with governor.scope():
        with governor.scope():
            assert governor.snapshot()['interactive'] == 1
        with governor.scope(key='another'):
            assert governor.snapshot()['interactive'] == 1
    assert governor.snapshot()['interactive'] == 0
    with pytest.raises(RuntimeError), governor.scope():
        raise RuntimeError('failure')
    for lease in background:
        lease.close()
    assert sum(governor.snapshot()[kind] for kind in ('interactive', 'background', 'child')) == 0


def test_overload_rejects_before_creating_work():
    governor = ExecutionAdmission(total=1, interactive_reserved=0)
    held = governor.try_acquire()
    created = []

    class Runtime:
        execution_admission = governor

        @admitted_execution
        def turn(self):
            created.append('run')

    with pytest.raises(ExecutionCapacityError):
        Runtime().turn()
    assert created == []
    held.close()
    Runtime().turn()
    assert created == ['run']


def test_reference_keeps_permit_while_worker_outlives_cancelled_request():
    governor = ExecutionAdmission(total=1, interactive_reserved=0)
    lease = governor.try_acquire()
    entered, finish = threading.Event(), threading.Event()

    def worker():
        with lease.activate():
            entered.set()
            assert finish.wait(2)

    with lease.activate():
        thread = threading.Thread(target=worker)
        thread.start()
        assert entered.wait(2)
    assert governor.try_acquire() is None
    finish.set()
    thread.join(2)
    assert not thread.is_alive()
    assert governor.snapshot()['interactive'] == 0


def test_parent_and_nested_child_wait_release_and_reacquire_capacity():
    async def scenario():
        governor = ExecutionAdmission(total=1, interactive_reserved=0, children=1)
        marks = []

        async def leaf():
            async with child_execution_scope(governor, 'leaf', lambda: None):
                assert governor.snapshot()['child'] == 1
                marks.append('leaf')

        with governor.scope(key='parent'):
            async with suspend_execution():
                assert governor.snapshot()['interactive'] == 0
                async with child_execution_scope(governor, 'child', lambda: None):
                    async with suspend_execution():
                        await leaf()
                    assert governor.snapshot()['child'] == 1
                assert governor.snapshot()['child'] == 0
            assert governor.snapshot()['interactive'] == 1
        assert marks == ['leaf']
        assert governor.snapshot()['interactive'] == 0

    asyncio.run(scenario())


def test_waiting_child_cancel_does_not_leak_permit():
    async def scenario():
        governor = ExecutionAdmission(children=1)
        lease = governor.try_acquire('child')

        async def wait():
            async with child_execution_scope(governor, 'wait', lambda: None):
                pytest.fail('capacity was occupied')

        task = asyncio.create_task(wait())
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert governor.snapshot()['child'] == 1
        lease.close()
        assert governor.snapshot()['child'] == 0
    asyncio.run(scenario())


def test_admitted_resume_uses_child_lane():
    governor = ExecutionAdmission(children=1)
    manager = SimpleNamespace(get_run=lambda _: SimpleNamespace(parent_run_id='parent'))
    runtime = SimpleNamespace(execution_admission=governor, agent_run_manager=manager)

    @admitted_execution
    def resume(self, run_id):
        assert governor.snapshot()['child'] == 1
        assert governor.snapshot()['interactive'] == 0

    resume(runtime, 'child')
    resume(runtime, run_id='child')
    assert governor.snapshot()['child'] == 0


@pytest.mark.parametrize('runner_kind', ['graph', 'legacy'])
def test_cancelled_async_entry_keeps_capacity_until_daemon_finishes(runner_kind):
    from app.core.agent_graph import AgentGraphRunner
    from app.core.agent_turn import AgentTurnLoop

    governor = ExecutionAdmission(total=1, interactive_reserved=0)
    entered, finish = threading.Event(), threading.Event()

    def blocking_call(**kwargs):
        entered.set()
        assert finish.wait(3)

    async def scenario():
        with governor.scope():
            if runner_kind == 'graph':
                work = AgentGraphRunner._run_in_worker(blocking_call)
            else:
                runner = object.__new__(AgentTurnLoop)
                runner.run = blocking_call
                work = runner.run_async(session_id='synthetic', user_input='wait')
            task = asyncio.create_task(work)
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert governor.try_acquire() is None
        finish.set()
        for _ in range(100):
            if governor.snapshot()['interactive'] == 0:
                break
            await asyncio.sleep(0.01)
        assert governor.snapshot()['interactive'] == 0

    try:
        asyncio.run(scenario())
    finally:
        finish.set()
